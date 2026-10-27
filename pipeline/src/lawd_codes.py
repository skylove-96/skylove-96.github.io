"""행정표준코드관리시스템(code.go.kr) 법정동코드 조회 클라이언트.

실거래가 API의 LAWD_CD는 법정동코드 앞 5자리(시/도 2자리 + 시/군/구 3자리)다.
행정구역 개편(통합/분구/폐지)으로 이 코드가 바뀌면 옛 코드로는 데이터가 0건으로
돌아오므로, 개편 여부와 새 코드를 이 모듈로 확인한다.

서비스키 없이 공개 조회 화면(법정동코드목록조회)을 그대로 호출한다.
'법정동코드 전체자료' 내려받기는 2026-10 기준 서버가 "파일이 삭제되었거나 없습니다"로
응답해 쓸 수 없어서, 시/도 → 시/군/구 단위로 나눠 조회한다.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Dict, List

import requests

BASE_URL = "https://www.code.go.kr/stdcode"
TIMEOUT = (10, 30)

_SIDO_SELECT_RE = re.compile(r'<select name="sidoCd".*?</select>', re.S)
_SIDO_OPTION_RE = re.compile(r'<option value="(\d{2})"\s*>\s*([^<]+?)\s*</option>')
_SGG_JS_RE = re.compile(r'strSgg(Nm|Cd) = "([^"]*)"')
_ROW_RE = re.compile(r'<td class="table_left">(\d{10})</td>(.*?)(?=<tr>|</tr>|</table>)', re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")


class LawdCodeError(RuntimeError):
    """법정동코드 조회 화면에 접속하지 못했거나 응답 형식이 예상과 다를 때."""


@dataclass
class SggCode:
    lawd_cd: str  # 5자리 (실거래가 API의 LAWD_CD)
    full_name: str  # 예: "전남광주통합특별시 광산구"
    created: str  # YYYYMMDD, 모르면 ""
    abolished: str  # YYYYMMDD, 현존이면 ""
    code10: str = ""  # 10자리 법정동코드 원본

    @property
    def name(self) -> str:
        """시/도 이름을 뗀 시/군/구 이름 (예: "광산구", "수원시 장안구")."""
        return self.full_name.split(" ", 1)[1] if " " in self.full_name else self.full_name

    @property
    def is_current(self) -> bool:
        return not self.abolished


def _post(path: str, data: Dict[str, str], retries: int = 3) -> str:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            resp = requests.post(f"{BASE_URL}/{path}", data=data, timeout=TIMEOUT)
            resp.raise_for_status()
            resp.encoding = "utf-8"  # 헤더는 utf-8이지만 requests가 추정을 틀리는 경우가 있다.
            return resp.text
        except requests.exceptions.RequestException as exc:
            last = exc
            time.sleep(2 * (attempt + 1))
    raise LawdCodeError(f"법정동코드 조회({path}) 실패: {last}")


def _clean(cell: str) -> str:
    return _TAG_RE.sub("", cell).replace("&nbsp;", "").replace("&nbsp", "").strip()


def list_sido() -> Dict[str, str]:
    """{시/도 코드 2자리: 시/도 이름}. 신설된 시/도(예: 12 전남광주통합특별시)도 여기 나온다."""
    html = _post("regCodeL.do", {"pageSize": "10", "cPage": "1", "disuseAt": "0"})
    select = _SIDO_SELECT_RE.search(html)
    found = dict(_SIDO_OPTION_RE.findall(select.group(0))) if select else {}
    if not found:
        raise LawdCodeError("시/도 목록을 읽지 못했습니다 (조회 화면 형식 변경 가능성).")
    return found


def _sgg_suffixes(sido_cd: str) -> List[str]:
    """시/도 아래 시/군/구 3자리 코드 목록 (폐지된 코드가 섞여 나올 수 있다)."""
    html = _post("sggCodeIL.do", {"sidoCd": sido_cd, "searchOk": "0"})
    parts = dict(_SGG_JS_RE.findall(html))
    codes = [c for c in parts.get("Cd", "").split(",") if c]
    return codes


def _query_rows(sido_cd: str, sgg_cd: str, disuse: str, page_size: int = 10, page: int = 1) -> List[SggCode]:
    html = _post(
        "regCodeL.do",
        {
            "cPage": str(page),
            "pageSize": str(page_size),
            "sidoCd": sido_cd,
            "sggCd": sgg_cd,
            "umdCd": "*",
            "riCd": "*",
            "disuseAt": disuse,  # ALL / 0(현존) / 1(폐지)
            "codeseId": "00002",
            # 출력 항목: 상위지역코드, 생성일, 폐지일 (0 = 출력)
            "chkHigh": "0",
            "chkCrtDt": "0",
            "chkClsDt": "0",
            "chkWantCnt": "3",
            "searchOk": "0",
        },
    )
    flat = html.replace("\r", "").replace("\n", "").replace("\t", "")
    rows: List[SggCode] = []
    for code, cells_html in _ROW_RE.findall(flat):
        cells = [_clean(c) for c in _CELL_RE.findall(cells_html)]
        # disuseAt=ALL이면 [법정동명, 현존/폐지, 상위지역코드, 생성일, 폐지일],
        # 그 외에는 [법정동명, 상위지역코드, 생성일, 폐지일] — 뒤에서부터 읽어 두 경우를 함께 처리한다.
        if len(cells) < 4:
            continue
        rows.append(
            SggCode(lawd_cd=code[:5], full_name=cells[0], created=cells[-2], abolished=cells[-1], code10=code)
        )
    return rows


def list_sgg(sido_cd: str, sleep_sec: float = 0.2) -> List[SggCode]:
    """시/도 아래 모든 시/군/구(현존 + 폐지)를 생성일/폐지일과 함께 돌려준다.

    시/군/구 행(법정동코드 뒤 5자리가 00000)만 모은다. 일반구가 있는 시(예: 수원시 41110)는
    시 자체 행과 일반구 행이 모두 나오므로, 실거래가 조회에 쓸 코드인지는 호출하는 쪽에서 고른다.
    """
    result: Dict[str, SggCode] = {}
    for suffix in _sgg_suffixes(sido_cd):
        for row in _query_rows(sido_cd, suffix, "ALL"):
            if row.lawd_cd == f"{sido_cd}{suffix}":
                # 같은 코드가 폐지 후 재사용되면 현존 행을 우선한다.
                prev = result.get(row.lawd_cd)
                if prev is None or (not row.abolished and prev.abolished):
                    result[row.lawd_cd] = row
                break
        time.sleep(sleep_sec)
    return sorted(result.values(), key=lambda r: r.lawd_cd)


def list_sgg_for_sidos(sido_cds: List[str]) -> List[SggCode]:
    rows: List[SggCode] = []
    for sido_cd in sido_cds:
        rows.extend(list_sgg(sido_cd))
    return rows


def _alive_on(row: SggCode, yyyymmdd: str) -> bool:
    return (not row.created or row.created <= yyyymmdd) and (not row.abolished or row.abolished > yyyymmdd)


def list_umd_names(lawd_cd: str, alive_on: str | None = None, max_pages: int = 10) -> List[str]:
    """시/군/구 하나에 속한 읍/면/동 이름 목록 (리 단위 제외).

    옛 코드↔새 코드가 같은 지역인지 '동 구성이 같은가'로 판단하기 위함이다.
    alive_on(YYYYMMDD)을 주면 그날 존재하던 동만 고른다. 폐지된 시/군/구에는 수십 년 전에
    다른 구로 넘어간 동까지 폐지 행으로 남아 있어서, 폐지 직전 날짜로 걸러야 비교가 정확하다.
    """
    names: set[str] = set()
    for page in range(1, max_pages + 1):
        rows = _query_rows(lawd_cd[:2], lawd_cd[2:], "ALL", page_size=100, page=page)
        for row in rows:
            is_umd = row.code10[5:8] != "000" and row.code10[8:] == "00"
            if row.lawd_cd != lawd_cd or not is_umd:
                continue
            if alive_on and not _alive_on(row, alive_on):
                continue
            names.add(row.full_name.split(" ")[-1])
        if len(rows) < 100:
            break
    return sorted(names)


def day_before(yyyymmdd: str) -> str:
    from datetime import date, timedelta

    d = date(int(yyyymmdd[:4]), int(yyyymmdd[4:6]), int(yyyymmdd[6:]))
    return (d - timedelta(days=1)).strftime("%Y%m%d")
