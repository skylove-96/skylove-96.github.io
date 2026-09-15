"""서울 아파트 전월세 실거래 데이터로 블로그 글을 자동 생성한다.

매일 실행을 상정하며, 아래 순서로 동작한다:
  1. 이번 달/전월 실거래 데이터를 한 번 수집 (apt_rent API)
  2. 후보 주제(전세/월세)를 날짜 기준으로 순서를 바꿔가며 시도
  3. 거래건수가 너무 적거나(MIN_TRANSACTIONS) 본문이 너무 짧으면(MIN_BODY_CHARS)
     해당 주제를 건너뛰고 다음 후보로 넘어감
  4. 표/차트 생성 자체가 실패하면(코드/환경 문제) 다른 주제로 넘어가지 않고 즉시 중단
  5. 성공하면 stdout에 `::post-meta::{...}` 한 줄을 출력해 CI가 PR 정보를 읽어가게 함

사용 예:
    python generate_post.py                 # 지난달 데이터, 실제 API
    python generate_post.py --month 202608  # 특정 월 지정
    python generate_post.py --mock          # API 키 없이 가짜 데이터로 파이프라인만 검증
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PIPELINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PIPELINE_DIR.parent
sys.path.insert(0, str(PIPELINE_DIR))

load_dotenv(PROJECT_ROOT / ".env")

from config import (  # noqa: E402
    CATEGORY_LABEL,
    DATASETS,
    MIN_BODY_CHARS,
    MIN_TRANSACTIONS,
    PROMO_PROBABILITY,
    SEOUL_DISTRICTS,
)
from src import analyze, books, chart, molit_api  # noqa: E402
from src.molit_api import ApiKey  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
BOOKS_FILE = PIPELINE_DIR / "books.json"
CONTENT_POSTS_DIR = PROJECT_ROOT / "content" / "posts"
DATA_SOURCE_URL = "https://www.data.go.kr/data/15126474/openapi.do"
REGION_LABEL = "서울"
DATASET_KEY = "apt_rent"  # 두 후보 주제 모두 같은 API(apt_rent)에서 필터만 다르게 뽑아 쓴다.

TOPICS = [
    {
        "id": "jeonse",
        "filter_fn": analyze.filter_pure_jeonse,
        "value_col": "보증금액",
        "out_prefix": "전세보증금",
        "unit_divisor": 10000.0,
        "unit_name": "억원",
        "deal_desc": "순수 전세",
        "exclude_desc": "월세를 낀 반전세/월세 계약은 제외",
        "title_template": "{year}년 {month}월 {region} 아파트 전세가 동향",
        "tags": ["아파트", "전세", "실거래가"],
    },
    {
        "id": "wolse",
        "filter_fn": analyze.filter_wolse,
        "value_col": "월세금액",
        "out_prefix": "월세",
        "unit_divisor": 1.0,
        "unit_name": "만원",
        "deal_desc": "월세(반전세 포함)",
        "exclude_desc": "순수 전세 계약은 제외",
        "title_template": "{year}년 {month}월 {region} 아파트 월세 시장 동향",
        "tags": ["아파트", "월세", "실거래가"],
    },
]


class TopicSkipped(Exception):
    """데이터가 너무 적거나 본문이 너무 짧아 이 주제를 건너뛸 때 발생 (다음 후보 시도)."""


def resolve_month(yyyymm: str | None) -> str:
    if yyyymm:
        return yyyymm
    today = datetime.now(KST)
    last_month_end = today.replace(day=1) - timedelta(days=1)
    return last_month_end.strftime("%Y%m")


def month_before(yyyymm: str) -> str:
    year, month = int(yyyymm[:4]), int(yyyymm[4:])
    if month == 1:
        return f"{year - 1}12"
    return f"{year}{month - 1:02d}"


def make_mock_rows(deal_ymd: str, districts: list[str]) -> list[dict]:
    """API 키 없이 정제/차트/글쓰기 로직을 검증하기 위한 가짜 데이터. 실제 발행에는 쓰지 않는다."""
    rng = random.Random(deal_ymd)
    rows = []
    for i, district in enumerate(districts):
        base = 30000 + i * 1500
        for j in range(rng.randint(15, 40)):
            deposit = max(base + rng.randint(-3000, 5000), 5000)
            rows.append(
                {
                    "구명": district,
                    "법정동": f"{district} {j}동",
                    "전용면적": str(rng.randint(50, 110)),
                    "층": str(rng.randint(1, 20)),
                    "보증금액": f"{deposit:,}",
                    "월세금액": "0" if rng.random() > 0.2 else str(rng.randint(10, 80)),
                    "년": deal_ymd[:4],
                    "월": str(int(deal_ymd[4:])),
                }
            )
    return rows


def fetch_month_rows(operation: str, deal_ymd: str, api_key: ApiKey, mock: bool) -> list[dict]:
    if mock:
        return make_mock_rows(deal_ymd, list(SEOUL_DISTRICTS.keys()))
    raw_rows = molit_api.fetch_region_month(operation, SEOUL_DISTRICTS, deal_ymd, api_key)
    return [molit_api.normalize_rent_row(row) for row in raw_rows]


def district_table_markdown(district_summary, mean_col: str, median_col: str, unit_divisor: float) -> str:
    lines = [f"| 자치구 | {mean_col} | {median_col} | 거래건수 |", "|---|---:|---:|---:|"]
    for _, row in district_summary.iterrows():
        lines.append(
            f"| {row['구명']} | {row[mean_col] / unit_divisor:.1f} | "
            f"{row[median_col] / unit_divisor:.1f} | {int(row['거래건수'])} |"
        )
    return "\n".join(lines)


def build_body(
    *,
    topic: dict,
    title: str,
    deal_ymd: str,
    prev_ymd: str,
    district_summary,
    insights: list[str],
) -> str:
    year, month = deal_ymd[:4], int(deal_ymd[4:])
    mean_col = f"평균{topic['out_prefix']}"
    median_col = f"중위{topic['out_prefix']}"
    insight_lines = "\n".join(f"- {line}" for line in insights)
    table_md = district_table_markdown(district_summary, mean_col, median_col, topic["unit_divisor"])

    return f"""
공공데이터포털의 국토교통부 아파트 전월세 실거래자료를 파이썬으로 직접 수집·분석해
{year}년 {month}월 {REGION_LABEL} 아파트 {topic['deal_desc']} 시장 흐름을 정리했습니다.

## 분석 방법

- 데이터 출처: [국토교통부 아파트 전월세 실거래자료]({DATA_SOURCE_URL}) (공공데이터포털 Open API)
- 분석 대상: {REGION_LABEL} 25개 자치구, {year}년 {month}월 신고 기준 {topic['deal_desc']} 계약
- 비교 기준월: {prev_ymd[:4]}년 {int(prev_ymd[4:])}월 (전월 대비 증감률 계산용)
- 실거래 신고는 계약 후 30일 이내에 이루어지므로, 최근월 데이터는 이후 계속 소폭 갱신될 수 있습니다.

## 자치구별 {mean_col}

![{year}년 {month}월 {REGION_LABEL} 자치구별 {mean_col}](chart.png)

{table_md}

## 이번 달 인사이트

{insight_lines}
"""


def try_topic(
    topic: dict,
    this_df_all,
    prev_df_all,
    deal_ymd: str,
    prev_ymd: str,
) -> dict:
    """주제 하나를 시도한다. 데이터 부족/본문 부족이면 TopicSkipped를 던진다.

    TopicSkipped 이후(표/차트 생성 단계)에서 나는 예외는 그대로 전파되어
    호출부에서 '기술적 실패'로 처리되고, 다른 주제로 넘어가지 않는다.
    """
    this_df = topic["filter_fn"](this_df_all)
    prev_df = topic["filter_fn"](prev_df_all)

    if len(this_df) < MIN_TRANSACTIONS:
        raise TopicSkipped(
            f"[{topic['id']}] 거래건수 부족: {len(this_df)}건 < 최소 {MIN_TRANSACTIONS}건"
        )

    district_summary = analyze.summarize_by_district(this_df, topic["value_col"], topic["out_prefix"])
    month_label = f"{deal_ymd[:4]}년 {int(deal_ymd[4:])}월"
    insights = analyze.build_insights(
        this_df,
        prev_df,
        district_summary,
        REGION_LABEL,
        month_label,
        value_col=topic["value_col"],
        out_prefix=topic["out_prefix"],
        unit_divisor=topic["unit_divisor"],
        unit_name=topic["unit_name"],
        deal_desc=topic["deal_desc"],
        exclude_desc=topic["exclude_desc"],
    )

    year, month = deal_ymd[:4], int(deal_ymd[4:])
    title = topic["title_template"].format(year=year, month=month, region=REGION_LABEL)
    slug = f"seoul-apt-{topic['id']}-{deal_ymd}"

    # ---- 여기부터 표/차트/본문 생성. 이 구간에서 나는 예외는 '기술적 실패'로 취급한다. ----
    body = build_body(
        topic=topic,
        title=title,
        deal_ymd=deal_ymd,
        prev_ymd=prev_ymd,
        district_summary=district_summary,
        insights=insights,
    )

    if len(body) < MIN_BODY_CHARS:
        raise TopicSkipped(f"[{topic['id']}] 본문 글자수 부족: {len(body)}자 < 최소 {MIN_BODY_CHARS}자")

    post_dir = CONTENT_POSTS_DIR / slug
    post_dir.mkdir(parents=True, exist_ok=True)
    chart.district_bar_chart(
        district_summary,
        post_dir / "chart.png",
        title=f"{month_label} {REGION_LABEL} 자치구별 평균 {topic['out_prefix']}",
        mean_col=f"평균{topic['out_prefix']}",
        unit_divisor=topic["unit_divisor"],
        unit_name=topic["unit_name"],
    )

    promo = None
    if random.random() < PROMO_PROBABILITY:
        promo = books.promo_for_book_key(BOOKS_FILE, DATASETS[DATASET_KEY]["book_key"])
    if promo:
        body += f"\n---\n\n{promo}\n"

    summary = f"{month_label} {REGION_LABEL} 아파트 {topic['deal_desc']} 실거래 데이터 분석"
    now = datetime.now(KST)
    front_matter = (
        "---\n"
        f'title: "{title}"\n'
        f"date: {now.strftime('%Y-%m-%dT%H:%M:%S+09:00')}\n"
        "draft: false\n"
        f"tags: {json.dumps(topic['tags'], ensure_ascii=False)}\n"
        f'categories: ["{CATEGORY_LABEL}"]\n'
        f'summary: "{summary}"\n'
        "---\n"
    )
    (post_dir / "index.md").write_text(front_matter + body, encoding="utf-8")

    return {
        "title": title,
        "summary": summary,
        "slug": slug,
        "path": f"content/posts/{slug}",
        "data_period": f"{month_label} (전월 비교: {prev_ymd[:4]}년 {int(prev_ymd[4:])}월)",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="서울 아파트 전월세 동향 글 자동 생성")
    parser.add_argument("--month", help="YYYYMM 형식. 생략하면 지난달 사용", default=None)
    parser.add_argument("--mock", action="store_true", help="API 키 없이 가짜 데이터로 파이프라인만 검증")
    args = parser.parse_args()

    dataset = DATASETS[DATASET_KEY]
    deal_ymd = resolve_month(args.month)
    prev_ymd = month_before(deal_ymd)

    api_key = ApiKey.from_env()
    if not args.mock and not api_key.is_configured():
        print(
            "DATA_GO_KR_DECODING_KEY(또는 ENCODING_KEY)가 설정되어 있지 않습니다.\n"
            "환경변수/.env를 채우거나, 파이프라인만 테스트하려면 --mock 옵션을 사용하세요.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[1/4] {deal_ymd} / {prev_ymd} {REGION_LABEL} {dataset['label']} 데이터 수집 중...")
    this_rows = fetch_month_rows(dataset["operation"], deal_ymd, api_key, args.mock)
    prev_rows = fetch_month_rows(dataset["operation"], prev_ymd, api_key, args.mock)
    print(f"[2/4] 데이터 정제 중... (이번달 원본 {len(this_rows)}건, 전월 원본 {len(prev_rows)}건)")
    this_df_all = analyze.to_dataframe(this_rows)
    prev_df_all = analyze.to_dataframe(prev_rows)

    # 날짜(연중 일수) 기준으로 후보 순서를 돌려가며 시도 -> 매일 같은 주제만 반복되는 것을 피한다.
    today_ordinal = datetime.now(KST).toordinal()
    start = today_ordinal % len(TOPICS)
    ordered_topics = TOPICS[start:] + TOPICS[:start]

    print(f"[3/4] 후보 주제 시도 순서: {[t['id'] for t in ordered_topics]}")
    skip_reasons = []
    for topic in ordered_topics:
        try:
            result = try_topic(topic, this_df_all, prev_df_all, deal_ymd, prev_ymd)
        except TopicSkipped as exc:
            print(f"  건너뜀: {exc}")
            skip_reasons.append(str(exc))
            continue

        print(f"[4/4] 완료: {result['path']}/index.md")
        print("::post-meta::" + json.dumps(result, ensure_ascii=False))
        return

    print("오늘 발행할 만한 주제가 없습니다 (모든 후보가 데이터/품질 기준 미달):")
    for reason in skip_reasons:
        print(f"  - {reason}")
    sys.exit(0)


if __name__ == "__main__":
    main()
