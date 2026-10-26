"""콘텐츠 생성 파이프라인 설정값. 자유롭게 수정하세요."""

import json
import os
from pathlib import Path

# 공공데이터포털 실거래가 API는 시/군/구 단위(LAWD_CD, 법정동코드 앞 5자리)로만 조회가
# 가능해서, "지역 전체"를 보려면 그 지역에 속한 시/군/구를 각각 호출해 합쳐야 한다.
#
# 지역 목록(표시 이름, 시/군/구 코드, 로테이션 순서, 행정구역 개편 이력)은 regions.json에 있다.
# 행정구역 개편이 있으면 코드가 아니라 그 파일을 고친다. 파일 형식은 regions.json의 "_설명" 참고.
#
# REGIONS의 각 항목 (regions.json의 regions.<key> 그대로):
#   label            : 글/제목에 그대로 쓰이는 지역명 (예: "서울", "전남광주통합특별시 광주권")
#   group_label      : 그 지역의 하위 행정구역 단위명 (예: "자치구", "시/군", "구/군")
#   districts        : [{"name": 행정구역명, "lawd_cd": LAWD_CD}]
#   code_history     : 행정구역 개편 이력과 옛 코드 → 새 코드 매핑 (없을 수 있음)
# REGIONS_FILE 환경변수로 다른 설정 파일을 지정할 수 있다 (개편 전 설정으로 건강 점검을 시뮬레이션할 때 등).
REGIONS_FILE = Path(os.getenv("REGIONS_FILE") or Path(__file__).resolve().parent / "regions.json")
_REGIONS_CONFIG = json.loads(REGIONS_FILE.read_text(encoding="utf-8"))
REGIONS = _REGIONS_CONFIG["regions"]

# 발행 로테이션 순서. generate_post.py가 이번 주에 어느 지역을 다룰지 이 순서대로
# 순환하며 고른다 (주 단위 고정 로테이션 - 같은 주의 월/수/금 글은 같은 지역을 다룸).
# 이번 주 지역의 코드가 이상하면(건강 점검 실패) 이 순서의 다음 지역으로 대체한다.
REGION_ROTATION = _REGIONS_CONFIG["rotation"]

# 국토교통부 실거래가 공개시스템 API (공공데이터포털 제공). 매매/전월세 x 아파트/연립다세대/오피스텔.
# https://www.data.go.kr/data/15126468/openapi.do (아파트 매매) 등에서 활용신청.
MOLIT_BASE_URL = "https://apis.data.go.kr/1613000"

DATASETS = {
    "apt_trade": {
        "label": "아파트 매매",
        "operation": "RTMSDataSvcAptTrade/getRTMSDataSvcAptTrade",
        "deal_type": "trade",
        "book_key": "apartment",
        "tags": ["아파트", "매매", "실거래가"],
    },
    "apt_rent": {
        "label": "아파트 전월세",
        "operation": "RTMSDataSvcAptRent/getRTMSDataSvcAptRent",
        "deal_type": "rent",
        "book_key": "apartment",
        "tags": ["아파트", "전월세", "실거래가"],
    },
    "row_trade": {
        "label": "연립다세대 매매",
        "operation": "RTMSDataSvcRHTrade/getRTMSDataSvcRHTrade",
        "deal_type": "trade",
        "book_key": "villa",
        "tags": ["연립다세대", "매매", "실거래가"],
    },
    "row_rent": {
        "label": "연립다세대 전월세",
        "operation": "RTMSDataSvcRHRent/getRTMSDataSvcRHRent",
        "deal_type": "rent",
        "book_key": "villa",
        "tags": ["연립다세대", "전월세", "실거래가"],
    },
    "officetel_trade": {
        "label": "오피스텔 매매",
        "operation": "RTMSDataSvcOffiTrade/getRTMSDataSvcOffiTrade",
        "deal_type": "trade",
        "book_key": "officetel",
        "tags": ["오피스텔", "매매", "실거래가"],
    },
    "officetel_rent": {
        "label": "오피스텔 전월세",
        "operation": "RTMSDataSvcOffiRent/getRTMSDataSvcOffiRent",
        "deal_type": "rent",
        "book_key": "officetel",
        "tags": ["오피스텔", "전월세", "실거래가"],
    },
}

# 구 하나당 한 번에 가져올 최대 행 수. 실거래 신고 건수가 많은 강남/송파 등을 감안해 여유있게 잡는다.
NUM_ROWS_PER_DISTRICT = 1000

# 전자책 홍보 문구를 글에 넣을 확률 (0.0~1.0). naver-blog-bot의 PROMO_PROBABILITY와 같은 개념.
PROMO_PROBABILITY = 0.7

# 홍보 문구가 있었던 글 이후, 최소 이만큼의 글을 건너뛰기 전에는 다시 넣지 않는다
# (바로 직전 글 포함). 예: 3이면 직전 3편 중 하나라도 홍보 문구가 있었으면 이번 글엔 넣지 않음.
PROMO_MIN_GAP = 3

CATEGORY_LABEL = "데이터분석"

# 애드센스 등 콘텐츠 품질 기준을 위한 발행 전 체크 값.
MIN_TRANSACTIONS = 10  # 이보다 거래건수가 적으면 그 주제는 건너뛰고 다른 후보로 대체
MIN_BODY_CHARS = 800  # 본문(마크다운 body) 글자수가 이보다 적으면 발행 후보에서 제외
