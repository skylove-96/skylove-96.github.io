"""전국 지역별 아파트 전월세 실거래 데이터로 블로그 글을 자동 생성한다.

주 3회(월/수/금) 실행을 상정하며, 아래 순서로 동작한다:
  0. 이번 주(월요일 기준)에 다룰 지역을 REGION_ROTATION 순서대로 고름 (--region으로 강제 지정 가능)
  1. 이번 달/전월 실거래 데이터를 수집하면서 지역 코드 건강 점검 (모든 코드 0건이거나 폐지된
     코드가 있으면 "코드 이상" → 확실한 새 코드면 regions.json에 반영, 아니면 로테이션 다음
     지역으로 대체하고 region_health_report.md를 남겨 CI가 Issue로 알림)
     전년 동월 등 행정구역 개편 이전 달은 regions.json의 code_history 매핑으로 옛 코드를 조회
  2. 후보 주제(전세/월세/전환율/평형대/전년비교/거래량-가격)를 최근 발행 이력 기준으로
     "오래 쓰이지 않은 주제부터" 순서를 바꿔가며 시도
  3. 거래건수가 너무 적거나(MIN_TRANSACTIONS) 본문이 너무 짧으면(MIN_BODY_CHARS)
     해당 주제를 건너뛰고 다음 후보로 넘어감
  4. 표/차트 생성 자체가 실패하면(코드/환경 문제) 다른 주제로 넘어가지 않고 즉시 중단
  5. 성공하면 stdout에 `::post-meta::{...}` 한 줄을 출력해 CI가 PR 정보를 읽어가게 함
  6. 공공데이터 API 호출이 끝내 실패하면 종료 코드 3으로 끝냄 (CI가 잠시 후 전체 재시도)

종료 코드: 0 = 글 생성 또는 "발행할 주제 없음", 1 = 설정/코드 오류, 3 = 공공데이터 API 오류
          (모든 지역이 코드 이상이면 0으로 끝내고 "발행할 주제 없음"과 같은 경로로 Issue를 남긴다)

사용 예:
    python generate_post.py                 # 지난달 데이터, 실제 API, 이번 주 로테이션 지역 자동 선택
    python generate_post.py --month 202608  # 특정 월 지정
    python generate_post.py --region gyeonggi  # 로테이션 대신 특정 지역 강제 지정
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
    REGION_ROTATION,
    REGIONS,
)
from src import analyze, books, chart, lawd_codes, molit_api  # noqa: E402
from src import regions as region_cfg  # noqa: E402
from src.molit_api import ApiKey  # noqa: E402
from src.regions import MappingUncertain  # noqa: E402

KST = ZoneInfo("Asia/Seoul")
BOOKS_FILE = PIPELINE_DIR / "books.json"
CONTENT_POSTS_DIR = PROJECT_ROOT / "content" / "posts"
DATA_SOURCE_URL = "https://www.data.go.kr/data/15126474/openapi.do"
DATASET_KEY = "apt_rent"  # 모든 후보 주제가 같은 API(apt_rent)에서 필터/가공만 다르게 쓴다.
# 지역 코드 이상을 발견했을 때 CI(generate-post.yml)가 Issue 본문으로 쓰는 파일.
HEALTH_REPORT_FILE = PROJECT_ROOT / "region_health_report.md"

# 로테이션 시작점(월요일). 이 날짜로부터 몇 주가 지났는지로 이번 주 지역을 정하기 때문에,
# 이 값 자체가 바뀌어도(과거로 옮겨도) 로테이션 "순서"는 바뀌지 않고 위상만 바뀐다.
# 2024-01-08로 맞춘 이유: 서울만 다루던 마지막 주(2026-09-14 주)가 REGION_ROTATION의
# 첫 항목("seoul")과 위상이 맞도록 해서, 그다음 주(2026-09-21 주)부터 "gyeonggi"로
# 자연스럽게 이어지게 하기 위함.
ROTATION_EPOCH = datetime(2024, 1, 8, tzinfo=ZoneInfo("Asia/Seoul"))


def active_region_rotation() -> list[str]:
    """districts가 아직 채워지지 않은(TODO) 지역은 로테이션에서 제외한다."""
    active = [key for key in REGION_ROTATION if REGIONS[key].get("districts")]
    if not active:
        raise RuntimeError("REGIONS에 districts가 채워진 지역이 하나도 없습니다.")
    return active


def pick_region_for_week(today: datetime | None = None) -> str:
    """이번 주(월요일 기준)에 다룰 지역을 로테이션 순서대로 고른다.

    같은 주의 월/수/금 실행이 모두 같은 지역을 다루도록, ISO 주차가 아니라
    ROTATION_EPOCH로부터 지난 '주 수'를 기준으로 삼는다 (연도가 바뀌어도 순서가 끊기지 않음).
    """
    today = today or datetime.now(KST)
    weeks_elapsed = (today.date() - ROTATION_EPOCH.date()).days // 7
    rotation = active_region_rotation()
    return rotation[weeks_elapsed % len(rotation)]


def fallback_order(first: str) -> list[str]:
    """first부터 시작해 로테이션 순서대로 돈다 (코드 이상 지역을 건너뛸 때의 대체 순서)."""
    rotation = active_region_rotation()
    if first not in rotation:
        return [first] + rotation
    i = rotation.index(first)
    return rotation[i:] + rotation[:i]

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
        "heading": "전세가 동향 인사이트",
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
        "heading": "월세 시장 인사이트",
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
        "heading": "전월세 전환율 인사이트",
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
        "heading": "평형대별 전세가 인사이트",
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
        "heading": "전세가 1년 전 대비 인사이트",
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

SLUG_RE = re.compile(r"^([a-z]+)-apt-([a-z]+)-(\d{6})$")
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
        sort_key = date_match.group(1) if date_match else match.group(3)
        entries.append((sort_key, match.group(2)))

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


def existing_month_titles(region_key: str, deal_ymd: str) -> list[tuple[str, str]]:
    """같은 지역·같은 분석월(deal_ymd)로 이미 생성된 글들의 (slug, title) 목록.

    주 3회 실행되는 동안 deal_ymd(지난달)는 한 달 내내 그대로이므로, 6개뿐인 후보
    주제가 순환하다 보면 같은 지역·같은 달에 같은 주제가 다시 뽑혀 제목이 그대로
    겹칠 수 있다. 이 목록은 그 중복을 판별하는 비교 대상이며, 실제 파일시스템
    (main에 병합된 글) 기준이라 병합 전 PR끼리의 충돌까지는 잡지 못한다.
    """
    if not CONTENT_POSTS_DIR.exists():
        return []

    prefix, suffix = f"{region_key}-apt-", f"-{deal_ymd}"
    results: list[tuple[str, str]] = []
    for post_dir in sorted(CONTENT_POSTS_DIR.iterdir()):
        if not post_dir.is_dir() or not post_dir.name.startswith(prefix) or not post_dir.name.endswith(suffix):
            continue
        index_md = post_dir / "index.md"
        if not index_md.exists():
            continue
        text = index_md.read_text(encoding="utf-8", errors="ignore")
        title_match = TITLE_RE.search(text)
        if title_match:
            results.append((post_dir.name, title_match.group(1)))
    return results


def _comparable_title(title: str, region_label: str, deal_ymd: str) -> str:
    """비교용으로 다듬은 제목.

    모든 제목이 요구사항 1에 따라 "{year}년 {month}월 {지역명} 아파트 " 접두사를
    공통으로 갖기 때문에, 이 접두사를 포함해 그대로 비교하면 주제가 전혀 달라도
    (예: 전세가 동향 vs 월세 시장 동향) 유사도가 항상 높게 나와 오탐이 난다.
    그래서 접두사를 뗀 "분석 관점" 부분만 비교한다.
    """
    year, month = deal_ymd[:4], int(deal_ymd[4:])
    prefix = f"{year}년 {month}월 {region_label} 아파트 "
    core = title[len(prefix):] if title.startswith(prefix) else title
    return re.sub(r"[\s\-()·,]", "", core)


def is_title_too_similar(
    candidate: str, existing_titles: list[str], region_label: str, deal_ymd: str
) -> bool:
    norm_candidate = _comparable_title(candidate, region_label, deal_ymd)
    for existing in existing_titles:
        norm_existing = _comparable_title(existing, region_label, deal_ymd)
        if norm_candidate == norm_existing:
            return True
        if SequenceMatcher(None, norm_candidate, norm_existing).ratio() >= TITLE_SIMILARITY_THRESHOLD:
            return True
    return False


def dedupe_title_and_slug(
    *,
    region_key: str,
    region_label: str,
    topic_id: str,
    base_title: str,
    base_slug: str,
    deal_ymd: str,
    ranked_focus_names: list[str],
) -> tuple[str, str]:
    """이번 지역·이번 달에 이미 겹치거나 너무 비슷한 제목이 있으면 더 구체화해서 다시 만든다.

    ranked_focus_names는 이번 분석에서 실제로 두드러진 항목(구/평형대 등)을 순위대로
    나열한 것이며, 매 시도마다 다음 순위 항목을 제목에 덧붙여 재충돌 가능성을 줄인다.
    슬러그도 같은 시도 순번의 접미사를 붙여 제목과 일관되게 유지한다
    (post/날짜-slug 브랜치 규칙은 그대로 유지됨).
    """
    existing = existing_month_titles(region_key, deal_ymd)
    existing_titles = [t for _, t in existing]
    existing_slugs = {s for s, _ in existing}

    title, slug = base_title, base_slug
    attempt = 0
    max_attempts = min(MAX_TITLE_DEDUPE_ATTEMPTS, len(ranked_focus_names), len(SLUG_SUFFIX_LETTERS))
    while (
        is_title_too_similar(title, existing_titles, region_label, deal_ymd) or slug in existing_slugs
    ) and attempt < max_attempts:
        focus = ranked_focus_names[attempt]
        title = f"{base_title} ({focus} 중심)"
        slug = f"{region_key}-apt-{topic_id}{SLUG_SUFFIX_LETTERS[attempt]}-{deal_ymd}"
        attempt += 1

    if is_title_too_similar(title, existing_titles, region_label, deal_ymd) or slug in existing_slugs:
        raise TopicSkipped(f"[{topic_id}] 이번 지역·달에 이미 비슷한 글이 있어 구체화를 시도했지만 계속 겹침")

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


def fetch_month_rows(
    operation: str, deal_ymd: str, api_key: ApiKey, mock: bool, region: dict
) -> tuple[list[dict], dict[str, int], list[str]]:
    """한 달치 행, 코드별 건수, 조회 방식 메모를 돌려준다.

    행정구역 개편 이전 달이면 regions.json의 매핑대로 옛 코드로 조회해 새 시/군/구 이름으로 담는다.
    매핑이 불확실하면 MappingUncertain이 그대로 올라가 호출부(주제)에서 건너뛴다.
    """
    plan = region_cfg.fetch_plan(region, deal_ymd)
    if mock:
        rows = make_mock_rows(deal_ymd, [d["name"] for d in region["districts"]])
        counts = {d["lawd_cd"]: sum(1 for r in rows if r["구명"] == d["name"]) for d in region["districts"]}
        return rows, counts, plan.notes
    raw_rows, counts = molit_api.fetch_region_month_with_counts(
        operation, plan.direct, deal_ymd, api_key, remap=plan.remap, assign=region_cfg.assign_district
    )
    return [molit_api.normalize_rent_row(row) for row in raw_rows], counts, plan.notes


# 그룹별 증감을 표/인사이트에 쓸 때, 두 달 중 한쪽이라도 거래가 이보다 적으면
# 몇 건의 계약만으로 증감률이 크게 튀므로 "-"로 표시하고 인사이트 비교에서도 뺀다.
MIN_GROUP_COMPARE_COUNT = 10

DATA_REVISION_NOTE = (
    "데이터 수집 기준일: {date}. 이후 늦게 신고된 계약이 반영되면 같은 달을 다룬 "
    "다른 글과 수치가 조금 다를 수 있습니다."
)


def collected_on() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d")


def josa_gwa_wa(word: str) -> str:
    return "과" if analyze._has_batchim(word) else "와"


def format_change(this_value: float, ref_value: float, unit_name: str) -> str:
    """증감을 표기한다. 비율(%) 지표는 증감률의 증감률이 되지 않도록 %p 차이로 쓴다."""
    if unit_name == "%":
        return f"{this_value - ref_value:+.2f}%p"
    return f"{(this_value - ref_value) / ref_value * 100:+.1f}%"


def add_group_change(group_summary, reference_summary, group_col: str, mean_col: str):
    """group_summary에 비교 기준월의 평균/거래건수를 붙인다 (기준월에 없는 그룹은 NaN)."""
    ref = reference_summary[[group_col, mean_col, "거래건수"]].rename(
        columns={mean_col: "기준 평균", "거래건수": "기준 거래건수"}
    )
    return group_summary.merge(ref, on=group_col, how="left")


def is_comparable(row) -> bool:
    ref_count = row["기준 거래건수"]
    return (
        row["거래건수"] >= MIN_GROUP_COMPARE_COUNT
        and ref_count == ref_count  # NaN 제외
        and ref_count >= MIN_GROUP_COMPARE_COUNT
        and row["기준 평균"] > 0
    )


def group_change_insight(
    group_summary, group_col: str, mean_col: str, unit_name: str, compare_label: str, group_noun: str
) -> str | None:
    if group_summary.empty:
        return None
    comparable = group_summary[group_summary.apply(is_comparable, axis=1)]
    if len(comparable) < 2:
        return None
    if unit_name == "%":
        change = comparable[mean_col] - comparable["기준 평균"]
    else:
        change = (comparable[mean_col] - comparable["기준 평균"]) / comparable["기준 평균"]
    top, bottom = comparable.loc[change.idxmax()], comparable.loc[change.idxmin()]
    top_desc = "가장 많이 오른" if change.max() > 0 else "가장 적게 내린"
    bottom_desc = "가장 많이 내린" if change.min() < 0 else "가장 적게 오른"
    return (
        f"{compare_label} 대비 평균이 {top_desc} {group_noun}은 {top[group_col]}"
        f"({format_change(top[mean_col], top['기준 평균'], unit_name)}), {bottom_desc} {group_noun}은 "
        f"{bottom[group_col]}({format_change(bottom[mean_col], bottom['기준 평균'], unit_name)})이었습니다 "
        f"(두 달 모두 {MIN_GROUP_COMPARE_COUNT}건 이상 거래된 {group_noun}만 비교)."
    )


def group_table_markdown(
    group_summary,
    mean_col: str,
    median_col: str,
    unit_divisor: float,
    unit_name: str,
    group_col: str,
    group_header: str,
    compare_label: str,
) -> str:
    """단위를 헤더에 적고, 비교 기준월 대비 평균 증감 열을 함께 보여준다."""
    lines = [
        f"| {group_header} | {mean_col}({unit_name}) | {median_col}({unit_name}) | {compare_label} 대비 | 거래건수 |",
        "|---|---:|---:|---:|---:|",
    ]
    for _, row in group_summary.iterrows():
        change = format_change(row[mean_col], row["기준 평균"], unit_name) if is_comparable(row) else "-"
        lines.append(
            f"| {row[group_col]} | {row[mean_col] / unit_divisor:.1f} | "
            f"{row[median_col] / unit_divisor:.1f} | {change} | {int(row['거래건수']):,} |"
        )
    return "\n".join(lines)


def build_body(
    *,
    title: str,
    topic: dict,
    region_label: str,
    group_count_desc: str,
    deal_ymd: str,
    reference_ymd: str,
    compare_label: str,
    group_summary,
    insights: list[str],
    group_col: str,
    group_header: str,
) -> str:
    """본문 마크다운을 만든다.

    소제목에 글 제목 전체를 붙이면 너무 길어지므로, 지역·그룹·지표·비교 기준
    (전월/전년 동월)과 주제별 heading만 넣는다. 같은 그룹 기준을 쓰는 주제끼리도
    (예: 전세 vs 전년비교) 비교 기준이나 heading이 달라 소제목이 겹치지 않는다.
    """
    year, month = deal_ymd[:4], int(deal_ymd[4:])
    ref_year, ref_month = reference_ymd[:4], int(reference_ymd[4:])
    mean_col = f"평균 {topic['out_prefix']}"
    median_col = f"중위 {topic['out_prefix']}"
    insight_lines = "\n".join(f"- {line}" for line in insights)
    table_md = group_table_markdown(
        group_summary, mean_col, median_col, topic["unit_divisor"], topic["unit_name"], group_col, group_header, compare_label
    )
    method_note = f"\n- {topic['method_note']}" if topic.get("method_note") else ""

    return f"""
공공데이터포털의 국토교통부 아파트 전월세 실거래자료를 파이썬으로 직접 수집·분석해
{year}년 {month}월 {region_label} 아파트 {topic['deal_desc']} 시장 흐름을 정리했습니다.

## 분석 방법

- 데이터 출처: [국토교통부 아파트 전월세 실거래자료]({DATA_SOURCE_URL}) (공공데이터포털 Open API)
- 분석 대상: {region_label} {group_count_desc}, {year}년 {month}월 신고 기준 {topic['deal_desc']} 계약
- 비교 기준월: {ref_year}년 {ref_month}월 ({compare_label} 대비 증감률 계산용)
- 실거래 신고는 계약 후 30일 이내에 이루어지므로, 최근월 데이터는 이후 계속 소폭 갱신될 수 있습니다.
- {DATA_REVISION_NOTE.format(date=collected_on())}{method_note}

## {region_label} {group_header}별 {mean_col}{josa_gwa_wa(mean_col)} {compare_label} 대비 변화

![{year}년 {month}월 {region_label} {group_header}별 {mean_col}](chart.png)

{table_md}

## {region_label} {month}월 {topic['heading']}

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
    region_key: str,
    region_label: str,
    region_group_label: str,
    this_df_all,
    reference_df_all,
    deal_ymd: str,
    reference_ymd: str,
    fetch_notes: list[str] | None = None,
) -> dict:
    """주제 하나를 시도한다. 데이터 부족/본문 부족이면 TopicSkipped를 던진다.

    TopicSkipped 이후(표/차트 생성 단계)에서 나는 예외는 그대로 전파되어
    호출부에서 '기술적 실패'로 처리되고, 다른 주제로 넘어가지 않는다.
    """
    group_col = topic.get("group_col", "구명")
    group_header = topic.get("group_header", group_col if group_col != "구명" else region_group_label)
    group_count_desc = topic.get("group_count_desc", f"{len(REGIONS[region_key]['districts'])}개 {region_group_label}")
    group_noun = topic.get("group_noun", "곳")
    compare_label = topic.get("compare_label", "전월")

    this_df = topic["filter_fn"](this_df_all)
    reference_df = topic["filter_fn"](reference_df_all)

    if len(this_df) < MIN_TRANSACTIONS:
        raise TopicSkipped(
            f"[{topic['id']}] 거래건수 부족: {len(this_df)}건 < 최소 {MIN_TRANSACTIONS}건"
        )

    mean_col = f"평균 {topic['out_prefix']}"
    round_ndigits = topic.get("round_ndigits", 0)
    group_summary = analyze.summarize_by_group(this_df, group_col, topic["value_col"], topic["out_prefix"], round_ndigits)
    reference_summary = analyze.summarize_by_group(
        reference_df, group_col, topic["value_col"], topic["out_prefix"], round_ndigits
    )
    group_summary = add_group_change(group_summary, reference_summary, group_col, mean_col)
    month_label = f"{deal_ymd[:4]}년 {int(deal_ymd[4:])}월"
    insights = analyze.build_insights(
        this_df,
        reference_df,
        group_summary,
        region_label,
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
    change_line = group_change_insight(group_summary, group_col, mean_col, topic["unit_name"], compare_label, group_noun)
    if change_line:
        insights.insert(len(insights) - 1, change_line)  # 마지막 "총 N건" 문장 앞에 넣는다

    year, month = deal_ymd[:4], int(deal_ymd[4:])
    title = topic["title_template"].format(year=year, month=month, region=region_label)
    slug = f"{region_key}-apt-{topic['id']}-{deal_ymd}"

    if not group_summary.empty:
        ranked_focus_names = [str(name) for name in group_summary[group_col].tolist()]
        title, slug = dedupe_title_and_slug(
            region_key=region_key,
            region_label=region_label,
            topic_id=topic["id"],
            base_title=title,
            base_slug=slug,
            deal_ymd=deal_ymd,
            ranked_focus_names=ranked_focus_names,
        )

    # ---- 여기부터 표/차트/본문 생성. 이 구간에서 나는 예외는 '기술적 실패'로 취급한다. ----
    if fetch_notes:  # 비교 기준월이 행정구역 개편 이전 달이면 어떻게 조회했는지 밝힌다.
        notes = ([topic["method_note"]] if topic.get("method_note") else []) + fetch_notes
        topic = {**topic, "method_note": "\n- ".join(notes)}
    body = build_body(
        title=title,
        topic=topic,
        region_label=region_label,
        group_count_desc=group_count_desc,
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
        title=f"{month_label} {region_label} {group_header}별 평균 {topic['out_prefix']}",
        mean_col=f"평균 {topic['out_prefix']}",
        unit_divisor=topic["unit_divisor"],
        unit_name=topic["unit_name"],
        group_col=group_col,
    )

    body = maybe_add_promo(body)
    # summary는 목록/검색 결과에서 같은 달·같은 지역 글끼리 구분되도록 이 글의 핵심 수치를 담는다.
    unit_name, unit_divisor = topic["unit_name"], topic["unit_divisor"]
    this_avg = this_df[topic["value_col"]].mean()
    ref_avg = reference_df[topic["value_col"]].mean() if not reference_df.empty else 0
    summary = (
        f"{month_label} {region_label} 아파트 {topic['deal_desc']} {len(this_df):,}건 분석: "
        f"평균 {this_avg / unit_divisor:.1f}{unit_name}"
    )
    if ref_avg:
        summary += f"({compare_label} 대비 {format_change(this_avg, ref_avg, unit_name)})"
    if not group_summary.empty:
        top = group_summary.iloc[0]
        summary += f", {group_header}별 최고는 {top[group_col]}({top[mean_col] / unit_divisor:.1f}{unit_name})"
    write_post(post_dir, title, topic["tags"], summary, body)

    return {
        "title": title,
        "summary": summary,
        "slug": slug,
        "path": f"content/posts/{slug}",
        "data_period": f"{month_label} (비교: {reference_ymd[:4]}년 {int(reference_ymd[4:])}월)",
    }


def try_volumeprice_topic(
    region_key: str,
    region_label: str,
    region_group_label: str,
    this_df_all,
    prev_df_all,
    deal_ymd: str,
    prev_ymd: str,
) -> dict:
    """거래량과 가격 변동률의 관계를 분석한다. 표/인사이트 구조가 달라 전용 함수로 처리한다."""
    vp_df = analyze.build_volume_price_table(this_df_all, prev_df_all)
    if len(vp_df) < 5:
        raise TopicSkipped(f"[{VOLUME_PRICE_TOPIC_ID}] 비교 가능한 {region_group_label} 수 부족: {len(vp_df)}개 < 최소 5개")

    total_contracts = int(vp_df["거래건수"].sum())
    if total_contracts < MIN_TRANSACTIONS:
        raise TopicSkipped(f"[{VOLUME_PRICE_TOPIC_ID}] 거래건수 부족: {total_contracts}건 < 최소 {MIN_TRANSACTIONS}건")

    corr = analyze.transaction_price_correlation(vp_df)
    year, month = deal_ymd[:4], int(deal_ymd[4:])
    month_label = f"{year}년 {month}월"
    title = VOLUME_PRICE_TITLE_TEMPLATE.format(year=year, month=month, region=region_label)
    slug = f"{region_key}-apt-{VOLUME_PRICE_TOPIC_ID}-{deal_ymd}"

    ranked_focus_names = [str(name) for name in vp_df["구명"].tolist()]
    title, slug = dedupe_title_and_slug(
        region_key=region_key,
        region_label=region_label,
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
        corr_desc = f"표본 {region_group_label} 수가 적어 상관계수는 참고용으로만 확인해주세요."
    else:
        strength = "뚜렷한" if abs(corr) >= 0.5 else ("약한" if abs(corr) >= 0.2 else "거의 없는")
        direction = (
            "비례(거래가 많을수록 가격도 더 올랐음)"
            if corr > 0
            else "반비례(거래가 많을수록 가격이 더 내렸음)"
        )
        corr_desc = f"거래건수와 가격 변동률의 상관계수는 {corr:.2f}로, {strength} {direction} 관계를 보였습니다."

    group_count_desc = f"{len(REGIONS[region_key]['districts'])}개 {region_group_label}"
    insights = [
        f"{month_label} {region_label} {group_count_desc} 중 순수 전세 거래가 가장 활발한 곳은 "
        f"{most_traded['구명']}({int(most_traded['거래건수'])}건)이었습니다.",
        f"전월 대비 평균 전세보증금이 가장 많이 오른 곳은 {biggest_gain['구명']}"
        f"({biggest_gain['가격변동률']:+.1f}%), 가장 많이 내린 곳은 {biggest_drop['구명']}"
        f"({biggest_drop['가격변동률']:+.1f}%)이었습니다.",
        corr_desc,
        f"상관관계는 인과관계를 의미하지 않으며, 표본 수가 적은 {region_group_label}은 결과가 왜곡될 수 있습니다.",
    ]

    table_lines = [f"| {region_group_label} | 거래건수 | 가격변동률(전월 대비) |", "|---|---:|---:|"]
    for _, row in vp_df.iterrows():
        table_lines.append(f"| {row['구명']} | {int(row['거래건수']):,} | {row['가격변동률']:+.1f}% |")
    table_md = "\n".join(table_lines)
    insight_lines = "\n".join(f"- {line}" for line in insights)

    body = f"""
공공데이터포털의 국토교통부 아파트 전월세 실거래자료를 파이썬으로 직접 수집·분석해
{year}년 {month}월 {region_label} 아파트 순수 전세 거래량과 가격 변동률 사이의 관계를 살펴봤습니다.

## 분석 방법

- 데이터 출처: [국토교통부 아파트 전월세 실거래자료]({DATA_SOURCE_URL}) (공공데이터포털 Open API)
- 분석 대상: {region_label} {group_count_desc}, {year}년 {month}월 신고 기준 순수 전세 계약
- 비교 기준월: {prev_ymd[:4]}년 {int(prev_ymd[4:])}월 (전월 대비 가격 변동률 계산용)
- 가격 변동률은 {region_group_label}별 평균 전세보증금 기준이며, 거래건수는 순수 전세 계약 건수 기준입니다.
- {DATA_REVISION_NOTE.format(date=collected_on())}

## {region_label} {region_group_label}별 거래건수와 가격 변동률

![{year}년 {month}월 {region_label} {region_group_label}별 순수 전세 거래건수](chart.png)

{table_md}

## {region_label} {month}월 전세 거래량-가격 인사이트

{insight_lines}
"""

    if len(body) < MIN_BODY_CHARS:
        raise TopicSkipped(f"[{VOLUME_PRICE_TOPIC_ID}] 본문 글자수 부족: {len(body)}자 < 최소 {MIN_BODY_CHARS}자")

    post_dir = CONTENT_POSTS_DIR / slug
    post_dir.mkdir(parents=True, exist_ok=True)
    chart.district_bar_chart(
        vp_df,
        post_dir / "chart.png",
        title=f"{month_label} {region_label} {region_group_label}별 순수 전세 거래건수",
        mean_col="거래건수",
        unit_divisor=1.0,
        unit_name="건",
    )

    body = maybe_add_promo(body)
    corr_summary = f", 상관계수 {corr:.2f}" if corr is not None else ""
    summary = (
        f"{month_label} {region_label} 아파트 순수 전세 {total_contracts:,}건으로 본 거래량-가격 관계: "
        f"거래 최다 {most_traded['구명']}({int(most_traded['거래건수']):,}건), "
        f"최대 상승 {biggest_gain['구명']}({biggest_gain['가격변동률']:+.1f}%){corr_summary}"
    )
    write_post(post_dir, title, VOLUME_PRICE_TAGS, summary, body)

    return {
        "title": title,
        "summary": summary,
        "slug": slug,
        "path": f"content/posts/{slug}",
        "data_period": f"{month_label} (비교: {prev_ymd[:4]}년 {int(prev_ymd[4:])}월)",
    }


EXIT_API_ERROR = 3  # CI(generate-post.yml)가 이 코드일 때만 잠시 기다렸다가 다시 시도한다.


def probe_past_months_query(region: dict, applied: list[dict], probe_ymd: str, api_key: ApiKey, dataset: dict) -> None:
    """자동 반영한 개편에 대해, 개편 이전 달(probe_ymd)을 API가 어느 코드로 주는지 실제 호출로 확인한다.

    새 코드로 건수가 나오면 new_codes, 옛 코드로만 나오면 old_codes. 둘 다 0건이면 값을 남기지 않아
    그 달과 비교하는 주제(전년 동월 등)는 건너뛰게 된다.
    """
    for change in region.get("code_history", []):
        if change.get("past_months_query") or not any(
            m["new_lawd_cd"] in {c["new_lawd_cd"] for c in applied} for m in change["mapping"]
        ):
            continue
        if probe_ymd >= change["effective_date"].replace("-", "")[:6]:
            continue
        new_total = sum(molit_api.count_district_month(dataset["operation"], m["new_lawd_cd"], probe_ymd, api_key)[0]
                        for m in change["mapping"])
        old_total = sum(molit_api.count_district_month(dataset["operation"], m["old_lawd_cd"], probe_ymd, api_key)[0]
                        for m in change["mapping"])
        change["api_verified_on"] = datetime.now(KST).strftime("%Y-%m-%d")
        change["evidence"].append(f"{probe_ymd} 호출: 새 코드 합계 {new_total:,}건, 옛 코드 합계 {old_total:,}건")
        if new_total > 0 and old_total == 0:
            change["past_months_query"] = "new_codes"
        elif old_total > 0 and new_total == 0:
            change["past_months_query"] = "old_codes"
        print(f"  개편 이전 달({probe_ymd}) 조회 방식: {change.get('past_months_query', '미확인 — 비교 주제 건너뜀')}")


def check_region(
    region_key: str, deal_ymd: str, prev_ymd: str, api_key: ApiKey, mock: bool, dataset: dict, save: bool = True
) -> tuple[region_cfg.HealthReport, dict]:
    """지역 코드 건강 점검. 최근 2개월(분석월·전월) 조회 결과를 코드별로 세어 판정한다.

    두 달 모두 어차피 글에 필요한 데이터라 점검만을 위해 API를 더 부르지 않는다
    (0건 코드는 호출 1번으로 끝나서 가볍다). 받은 데이터는 frames로 돌려줘 재사용한다.
    이상이면 법정동코드 자료로 새 코드 후보를 찾고, 확실한 후보만 반영해 한 번 더 점검한다.
    """
    region = REGIONS[region_key]
    frames: dict = {}
    counts: dict[str, dict[str, int]] = {}
    for ymd in (deal_ymd, prev_ymd):
        print(f"  ({ymd} {region['label']} {dataset['label']} 데이터 수집 중...)")
        rows, by_code, notes = fetch_month_rows(dataset["operation"], ymd, api_key, mock, region)
        frames[ymd] = (analyze.to_dataframe(rows), notes)
        for code, n in by_code.items():
            counts.setdefault(code, {})[ymd] = n
    report = region_cfg.evaluate_health(region_key, region, [deal_ymd, prev_ymd], counts)
    print(f"  건강 점검: {report.summary_line()}")
    if report.healthy:
        return report, frames

    try:
        report.candidates = region_cfg.find_candidates(report, region)
    except lawd_codes.LawdCodeError as exc:
        report.lookup_error = str(exc)
        return report, frames
    for cand in report.candidates:
        print(f"  후보: {cand['old_lawd_cd']} → {cand.get('new_lawd_cd') or '없음'} ({'확실' if cand['certain'] else '불확실'})")
    backup = json.loads(json.dumps(region))
    applied = region_cfg.apply_certain_candidates(region, report.candidates)
    if not applied:
        return report, frames

    # 확실한 후보(1:1, 법정동 구성 동일)만 반영됐다. 새 코드로 실제 데이터가 들어오는지 다시 확인한다.
    print("  새 코드 자동 반영 후 재점검: " + ", ".join(f"{c['old_lawd_cd']}→{c['new_lawd_cd']}" for c in applied))
    retry_frames: dict = {}
    retry_counts: dict[str, dict[str, int]] = {}
    for ymd in (deal_ymd, prev_ymd):
        rows, by_code, notes = fetch_month_rows(dataset["operation"], ymd, api_key, mock, region)
        retry_frames[ymd] = (analyze.to_dataframe(rows), notes)
        for code, n in by_code.items():
            retry_counts.setdefault(code, {})[ymd] = n
    retry = region_cfg.evaluate_health(region_key, region, [deal_ymd, prev_ymd], retry_counts)
    retry.candidates = report.candidates
    if not retry.healthy:
        REGIONS[region_key] = backup  # 새 코드로도 데이터가 없으면 반영을 되돌린다.
        report.candidates.append({"old_lawd_cd": "-", "old_name": "", "certain": False,
                                  "evidence": ["확실한 후보를 반영했지만 새 코드로도 데이터가 없어 되돌림"]})
        return report, frames
    region["codes_verified_on"] = datetime.now(KST).strftime("%Y-%m-%d")
    if not mock:
        probe_past_months_query(region, applied, month_year_before(deal_ymd), api_key, dataset)
    retry.auto_applied = applied
    if save:
        save_regions_file()
    print(f"  재점검: {retry.summary_line()}" + (" (regions.json 갱신)" if save else " (점검 전용: regions.json은 그대로)"))
    return retry, retry_frames


def save_regions_file() -> None:
    """메모리의 REGIONS 변경(자동 반영된 새 코드)을 regions.json에 쓴다. CI가 글과 함께 PR에 올린다."""
    cfg = region_cfg.load_config()
    cfg["regions"] = REGIONS
    region_cfg.save_config(cfg)


def write_health_report(reports: list[region_cfg.HealthReport], chosen: str | None) -> None:
    """코드 이상이 있었던 지역들의 점검 결과를, CI가 Issue로 올릴 마크다운 파일로 남긴다."""
    flagged = [r for r in reports if not r.healthy or r.auto_applied]
    if not flagged:
        HEALTH_REPORT_FILE.unlink(missing_ok=True)
        return
    lines = ["## 지역 코드 건강 점검 결과", ""]
    for r in reports:
        applied = " (새 코드 자동 반영 — 글 PR의 regions.json 변경을 확인해주세요)" if r.auto_applied else ""
        lines.append(f"- {r.summary_line()}{applied}")
    lines.append("")
    if chosen:
        lines.append(f"오늘 글은 **{REGIONS[chosen]['label']}**(`{chosen}`) 데이터로 생성을 시도했습니다.")
    else:
        lines.append("대체할 수 있는 정상 지역이 없어 오늘 글을 만들지 못했습니다.")
    lines += [
        "",
        "자동 반영되지 않은 지역은 아래 후보와 근거를 확인한 뒤 `pipeline/regions.json`의 `districts`와 "
        "`code_history`를 고쳐주세요. 옛/새 코드 응답 확인: "
        "`python pipeline/diagnose_codes.py --codes <옛코드>,<새코드>`",
        "",
    ]
    for r in flagged:
        lines += [r.to_markdown(), ""]
    HEALTH_REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")
    print(f"::region-alert::{HEALTH_REPORT_FILE.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description="전국 지역별 아파트 전월세 동향 글 자동 생성")
    parser.add_argument("--month", help="YYYYMM 형식. 생략하면 지난달 사용", default=None)
    parser.add_argument(
        "--region",
        choices=sorted(REGIONS.keys()),
        default=None,
        help="분석할 지역 키. 생략하면 이번 주 로테이션(REGION_ROTATION)에서 자동 선택",
    )
    parser.add_argument(
        "--no-fallback",
        action="store_true",
        help="지역 코드 이상이어도 로테이션 다음 지역으로 대체하지 않음 (진단용)",
    )
    parser.add_argument(
        "--health-only",
        action="store_true",
        help="글은 만들지 않고 지역 코드 건강 점검 결과만 출력 (--region 생략 시 로테이션 전체, regions.json은 바꾸지 않음)",
    )
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

    if args.health_only:
        try:
            for key in [args.region] if args.region else active_region_rotation():
                print(f"== {key} ({REGIONS[key]['label']}) 건강 점검: {deal_ymd} / {prev_ymd}")
                report, _ = check_region(key, deal_ymd, prev_ymd, api_key, args.mock, dataset, save=False)
                print(report.to_markdown() + "\n")
        except molit_api.MolitApiError as exc:
            print(f"공공데이터 API 오류: {exc}", file=sys.stderr)
            sys.exit(EXIT_API_ERROR)
        return

    first_region = args.region or pick_region_for_week()
    if not REGIONS[first_region].get("districts"):
        print(f"'{first_region}' 지역은 아직 districts가 채워지지 않았습니다.", file=sys.stderr)
        sys.exit(1)
    region_order = [first_region] if args.no_fallback else fallback_order(first_region)

    # 대부분의 후보 주제가 전월 데이터를 필요로 하므로, 분석월·전월 조회가 곧 건강 점검이다.
    print(f"[1/4] {deal_ymd} / {prev_ymd} {dataset['label']} 데이터 수집 + 지역 코드 건강 점검")
    reports: list[region_cfg.HealthReport] = []
    region_key: str | None = None
    frames_cache: dict[str, tuple] = {}
    try:
        for key in region_order:
            report, frames = check_region(key, deal_ymd, prev_ymd, api_key, args.mock, dataset)
            reports.append(report)
            if report.healthy:
                region_key, frames_cache = key, frames
                break
            print(f"  → {REGIONS[key]['label']}은(는) 오늘 건너뛰고 로테이션 다음 지역을 시도합니다.")
    except molit_api.MolitApiError as exc:
        print(f"공공데이터 API 오류로 데이터를 수집하지 못했습니다: {exc}", file=sys.stderr)
        sys.exit(EXIT_API_ERROR)
    write_health_report(reports, region_key)

    if region_key is None:
        print("오늘은 발행할 만한 주제가 없습니다 (모든 지역이 지역 코드 이상):")
        for r in reports:
            print(f"  - {r.summary_line()}")
        sys.exit(0)

    region_info = REGIONS[region_key]
    region_label = region_info["label"]
    region_group_label = region_info["group_label"]
    if region_key != first_region:
        print(f"  대체 지역: {REGIONS[first_region]['label']} → {region_label}")

    def get_frame(ymd: str):
        """(DataFrame, 조회 방식 메모). 개편 이전 달의 매핑이 불확실하면 MappingUncertain."""
        if ymd not in frames_cache:
            print(f"  ({ymd} {region_label} {dataset['label']} 데이터 수집 중...)")
            rows, _, notes = fetch_month_rows(dataset["operation"], ymd, api_key, args.mock, region_info)
            frames_cache[ymd] = (analyze.to_dataframe(rows), notes)
        return frames_cache[ymd]

    this_df_all, prev_df_all = get_frame(deal_ymd)[0], get_frame(prev_ymd)[0]
    print(f"[2/4] 데이터 정제 완료 ({region_label}: 이번달 {len(this_df_all)}건, 전월 {len(prev_df_all)}건)")

    ordered_topics = order_topics_by_recency(CANDIDATE_TOPICS)
    print(f"[3/4] 후보 주제 시도 순서: {[t['id'] for t in ordered_topics]}")

    skip_reasons: list[str] = []
    for topic in ordered_topics:
        try:
            if topic["id"] == VOLUME_PRICE_TOPIC_ID:
                result = try_volumeprice_topic(
                    region_key, region_label, region_group_label, this_df_all, prev_df_all, deal_ymd, prev_ymd
                )
            else:
                if topic.get("compare") == "yoy":
                    reference_ymd = month_year_before(deal_ymd)
                else:
                    reference_ymd = prev_ymd
                try:
                    reference_df_all, ref_notes = get_frame(reference_ymd)
                except molit_api.MolitApiError as exc:
                    print(f"공공데이터 API 오류로 데이터를 수집하지 못했습니다: {exc}", file=sys.stderr)
                    sys.exit(EXIT_API_ERROR)
                except MappingUncertain as exc:
                    # 근거 없이 옛 데이터와 비교하지 않는다: 이 주제만 건너뛰고 다음 후보로.
                    raise TopicSkipped(f"[{topic['id']}] 비교 기준월 옛 코드 매핑 불확실: {exc}") from exc
                result = try_topic(
                    topic, region_key, region_label, region_group_label, this_df_all, reference_df_all,
                    deal_ymd, reference_ymd, fetch_notes=ref_notes,
                )
        except TopicSkipped as exc:
            print(f"  건너뜀: {exc}")
            skip_reasons.append(str(exc))
            continue

        print(f"[4/4] 완료: {result['path']}/index.md")
        if region_key != first_region:
            result["region_note"] = f"{REGIONS[first_region]['label']} 지역 코드 이상으로 {region_label}(으)로 대체"
        print("::post-meta::" + json.dumps(result, ensure_ascii=False))
        return

    print(f"오늘({region_label})은 발행할 만한 주제가 없습니다 (모든 후보가 데이터/품질 기준 미달):")
    for reason in skip_reasons:
        print(f"  - {reason}")
    sys.exit(0)


if __name__ == "__main__":
    main()
