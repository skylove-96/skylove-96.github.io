"""서울 아파트 전세가 동향 블로그 글을 실거래가 데이터로 자동 생성한다 (현재는 수동 실행 샘플).

사용 예:
    python generate_post.py --month 202608          # 실제 API로 2026년 8월 데이터 사용
    python generate_post.py --month 202608 --mock    # API 키 없이 가짜 데이터로 파이프라인만 검증
    python generate_post.py                          # --month 생략 시 지난달 자동 사용
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

from config import CATEGORY_LABEL, DATASETS, PROMO_PROBABILITY, SEOUL_DISTRICTS  # noqa: E402
from src import analyze, books, chart, molit_api  # noqa: E402
from src.molit_api import ApiKey  # noqa: E402

DATASET_KEY = "apt_rent"
BOOKS_FILE = PIPELINE_DIR / "books.json"
CONTENT_POSTS_DIR = PROJECT_ROOT / "content" / "posts"
DATA_SOURCE_URL = "https://www.data.go.kr/data/15126474/openapi.do"


def resolve_month(yyyymm: str | None) -> str:
    """--month가 없으면 지난달(YYYYMM)을 반환한다."""
    if yyyymm:
        return yyyymm
    today = datetime.now(ZoneInfo("Asia/Seoul"))
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


def district_table_markdown(district_summary) -> str:
    lines = ["| 자치구 | 평균 전세보증금(억) | 중위 전세보증금(억) | 거래건수 |", "|---|---:|---:|---:|"]
    for _, row in district_summary.iterrows():
        lines.append(
            f"| {row['구명']} | {row['평균보증금'] / 10000:.1f} | "
            f"{row['중위보증금'] / 10000:.1f} | {int(row['거래건수'])} |"
        )
    return "\n".join(lines)


def render_post(
    *,
    title: str,
    date_str: str,
    tags: list[str],
    summary: str,
    deal_ymd: str,
    prev_ymd: str,
    region_label: str,
    district_summary,
    insights: list[str],
    promo: str | None,
) -> str:
    year, month = deal_ymd[:4], int(deal_ymd[4:])

    front_matter = (
        "---\n"
        f'title: "{title}"\n'
        f"date: {date_str}\n"
        "draft: false\n"
        f"tags: {json.dumps(tags, ensure_ascii=False)}\n"
        f'categories: ["{CATEGORY_LABEL}"]\n'
        f'summary: "{summary}"\n'
        "---\n"
    )

    insight_lines = "\n".join(f"- {line}" for line in insights)

    body = f"""
공공데이터포털의 국토교통부 아파트 전월세 실거래자료를 파이썬으로 직접 수집·분석해
{year}년 {month}월 {region_label} 아파트 전세 시장 흐름을 정리했습니다.

## 분석 방법

- 데이터 출처: [국토교통부 아파트 전월세 실거래자료]({DATA_SOURCE_URL}) (공공데이터포털 Open API)
- 분석 대상: {region_label} 25개 자치구, {year}년 {month}월 신고 기준 순수 전세 계약(월세 없는 건)
- 비교 기준월: {prev_ymd[:4]}년 {int(prev_ymd[4:])}월 (전월 대비 증감률 계산용)
- 실거래 신고는 계약 후 30일 이내에 이루어지므로, 최근월 데이터는 이후 계속 소폭 갱신될 수 있습니다.

## 자치구별 평균 전세보증금

![{year}년 {month}월 서울 자치구별 평균 전세보증금](chart.png)

{district_table_markdown(district_summary)}

## 이번 달 인사이트

{insight_lines}
"""

    if promo:
        body += f"\n---\n\n{promo}\n"

    return front_matter + body


def main() -> None:
    parser = argparse.ArgumentParser(description="서울 아파트 전세가 동향 글 자동 생성")
    parser.add_argument("--month", help="YYYYMM 형식. 생략하면 지난달 사용", default=None)
    parser.add_argument("--mock", action="store_true", help="API 키 없이 가짜 데이터로 파이프라인만 검증")
    args = parser.parse_args()

    dataset = DATASETS[DATASET_KEY]
    deal_ymd = resolve_month(args.month)
    prev_ymd = month_before(deal_ymd)
    region_label = "서울"

    api_key = ApiKey.from_env()
    if not args.mock and not api_key.is_configured():
        print(
            "DATA_GO_KR_DECODING_KEY(또는 ENCODING_KEY)가 .env에 설정되어 있지 않습니다.\n"
            "먼저 .env를 채우거나, 파이프라인만 테스트하려면 --mock 옵션을 사용하세요.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[1/5] {deal_ymd} / {prev_ymd} {region_label} {dataset['label']} 데이터 수집 중...")
    this_rows = fetch_month_rows(dataset["operation"], deal_ymd, api_key, args.mock)
    prev_rows = fetch_month_rows(dataset["operation"], prev_ymd, api_key, args.mock)

    print(f"[2/5] 데이터 정제 중... (이번달 원본 {len(this_rows)}건, 전월 원본 {len(prev_rows)}건)")
    this_df = analyze.filter_pure_jeonse(analyze.to_dataframe(this_rows))
    prev_df = analyze.filter_pure_jeonse(analyze.to_dataframe(prev_rows))

    print("[3/5] 분석 중...")
    district_summary = analyze.summarize_by_district(this_df)
    month_label = f"{deal_ymd[:4]}년 {int(deal_ymd[4:])}월"
    insights = analyze.build_insights(this_df, prev_df, district_summary, region_label, month_label)

    year, month = deal_ymd[:4], int(deal_ymd[4:])
    title = f"{year}년 {month}월 {region_label} 아파트 전세가 동향"
    slug = f"seoul-apt-jeonse-{deal_ymd}"
    post_dir = CONTENT_POSTS_DIR / slug
    post_dir.mkdir(parents=True, exist_ok=True)

    print("[4/5] 차트 생성 중...")
    chart.district_bar_chart(
        district_summary,
        post_dir / "chart.png",
        title=f"{year}년 {month}월 서울 자치구별 평균 전세보증금",
    )

    print("[5/5] 마크다운 글 작성 중...")
    promo = None
    if random.random() < PROMO_PROBABILITY:
        promo = books.promo_for_book_key(BOOKS_FILE, dataset["book_key"])

    now = datetime.now(ZoneInfo("Asia/Seoul"))
    md = render_post(
        title=title,
        date_str=now.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        tags=dataset["tags"],
        summary=f"{month_label} {region_label} 아파트 전세 실거래 데이터 분석",
        deal_ymd=deal_ymd,
        prev_ymd=prev_ymd,
        region_label=region_label,
        district_summary=district_summary,
        insights=insights,
        promo=promo,
    )
    (post_dir / "index.md").write_text(md, encoding="utf-8")
    print(f"완료: {post_dir / 'index.md'}")


if __name__ == "__main__":
    main()
