"""국토교통부 실거래가 공개시스템 API(공공데이터포털) 클라이언트.

공공데이터포털에서 발급받은 서비스키를 .env의 DATA_GO_KR_DECODING_KEY 또는
DATA_GO_KR_ENCODING_KEY 로 넣어두면 이 모듈이 알아서 인증해서 호출한다.
"""
from __future__ import annotations

import os
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
    root = ET.fromstring(xml_text)

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
    timeout: int,
    max_retries: int = 4,
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
                time.sleep(2**attempt)  # 공공데이터포털 서버 응답 지연 대응 (1s, 2s, 4s ...)
    raise MolitApiError(f"API 호출이 {max_retries}회 재시도 후에도 실패했습니다: {last_error}")


def fetch_district_month(
    operation: str,
    lawd_cd: str,
    deal_ymd: str,
    api_key: ApiKey,
    num_rows: int = NUM_ROWS_PER_DISTRICT,
    timeout: int = 30,
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
    all_rows: List[Dict[str, Any]] = []
    for district_name, lawd_cd in lawd_codes.items():
        rows = fetch_district_month(operation, lawd_cd, deal_ymd, api_key)
        for row in rows:
            row["구명"] = district_name
        all_rows.extend(rows)
        time.sleep(sleep_sec)  # 공공데이터포털 트래픽 제한 배려
    return all_rows
