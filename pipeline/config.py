"""콘텐츠 생성 파이프라인 설정값. 자유롭게 수정하세요."""

# 서울 25개 자치구 법정동코드(앞 5자리, LAWD_CD). 공공데이터포털 실거래가 API는
# 구 단위로만 조회가 가능해서, "서울 전체"를 보려면 25개 구를 각각 호출해 합쳐야 한다.
SEOUL_DISTRICTS = {
    "종로구": "11110",
    "중구": "11140",
    "용산구": "11170",
    "성동구": "11200",
    "광진구": "11215",
    "동대문구": "11230",
    "중랑구": "11260",
    "성북구": "11290",
    "강북구": "11305",
    "도봉구": "11320",
    "노원구": "11350",
    "은평구": "11380",
    "서대문구": "11410",
    "마포구": "11440",
    "양천구": "11470",
    "강서구": "11500",
    "구로구": "11530",
    "금천구": "11545",
    "영등포구": "11560",
    "동작구": "11590",
    "관악구": "11620",
    "서초구": "11650",
    "강남구": "11680",
    "송파구": "11710",
    "강동구": "11740",
}

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

CATEGORY_LABEL = "데이터분석"
