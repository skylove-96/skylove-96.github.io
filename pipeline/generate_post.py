"""서울 아파트 전월세 실거래 데이터로 블로그 글을 자동 생성한다.

주 3회(월/수/금) 실행을 상정하며, 아래 순서로 동작한다:
  1. 이번 달/전월(또는 전년 동월) 실거래 데이터를 필요한 만큼 수집 (apt_rent API)
  2. 후보 주제(전세/월세/전환율/평형대/전년비교/거래량-가격)를 최근 발행 이력 기준으로
     "오래 쓰이지 않은 주제부터" 순서를 바꿔가며 시도
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
import re
import sys
from datetime import datetime, timedelta
from difflib import SequenceMatcher
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
    PROMO_MIN_GAP,
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
DATASET_KEY = "apt_rent"  # 모든 후보 주제가 같은 API(apt_rent)에서 필터/가공만 다르게 쓴다.

# id는 발행된 글의 slug(seoul-apt-{id}-{yyyymm})에 그대로 쓰이므로 영소문자만 사용한다.
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
    {
        "id": "conversion",
        "filter_fn": analyze.build_conversion_df,
        "value_col": "전환율",
        "out_prefix": "전환율",
        "unit_divisor": 1.0,
        "unit_name": "%",
        "round_ndigits": 2,
        "deal_desc": "반전세(전월세 전환)",
        "exclude_desc": (
            "같은 자치구·같은 달 순수 전세 평균가 대비 환산한 추정치이며, "
            "전환율이 0% 이하이거나 30%를 넘는 이상치는 제외"
        ),
        "title_template": "{year}년 {month}월 {region} 아파트 전월세 전환율 분석",
        "tags": ["아파트", "전월세전환율", "실거래가"],
        "method_note": (
            "전월세 전환율은 실제 시세 대신 같은 자치구·같은 달의 순수 전세 평균 보증금을 "
            "기준가로 삼아 추정한 값입니다(연 환산 %)."
        ),
    },
    {
        "id": "areaband",
        "filter_fn": analyze.build_area_band_df,
        "value_col": "보증금액",
        "out_prefix": "전세보증금",
        "unit_divisor": 10000.0,
        "unit_name": "억원",
        "deal_desc": "순수 전세",
        "exclude_desc": "월세를 낀 반전세/월세 계약은 제외",
        "title_template": "{year}년 {month}월 {region} 아파트 평형대별 전세가 분석",
        "tags": ["아파트", "평형대", "전세", "실거래가"],
        "group_col": "평형대",
        "group_count_desc": f"{len(analyze.AREA_BANDS)}개 평형대 구간",
        "group_noun": "구간",
    },
    {
        "id": "yoy",
        "filter_fn": analyze.filter_pure_jeonse,
        "value_col": "보증금액",
        "out_prefix": "전세보증금",
        "unit_divisor": 10000.0,
        "unit_name": "억원",
        "deal_desc": "순수 전세",
        "exclude_desc": "월세를 낀 반전세/월세 계약은 제외",
        "title_template": "{year}년 {month}월 {region} 아파트 전세가, 1년 전과 비교하면",
        "tags": ["아파트", "전세", "전년동월비교", "실거래가"],
        "compare": "yoy",
        "compare_label": "전년 동월",
    },
]

VOLUME_PRICE_TOPIC_ID = "volumeprice"
VOLUME_PRICE_TITLE_TEMPLATE = "{year}년 {month}월 {region} 아파트 전세 거래량과 가격 변동의 관계"
VOLUME_PRICE_TAGS = ["아파트", "전세", "거래량", "실거래가"]

# 최근 발행 이력을 기준으로 순서를 정할 때 후보로 취급할 전체 주제 id 목록
# (volumeprice는 전용 분석 함수를 쓰므로 TOPICS 리스트에는 없고 자리표시자만 추가한다).
CANDIDATE_TOPICS = TOPICS + [{"id": VOLUME_PRICE_TOPIC_ID}]

SLUG_RE = re.compile(r"^seoul-apt-([a-z]+)-(\d{6})$")
DATE_RE = re.compile(r"^date:\s*(\S+)", re.MULTILINE)
TITLE_RE = re.compile(r'^title:\s*"(.*)"\s*$', re.MULTILINE)

# books.PROMO_TEMPLATE이 항상 이 문구로 시작하므로, 이미 발행된 글에 홍보 문구가
# 있었는지는 본문에 이 문구가 있는지로 판별한다.
PROMO_MARKER = "이런 데이터를 직접 파이썬으로 분석해보고 싶으신 분들을 위해"

# 제목이 겹칠 때 더 구체화를 몇 번까지 시도할지, 그리고 그때마다 슬러그 뒤에 붙일 접미사.
# 'a'는 접미사 없는 기본 슬러그와 헷갈리므로 뺀다.
MAX_TITLE_DEDUPE_ATTEMPTS = 5
SLUG_SUFFIX_LETTERS = "bcdefghijklmnopqrstuvwxyz"
TITLE_SIMILARITY_THRESHOLD = 0.85


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


def month_year_before(yyyymm: str) -> str:
    """1년 전 같은 달 (전년 동월 비교용)."""
    year, month = int(yyyymm[:4]), int(yyyymm[4:])
    return f"{year - 1}{month:02d}"


def recent_topic_ids(limit: int = 12) -> list[str]:
    """content/posts 아래 이미 병합된 글들을 최신순으로 훑어 최근에 쓰인 주제 id를 얻는다.

    같은 주제가 연속으로(혹은 너무 자주) 반복되지 않도록 순서를 정하는 데만 쓰이는
    참고 자료이며, PR이 병합되어야 반영되므로 "실제로 발행된" 이력을 기준으로 한다.
    """
    if not CONTENT_POSTS_DIR.exists():
        return []

    entries: list[tuple[str, str]] = []
    for post_dir in CONTENT_POSTS_DIR.iterdir():
        if not post_dir.is_dir():
            continue
        match = SLUG_RE.match(post_dir.name)
        if not match:
            continue
        index_md = post_dir / "index.md"
        if not index_md.exists():
            continue
        text = index_md.read_text(encoding="utf-8", errors="ignore")
        date_match = DATE_RE.search(text)
        # 날짜를 못 읽으면 slug에 박힌 계약월(YYYYMM)로라도 대략 정렬한다.
        sort_key = date_match.group(1) if date_match else match.group(2)
        entries.append((sort_key, match.group(1)))

    entries.sort(key=lambda pair: pair[0], reverse=True)
    return [topic_id for _, topic_id in entries[:limit]]


def recent_posts_have_promo(limit: int) -> bool:
    """최근 발행된 글 중 최대 limit편 이내에 전자책 홍보 문구가 있었는지 확인한다.

    바로 직전 글(limit편 중 가장 최근 1건)과 최근 limit편 전체를 함께 판단하는
    데 쓰인다. content/posts 아래 실제 파일(=병합되어 실제 발행된 글) 기준이라,
    recent_topic_ids와 마찬가지로 병합 전 PR끼리의 충돌까지는 잡지 못한다.
    """
    if not CONTENT_POSTS_DIR.exists():
        return False

    entries: list[tuple[str, str]] = []
    for post_dir in CONTENT_POSTS_DIR.iterdir():
        if not post_dir.is_dir():
            continue
        if not SLUG_RE.match(post_dir.name):
            continue
        index_md = post_dir / "index.md"
        if not index_md.exists():
            continue
        text = index_md.read_text(encoding="utf-8", errors="ignore")
        date_match = DATE_RE.search(text)
        sort_key = date_match.group(1) if date_match else post_dir.name
        entries.append((sort_key, text))

    entries.sort(key=lambda pair: pair[0], reverse=True)
    return any(PROMO_MARKER in text for _, text in entries[:limit])


def order_topics_by_recency(topics: list[dict]) -> list[dict]:
    """한 번도 안 쓴 주제부터, 그다음 가장 오래전에 쓰인 주제 순으로 시도 순서를 정한다."""
    history = recent_topic_ids(limit=len(topics) * 2)
    last_seen_rank: dict[str, int] = {}
    for rank, topic_id in enumerate(history):  # rank 0 = 가장 최근
        last_seen_rank.setdefault(topic_id, rank)

    never_used = [t for t in topics if t["id"] not in last_seen_rank]
    used = [t for t in topics if t["id"] in last_seen_rank]
    used.sort(key=lambda t: last_seen_rank[t["id"]], reverse=True)  # rank가 클수록(=오래전) 우선

    if never_used:
        # 한 번도 안 쓴 주제끼리는 매번 같은 순서로 고정되지 않도록 날짜 기준으로 시작점을 돌린다.
        shift = datetime.now(KST).toordinal() % len(never_used)
        never_used = never_used[shift:] + never_used[:shift]

    return never_used + used


def existing_month_titles(deal_ymd: str) -> list[tuple[str, str]]:
    """같은 분석월(deal_ymd)로 이미 생성된 글들의 (slug, title) 목록.

    주 3회 실행되는 동안 deal_ymd(지난달)는 한 달 내내 그대로이므로, 6개뿐인 후보
    주제가 순환하다 보면 같은 달에 같은 주제가 다시 뽑혀 제목이 그대로 겹칠 수 있다.
    이 목록은 그 중복을 판별하는 비교 대상이며, 실제 파일시스템(main에 병합된 글)
    기준이라 병합 전 PR끼리의 충돌까지는 잡지 못한다.
    """
    if not CONTENT_POSTS_DIR.exists():
        return []

    results: list[tuple[str, str]] = []
    for post_dir in sorted(CONTENT_POSTS_DIR.iterdir()):
        if not post_dir.is_dir() or not post_dir.name.endswith(f"-{deal_ymd}"):
            continue
        index_md = post_dir / "index.md"
        if not index_md.exists():
            continue
        text = index_md.read_text(encoding="utf-8", errors="ignore")
        title_match = TITLE_RE.search(text)
        if title_match:
            results.append((post_dir.name, title_match.group(1)))
    return results


def _comparable_title(title: str, deal_ymd: str) -> str:
    """비교용으로 다듬은 제목.

    모든 제목이 요구사항 1에 따라 "{year}년 {month}월 서울 아파트 " 접두사를
    공통으로 갖기 때문에, 이 접두사를 포함해 그대로 비교하면 주제가 전혀 달라도
    (예: 전세가 동향 vs 월세 시장 동향) 유사도가 항상 높게 나와 오탐이 난다.
    그래서 접두사를 뗀 "분석 관점" 부분만 비교한다.
    """
    year, month = deal_ymd[:4], int(deal_ymd[4:])
    prefix = f"{year}년 {month}월 {REGION_LABEL} 아파트 "
    core = title[len(prefix):] if title.startswith(prefix) else title
    return re.sub(r"[\s\-()·,]", "", core)


def is_title_too_similar(candidate: str, existing_titles: list[str], deal_ymd: str) -> bool:
    norm_candidate = _comparable_title(candidate, deal_ymd)
    for existing in existing_titles:
        norm_existing = _comparable_title(existing, deal_ymd)
        if norm_candidate == norm_existing:
            return True
        if SequenceMatcher(None, norm_candidate, norm_existing).ratio() >= TITLE_SIMILARITY_THRESHOLD:
            return True
    return False


def dedupe_title_and_slug(
    *,
    topic_id: str,
    base_title: str,
    base_slug: str,
    deal_ymd: str,
    ranked_focus_names: list[str],
) -> tuple[str, str]:
    """이번 달에 이미 겹치거나 너무 비슷한 제목이 있으면 더 구체화해서 다시 만든다.

    ranked_focus_names는 이번 분석에서 실제로 두드러진 항목(구/평형대 등)을 순위대로
    나열한 것이며, 매 시도마다 다음 순위 항목을 제목에 덧붙여 재충돌 가능성을 줄인다.
    슬러그도 같은 시도 순번의 접미사를 붙여 제목과 일관되게 유지한다
    (post/날짜-slug 브랜치 규칙은 그대로 유지됨).
    """
    existing = existing_month_titles(deal_ymd)
    existing_titles = [t for _, t in existing]
    existing_slugs = {s for s, _ in existing}

    title, slug = base_title, base_slug
    attempt = 0
    max_attempts = min(MAX_TITLE_DEDUPE_ATTEMPTS, len(ranked_focus_names), len(SLUG_SUFFIX_LETTERS))
    while (
        is_title_too_similar(title, existing_titles, deal_ymd) or slug in existing_slugs
    ) and attempt < max_attempts:
        focus = ranked_focus_names[attempt]
        title = f"{base_title} ({focus} 중심)"
        slug = f"seoul-apt-{topic_id}{SLUG_SUFFIX_LETTERS[attempt]}-{deal_ymd}"
        attempt += 1

    if is_title_too_similar(title, existing_titles, deal_ymd) or slug in existing_slugs:
        raise TopicSkipped(f"[{topic_id}] 이번 달에 이미 비슷한 글이 있어 구체화를 시도했지만 계속 겹침")

    return title, slug


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


def group_table_markdown(
    group_summary,
    mean_col: str,
    median_col: str,
    unit_divisor: float,
    group_col: str,
    group_header: str,
) -> str:
    lines = [f"| {group_header} | {mean_col} | {median_col} | 거래건수 |", "|---|---:|---:|---:|"]
    for _, row in group_summary.iterrows():
        lines.append(
            f"| {row[group_col]} | {row[mean_col] / unit_divisor:.1f} | "
            f"{row[median_col] / unit_divisor:.1f} | {int(row['거래건수'])} |"
        )
    return "\n".join(lines)


def build_body(
    *,
    title: str,
    topic: dict,
    deal_ymd: str,
    reference_ymd: str,
    compare_label: str,
    group_summary,
    insights: list[str],
    group_col: str,
    group_header: str,
) -> str:
    """본문 마크다운을 만든다.

    표/인사이트 소제목은 그냥 "{group_header}별 인사이트" 식으로 두면, 같은 달에
    같은 그룹 기준(예: 자치구)을 쓰는 다른 주제(전세/전년비교 등)와 소제목이
    그대로 겹친다. 이미 대상+시점+관점을 담고 있고(dedupe_title_and_slug에서
    같은 달 중복까지 걸러진) 글 제목(title)을 소제목 앞에 그대로 붙여, 소제목도
    같은 구체성/유일성을 물려받도록 한다.
    """
    year, month = deal_ymd[:4], int(deal_ymd[4:])
    ref_year, ref_month = reference_ymd[:4], int(reference_ymd[4:])
    mean_col = f"평균 {topic['out_prefix']}"
    median_col = f"중위 {topic['out_prefix']}"
    insight_lines = "\n".join(f"- {line}" for line in insights)
    table_md = group_table_markdown(group_summary, mean_col, median_col, topic["unit_divisor"], group_col, group_header)
    method_note = f"\n- {topic['method_note']}" if topic.get("method_note") else ""

    return f"""
공공데이터포털의 국토교통부 아파트 전월세 실거래자료를 파이썬으로 직접 수집·분석해
{year}년 {month}월 {REGION_LABEL} 아파트 {topic['deal_desc']} 시장 흐름을 정리했습니다.

## 분석 방법

- 데이터 출처: [국토교통부 아파트 전월세 실거래자료]({DATA_SOURCE_URL}) (공공데이터포털 Open API)
- 분석 대상: {REGION_LABEL} 25개 자치구, {year}년 {month}월 신고 기준 {topic['deal_desc']} 계약
- 비교 기준월: {ref_year}년 {ref_month}월 ({compare_label} 대비 증감률 계산용)
- 실거래 신고는 계약 후 30일 이내에 이루어지므로, 최근월 데이터는 이후 계속 소폭 갱신될 수 있습니다.{method_note}

## {title} · {group_header}별 {mean_col}

![{year}년 {month}월 {REGION_LABEL} {group_header}별 {mean_col}](chart.png)

{table_md}

## {title} · {group_header}별 인사이트

{insight_lines}
"""


def write_post(post_dir: Path, title: str, tags: list[str], summary: str, body: str) -> None:
    now = datetime.now(KST)
    front_matter = (
        "---\n"
        f'title: "{title}"\n'
        f"date: {now.strftime('%Y-%m-%dT%H:%M:%S+09:00')}\n"
        "draft: false\n"
        f"tags: {json.dumps(tags, ensure_ascii=False)}\n"
        f'categories: ["{CATEGORY_LABEL}"]\n'
        f'summary: "{summary}"\n'
        "---\n"
    )
    post_dir.mkdir(parents=True, exist_ok=True)
    (post_dir / "index.md").write_text(front_matter + body, encoding="utf-8")


def maybe_add_promo(body: str) -> str:
    if recent_posts_have_promo(PROMO_MIN_GAP):
        return body
    if random.random() < PROMO_PROBABILITY:
        promo = books.promo_for_book_key(BOOKS_FILE, DATASETS[DATASET_KEY]["book_key"])
        if promo:
            return f"{body}\n---\n\n{promo}\n"
    return body


def try_topic(
    topic: dict,
    this_df_all,
    reference_df_all,
    deal_ymd: str,
    reference_ymd: str,
) -> dict:
    """주제 하나를 시도한다. 데이터 부족/본문 부족이면 TopicSkipped를 던진다.

    TopicSkipped 이후(표/차트 생성 단계)에서 나는 예외는 그대로 전파되어
    호출부에서 '기술적 실패'로 처리되고, 다른 주제로 넘어가지 않는다.
    """
    group_col = topic.get("group_col", "구명")
    group_header = topic.get("group_header", group_col if group_col != "구명" else "자치구")
    group_count_desc = topic.get("group_count_desc", "25개 자치구")
    group_noun = topic.get("group_noun", "곳")
    compare_label = topic.get("compare_label", "전월")

    this_df = topic["filter_fn"](this_df_all)
    reference_df = topic["filter_fn"](reference_df_all)

    if len(this_df) < MIN_TRANSACTIONS:
        raise TopicSkipped(
            f"[{topic['id']}] 거래건수 부족: {len(this_df)}건 < 최소 {MIN_TRANSACTIONS}건"
        )

    group_summary = analyze.summarize_by_group(
        this_df, group_col, topic["value_col"], topic["out_prefix"], round_ndigits=topic.get("round_ndigits", 0)
    )
    month_label = f"{deal_ymd[:4]}년 {int(deal_ymd[4:])}월"
    insights = analyze.build_insights(
        this_df,
        reference_df,
        group_summary,
        REGION_LABEL,
        month_label,
        value_col=topic["value_col"],
        out_prefix=topic["out_prefix"],
        unit_divisor=topic["unit_divisor"],
        unit_name=topic["unit_name"],
        deal_desc=topic["deal_desc"],
        exclude_desc=topic["exclude_desc"],
        group_col=group_col,
        group_count_desc=group_count_desc,
        group_noun=group_noun,
        compare_label=compare_label,
    )

    year, month = deal_ymd[:4], int(deal_ymd[4:])
    title = topic["title_template"].format(year=year, month=month, region=REGION_LABEL)
    slug = f"seoul-apt-{topic['id']}-{deal_ymd}"

    if not group_summary.empty:
        ranked_focus_names = [str(name) for name in group_summary[group_col].tolist()]
        title, slug = dedupe_title_and_slug(
            topic_id=topic["id"],
            base_title=title,
            base_slug=slug,
            deal_ymd=deal_ymd,
            ranked_focus_names=ranked_focus_names,
        )

    # ---- 여기부터 표/차트/본문 생성. 이 구간에서 나는 예외는 '기술적 실패'로 취급한다. ----
    body = build_body(
        title=title,
        topic=topic,
        deal_ymd=deal_ymd,
        reference_ymd=reference_ymd,
        compare_label=compare_label,
        group_summary=group_summary,
        insights=insights,
        group_col=group_col,
        group_header=group_header,
    )

    if len(body) < MIN_BODY_CHARS:
        raise TopicSkipped(f"[{topic['id']}] 본문 글자수 부족: {len(body)}자 < 최소 {MIN_BODY_CHARS}자")

    post_dir = CONTENT_POSTS_DIR / slug
    post_dir.mkdir(parents=True, exist_ok=True)
    chart.district_bar_chart(
        group_summary,
        post_dir / "chart.png",
        title=f"{month_label} {REGION_LABEL} {group_col}별 평균 {topic['out_prefix']}",
        mean_col=f"평균 {topic['out_prefix']}",
        unit_divisor=topic["unit_divisor"],
        unit_name=topic["unit_name"],
        group_col=group_col,
    )

    body = maybe_add_promo(body)
    summary = f"{month_label} {REGION_LABEL} 아파트 {topic['deal_desc']} 실거래 데이터 분석"
    write_post(post_dir, title, topic["tags"], summary, body)

    return {
        "title": title,
        "summary": summary,
        "slug": slug,
        "path": f"content/posts/{slug}",
        "data_period": f"{month_label} (비교: {reference_ymd[:4]}년 {int(reference_ymd[4:])}월)",
    }


def try_volumeprice_topic(this_df_all, prev_df_all, deal_ymd: str, prev_ymd: str) -> dict:
    """거래량과 가격 변동률의 관계를 분석한다. 표/인사이트 구조가 달라 전용 함수로 처리한다."""
    vp_df = analyze.build_volume_price_table(this_df_all, prev_df_all)
    if len(vp_df) < 5:
        raise TopicSkipped(f"[{VOLUME_PRICE_TOPIC_ID}] 비교 가능한 자치구 수 부족: {len(vp_df)}개 < 최소 5개")

    total_contracts = int(vp_df["거래건수"].sum())
    if total_contracts < MIN_TRANSACTIONS:
        raise TopicSkipped(f"[{VOLUME_PRICE_TOPIC_ID}] 거래건수 부족: {total_contracts}건 < 최소 {MIN_TRANSACTIONS}건")

    corr = analyze.transaction_price_correlation(vp_df)
    year, month = deal_ymd[:4], int(deal_ymd[4:])
    month_label = f"{year}년 {month}월"
    title = VOLUME_PRICE_TITLE_TEMPLATE.format(year=year, month=month, region=REGION_LABEL)
    slug = f"seoul-apt-{VOLUME_PRICE_TOPIC_ID}-{deal_ymd}"

    ranked_focus_names = [str(name) for name in vp_df["구명"].tolist()]
    title, slug = dedupe_title_and_slug(
        topic_id=VOLUME_PRICE_TOPIC_ID,
        base_title=title,
        base_slug=slug,
        deal_ymd=deal_ymd,
        ranked_focus_names=ranked_focus_names,
    )

    most_traded = vp_df.iloc[0]
    biggest_gain = vp_df.sort_values("가격변동률", ascending=False).iloc[0]
    biggest_drop = vp_df.sort_values("가격변동률", ascending=True).iloc[0]

    if corr is None:
        corr_desc = "표본 자치구 수가 적어 상관계수는 참고용으로만 확인해주세요."
    else:
        strength = "뚜렷한" if abs(corr) >= 0.5 else ("약한" if abs(corr) >= 0.2 else "거의 없는")
        direction = (
            "비례(거래가 많을수록 가격도 더 올랐음)"
            if corr > 0
            else "반비례(거래가 많을수록 가격이 더 내렸음)"
        )
        corr_desc = f"거래건수와 가격 변동률의 상관계수는 {corr:.2f}로, {strength} {direction} 관계를 보였습니다."

    insights = [
        f"{month_label} {REGION_LABEL} 25개 자치구 중 순수 전세 거래가 가장 활발한 곳은 "
        f"{most_traded['구명']}({int(most_traded['거래건수'])}건)이었습니다.",
        f"전월 대비 평균 전세보증금이 가장 많이 오른 곳은 {biggest_gain['구명']}"
        f"({biggest_gain['가격변동률']:+.1f}%), 가장 많이 내린 곳은 {biggest_drop['구명']}"
        f"({biggest_drop['가격변동률']:+.1f}%)이었습니다.",
        corr_desc,
        "상관관계는 인과관계를 의미하지 않으며, 표본 수가 적은 자치구는 결과가 왜곡될 수 있습니다.",
    ]

    table_lines = ["| 자치구 | 거래건수 | 가격변동률(전월 대비) |", "|---|---:|---:|"]
    for _, row in vp_df.iterrows():
        table_lines.append(f"| {row['구명']} | {int(row['거래건수'])} | {row['가격변동률']:+.1f}% |")
    table_md = "\n".join(table_lines)
    insight_lines = "\n".join(f"- {line}" for line in insights)

    body = f"""
공공데이터포털의 국토교통부 아파트 전월세 실거래자료를 파이썬으로 직접 수집·분석해
{year}년 {month}월 {REGION_LABEL} 아파트 순수 전세 거래량과 가격 변동률 사이의 관계를 살펴봤습니다.

## 분석 방법

- 데이터 출처: [국토교통부 아파트 전월세 실거래자료]({DATA_SOURCE_URL}) (공공데이터포털 Open API)
- 분석 대상: {REGION_LABEL} 25개 자치구, {year}년 {month}월 신고 기준 순수 전세 계약
- 비교 기준월: {prev_ymd[:4]}년 {int(prev_ymd[4:])}월 (전월 대비 가격 변동률 계산용)
- 가격 변동률은 자치구별 평균 전세보증금 기준이며, 거래건수는 순수 전세 계약 건수 기준입니다.

## {title} · 자치구별 거래건수와 가격 변동률

![{year}년 {month}월 {REGION_LABEL} 자치구별 순수 전세 거래건수](chart.png)

{table_md}

## {title} · 자치구별 인사이트

{insight_lines}
"""

    if len(body) < MIN_BODY_CHARS:
        raise TopicSkipped(f"[{VOLUME_PRICE_TOPIC_ID}] 본문 글자수 부족: {len(body)}자 < 최소 {MIN_BODY_CHARS}자")

    post_dir = CONTENT_POSTS_DIR / slug
    post_dir.mkdir(parents=True, exist_ok=True)
    chart.district_bar_chart(
        vp_df,
        post_dir / "chart.png",
        title=f"{month_label} {REGION_LABEL} 자치구별 순수 전세 거래건수",
        mean_col="거래건수",
        unit_divisor=1.0,
        unit_name="건",
    )

    body = maybe_add_promo(body)
    summary = f"{month_label} {REGION_LABEL} 아파트 순수 전세 거래량-가격 변동 관계 분석"
    write_post(post_dir, title, VOLUME_PRICE_TAGS, summary, body)

    return {
        "title": title,
        "summary": summary,
        "slug": slug,
        "path": f"content/posts/{slug}",
        "data_period": f"{month_label} (비교: {prev_ymd[:4]}년 {int(prev_ymd[4:])}월)",
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

    frames_cache: dict[str, object] = {}

    def get_df_all(ymd: str):
        if ymd not in frames_cache:
            print(f"  ({ymd} {REGION_LABEL} {dataset['label']} 데이터 수집 중...)")
            rows = fetch_month_rows(dataset["operation"], ymd, api_key, args.mock)
            frames_cache[ymd] = analyze.to_dataframe(rows)
        return frames_cache[ymd]

    print(f"[1/4] {deal_ymd} / {prev_ymd} {REGION_LABEL} {dataset['label']} 데이터 수집 중...")
    this_df_all = get_df_all(deal_ymd)
    prev_df_all = get_df_all(prev_ymd)  # 대부분의 후보 주제가 전월 데이터를 필요로 함
    print(f"[2/4] 데이터 정제 완료 (이번달 {len(this_df_all)}건, 전월 {len(prev_df_all)}건)")

    ordered_topics = order_topics_by_recency(CANDIDATE_TOPICS)
    print(f"[3/4] 후보 주제 시도 순서: {[t['id'] for t in ordered_topics]}")

    skip_reasons: list[str] = []
    for topic in ordered_topics:
        try:
            if topic["id"] == VOLUME_PRICE_TOPIC_ID:
                result = try_volumeprice_topic(this_df_all, prev_df_all, deal_ymd, prev_ymd)
            else:
                if topic.get("compare") == "yoy":
                    reference_ymd = month_year_before(deal_ymd)
                else:
                    reference_ymd = prev_ymd
                reference_df_all = get_df_all(reference_ymd)
                result = try_topic(topic, this_df_all, reference_df_all, deal_ymd, reference_ymd)
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
