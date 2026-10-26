"""국토교통부 실거래가 공개시스템 API(공공데이터포털) 클라이언트.

공공데이터포털에서 발급받은 서비스키를 .env의 DATA_GO_KR_DECODING_KEY 또는
DATA_GO_KR_ENCODING_KEY 로 넣어두면 이 모듈이 알아서 인증해서 호출한다.
"""
from __future__ import annotations

import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import requests

from config import MOLIT_BASE_URL, NUM_ROWS_PER_DISTRICT

# 국토교통부 실거래가 API 원본 필드명 -> 이 프로젝트에서 쓰는 표준 필드명.
# apt_rent(아파트 전월세) 기준으로 확인된 스키마. 다른 데이터셋(매매/연립다세대/오피스텔)은
# 필드 구성이 달라서 전체 자동화 단계에서 별도로 매핑을 추가해야 한다.
RENT_FIELD_MAP = {
    "umdNm": "법정동",
    "aptNm": "아파트명",
    "deposit": "보증금액",
    "monthlyRent": "월세금액",
    "excluUseAr": "전용면적",
    "floor": "층",
    "dealYear": "년",
    "dealMonth": "월",
    "dealDay": "일",
    "buildYear": "건축년도",
}


def normalize_rent_row(raw: Dict[str, str]) -> Dict[str, str]:
    """API 원본 필드명을 표준 필드명으로 바꾼다. 매핑에 없는 필드(구명 등)는 그대로 둔다."""
    return {RENT_FIELD_MAP.get(k, k): v for k, v in raw.items()}


class MolitApiError(RuntimeError):
    """API 인증 실패, 잘못된 요청 등 국토부 API가 에러를 반환했을 때 발생."""


@dataclass
class ApiKey:
    decoding_key: str | None
    encoding_key: str | None

    @classmethod
    def from_env(cls) -> "ApiKey":
        return cls(
            decoding_key=os.getenv("DATA_GO_KR_DECODING_KEY") or None,
            encoding_key=os.getenv("DATA_GO_KR_ENCODING_KEY") or None,
        )

    def is_configured(self) -> bool:
        return bool(self.decoding_key or self.encoding_key)


def _parse_response(xml_text: str) -> Tuple[List[Dict[str, str]], int]:
    """<item> 태그들을 필드명 그대로 dict 리스트로, 함께 totalCount를 반환한다.

    아파트/연립다세대/오피스텔, 매매/전월세마다 필드 구성이 조금씩 달라서
    스키마를 하드코딩하지 않고 자식 태그를 그대로 읽어들인다.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        # 점검/장애 시 공공데이터포털이 XML 대신 HTML 안내 페이지를 돌려주는 경우가 있다.
        raise MolitApiError(f"API 응답을 XML로 해석할 수 없습니다 (서버 점검/장애 가능성): {exc}") from exc

    result_code_el = root.find("./header/resultCode")
    result_msg_el = root.find("./header/resultMsg")
    if result_code_el is not None and result_code_el.text not in ("00", "000"):
        raise MolitApiError(
            f"API 오류 (resultCode={result_code_el.text}): "
            f"{result_msg_el.text if result_msg_el is not None else '알 수 없는 오류'}"
        )

    # 서비스키가 아예 잘못됐을 때는 공공데이터포털 공통 에러 포맷(OpenAPI_ServiceResponse)으로 온다.
    common_err = root.find("./cmmMsgHeader/errMsg")
    if common_err is not None:
        raise MolitApiError(f"API 공통 오류: {common_err.text}")

    items = []
    for item in root.findall("./body/items/item"):
        items.append({child.tag: (child.text or "").strip() for child in item})

    total_count_el = root.find("./body/totalCount")
    total_count = int(total_count_el.text) if total_count_el is not None and total_count_el.text else len(items)

    return items, total_count


def _request_page(
    url: str,
    common_params: Dict[str, str],
    api_key: ApiKey,
    timeout: Tuple[int, int],
    max_retries: int = 6,
) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            if api_key.decoding_key:
                # requests가 알아서 퍼센트 인코딩을 해주므로, "디코딩된" 원본 키를 그대로 넘긴다.
                params = {"serviceKey": api_key.decoding_key, **common_params}
                resp = requests.get(url, params=params, timeout=timeout)
            else:
                # 이미 퍼센트 인코딩된 키라서 requests가 다시 인코딩하면 이중 인코딩 오류가 난다.
                # serviceKey만 쿼리스트링에 직접 붙이고, 나머지 파라미터만 requests에 맡긴다.
                query = "&".join(f"{k}={quote(str(v))}" for k, v in common_params.items())
                full_url = f"{url}?serviceKey={api_key.encoding_key}&{query}"
                resp = requests.get(full_url, timeout=timeout)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as exc:
            last_error = exc
            if attempt < max_retries - 1:
                # 공공데이터포털 서버 응답 지연/일시 장애 대응 (2s, 4s, 8s, 16s, 30s)
                time.sleep(min(2 ** (attempt + 1), 30))
    # 예외 메시지에 요청 URL(서비스키 포함)이 들어가므로, 로그/Issue로 새지 않게 가린다.
    safe_error = re.sub(r"serviceKey=[^&\s]+", "serviceKey=***", str(last_error))
    raise MolitApiError(f"API 호출이 {max_retries}회 재시도 후에도 실패했습니다: {safe_error}")


def fetch_district_month(
    operation: str,
    lawd_cd: str,
    deal_ymd: str,
    api_key: ApiKey,
    num_rows: int = NUM_ROWS_PER_DISTRICT,
    timeout: Tuple[int, int] = (10, 30),  # (접속, 응답) 초. 접속이 안 되는 장애는 빨리 재시도한다.
) -> List[Dict[str, str]]:
    """구(LAWD_CD) 하나, 월(YYYYMM) 하나에 대한 실거래 목록을 페이지네이션까지 처리해 모두 가져온다."""
    if not api_key.is_configured():
        raise MolitApiError(
            "공공데이터포털 서비스키가 설정되지 않았습니다. "
            ".env 파일의 DATA_GO_KR_DECODING_KEY(또는 ENCODING_KEY)를 채워주세요."
        )

    url = f"{MOLIT_BASE_URL}/{operation}"
    all_items: List[Dict[str, str]] = []
    page_no = 1

    while True:
        common_params = {
            "LAWD_CD": lawd_cd,
            "DEAL_YMD": deal_ymd,
            "numOfRows": str(num_rows),
            "pageNo": str(page_no),
        }
        resp = _request_page(url, common_params, api_key, timeout)
        items, total_count = _parse_response(resp.text)
        all_items.extend(items)

        if not items or len(all_items) >= total_count:
            break
        page_no += 1

    return all_items


def count_district_month(
    operation: str,
    lawd_cd: str,
    deal_ymd: str,
    api_key: ApiKey,
    timeout: Tuple[int, int] = (10, 30),
) -> Tuple[int, Dict[str, str] | None]:
    """한 페이지에 1건만 받아 totalCount(전체 건수)와 샘플 1건만 돌려준다.

    지역 코드 건강 점검처럼 "데이터가 들어오는지"만 확인할 때 쓰는 가벼운 호출이다.
    샘플의 sggCd/umdNm 필드로 API가 그 거래를 어느 코드로 분류하고 있는지도 볼 수 있다.
    """
    if not api_key.is_configured():
        raise MolitApiError("공공데이터포털 서비스키가 설정되지 않았습니다.")
    url = f"{MOLIT_BASE_URL}/{operation}"
    params = {"LAWD_CD": lawd_cd, "DEAL_YMD": deal_ymd, "numOfRows": "1", "pageNo": "1"}
    resp = _request_page(url, params, api_key, timeout, max_retries=3)
    items, total_count = _parse_response(resp.text)
    return total_count, (items[0] if items else None)


def fetch_region_month(
    operation: str,
    lawd_codes: Dict[str, str],
    deal_ymd: str,
    api_key: ApiKey,
    sleep_sec: float = 0.2,
) -> List[Dict[str, Any]]:
    """여러 구(district_name -> LAWD_CD)를 순회하며 한 달치 데이터를 모두 모은다.

    각 row에 어느 구에서 온 데이터인지 알 수 있도록 "구명" 필드를 추가한다.
    """
    all_rows, _ = fetch_region_month_with_counts(operation, lawd_codes, deal_ymd, api_key, sleep_sec=sleep_sec)
    return all_rows


def fetch_region_month_with_counts(
    operation: str,
    lawd_codes: Dict[str, str],
    deal_ymd: str,
    api_key: ApiKey,
    remap: Dict[str, List[Tuple[Any, str]]] | None = None,
    assign: Any = None,
    sleep_sec: float = 0.2,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """fetch_region_month와 같되, 조회한 코드별 건수({LAWD_CD: 건수})도 함께 돌려준다.

    remap({옛 LAWD_CD: 규칙})이 있으면 그 옛 코드도 조회해서, assign(규칙, 법정동명, 옛 코드)이
    정해 주는 새 시/군/구 이름을 "구명"으로 붙인다 (행정구역 개편 이전 달 조회용).
    코드별 건수는 지역 코드 건강 점검(모든 코드 0건 = 코드 이상)에 그대로 쓰인다.
    """
    all_rows: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for district_name, lawd_cd in lawd_codes.items():
        rows = fetch_district_month(operation, lawd_cd, deal_ymd, api_key)
        counts[lawd_cd] = len(rows)
        for row in rows:
            row["구명"] = district_name
        all_rows.extend(rows)
        time.sleep(sleep_sec)  # 공공데이터포털 트래픽 제한 배려
    for old_cd, rules in (remap or {}).items():
        rows = fetch_district_month(operation, old_cd, deal_ymd, api_key)
        counts[old_cd] = len(rows)
        for row in rows:
            row["구명"] = assign(rules, row.get("umdNm", ""), old_cd)
        all_rows.extend(rows)
        time.sleep(sleep_sec)
    return all_rows, counts
