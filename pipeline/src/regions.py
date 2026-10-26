"""regions.json(지역 설정) 로딩, 옛 코드↔새 코드 매핑, 지역 코드 건강 점검/새 코드 탐색.

행정구역 개편으로 시/군/구 코드(LAWD_CD)가 바뀌면 실거래가 API가 0건을 돌려주는데,
에러가 아니라서 그냥 두면 "거래건수 부족"으로만 보인다 (2026-10-26 광주 사례). 이 모듈은
  1) 지역 설정을 코드가 아닌 regions.json에서 읽고,
  2) 개편 이전 달을 조회할 때 옛 코드로 받아 새 시/군/구로 다시 나눠 담는 매핑을 제공하고,
  3) 최근 2개월 조회 결과로 "코드 이상"을 판정하고, 법정동코드 자료에서 새 코드 후보를 찾는다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from src import lawd_codes

from config import REGIONS_FILE  # noqa: E402  (REGIONS_FILE 환경변수로 다른 설정 파일을 시험할 수 있음)


class MappingUncertain(Exception):
    """옛 코드 데이터를 새 시/군/구로 확실하게 옮길 근거가 없을 때. 해당 비교 주제는 건너뛴다."""


def load_config(path: Path = REGIONS_FILE) -> dict:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    for key in cfg["rotation"]:
        if key not in cfg["regions"]:
            raise ValueError(f"regions.json rotation에 있는 '{key}'가 regions에 없습니다.")
    return cfg


def _format_json(obj, indent: int = 0, width: int = 110) -> str:
    """짧은 객체/리스트는 한 줄로, 긴 문자열 리스트(동 목록)는 여러 줄에 나눠 담는다."""
    pad = "  " * indent
    one_line = json.dumps(obj, ensure_ascii=False)
    if len(pad) + len(one_line) <= width:
        return one_line
    if isinstance(obj, list) and obj and all(isinstance(x, str) and len(x) <= 40 for x in obj):
        rows, cur = [], []
        for item in obj:
            cur.append(json.dumps(item, ensure_ascii=False))
            if len(pad) + len(", ".join(cur)) > width - 10:
                rows.append(", ".join(cur))
                cur = []
        if cur:
            rows.append(", ".join(cur))
        return "[\n" + ",\n".join(f"{pad}  {r}" for r in rows) + f"\n{pad}]"
    if isinstance(obj, dict):
        items = [f"{pad}  {json.dumps(k, ensure_ascii=False)}: {_format_json(v, indent + 1, width)}" for k, v in obj.items()]
        return "{\n" + ",\n".join(items) + f"\n{pad}}}"
    if isinstance(obj, list):
        return "[\n" + ",\n".join(f"{pad}  {_format_json(v, indent + 1, width)}" for v in obj) + f"\n{pad}]"
    return one_line


def save_config(cfg: dict, path: Path = REGIONS_FILE) -> None:
    text = _format_json(cfg) + "\n"
    assert json.loads(text) == cfg
    path.write_text(text, encoding="utf-8", newline="\n")


def district_codes(region: dict) -> Dict[str, str]:
    """{시/군/구 이름: LAWD_CD} — 기존 fetch 함수들이 쓰는 형태."""
    return {d["name"]: d["lawd_cd"] for d in region["districts"]}


def _ym(date_str: str) -> str:
    """'2026-07-01' -> '202607'"""
    return date_str.replace("-", "")[:6]


def _label(yyyymm: str) -> str:
    return f"{yyyymm[:4]}년 {int(yyyymm[4:])}월"


# ---------------------------------------------------------------- 옛 코드 매핑

@dataclass
class FetchPlan:
    """한 달치를 어떤 코드로 조회해서 어느 시/군/구 이름으로 담을지."""

    # 그대로 조회하는 코드 {시/군/구 이름: LAWD_CD}
    direct: Dict[str, str] = field(default_factory=dict)
    # 옛 코드로 조회한 뒤 법정동으로 나눠 담는 규칙 {옛 LAWD_CD: [(dongs 또는 "*", 새 시/군/구 이름)]}
    remap: Dict[str, List[tuple]] = field(default_factory=dict)
    # 글 본문 "분석 방법"에 그대로 넣을 독자용 설명 (개편 이전 달을 어떻게 다뤘는지)
    notes: List[str] = field(default_factory=list)


def fetch_plan(region: dict, deal_ymd: str) -> FetchPlan:
    """개편(effective_date) 이전 달을 어떤 코드로 조회할지 정한다.

    code_history의 past_months_query가 그 기준이며, 추측이 아니라 실제 호출로 확인한 값이어야 한다.
      - "new_codes": API가 과거 달 데이터도 새 코드로 재분류해 둔 경우 → 현재 코드 그대로 조회
        (2026-07-01 광주·인천 개편은 2026-10-26 호출로 확인: 옛 코드는 202509·202606도 0건,
        새 코드는 같은 달에 정상 건수)
      - "old_codes": 과거 달은 옛 코드로만 나오는 경우 → 옛 코드로 받아 법정동 기준으로 새 시/군/구에 배정
      - 그 밖(미확인): MappingUncertain → 그 달과 비교하는 주제는 건너뛴다 (근거 없이 비교하지 않음)
    개편이 월 중간이면 그 달은 옛/새 코드가 섞일 수 있어 역시 MappingUncertain.
    """
    name_by_code = {d["lawd_cd"]: d["name"] for d in region["districts"]}
    plan = FetchPlan(direct=dict(district_codes(region)))
    for change in sorted(region.get("code_history", []), key=lambda c: c["effective_date"], reverse=True):
        eff = change["effective_date"]
        eff_ym = _ym(eff)
        if deal_ymd >= eff_ym and not (deal_ymd == eff_ym and not eff.endswith("-01")):
            continue  # 개편 이후 달: 현재 코드 그대로
        if deal_ymd == eff_ym:
            raise MappingUncertain(f"{deal_ymd}은 개편일({eff})이 월 중간이라 옛/새 코드 데이터가 섞일 수 있음")
        mode = change.get("past_months_query")
        if mode == "new_codes":
            plan.notes.append(
                f"{_label(deal_ymd)} 데이터는 행정구역 개편({eff}) 이전이지만, 국토교통부 API가 현재 "
                f"{region['group_label']} 기준으로 재분류해 제공하는 값을 사용했습니다."
            )
            continue
        if mode != "old_codes":
            raise MappingUncertain(f"{eff} 개편 이전 달을 API가 어느 코드로 주는지 확인되지 않음 ({change['description']})")
        if change.get("status") != "verified":
            raise MappingUncertain(f"{eff} 개편 매핑이 검증되지 않음(status={change.get('status')}): {change['description']}")
        for m in change["mapping"]:
            new_name = name_by_code.get(m["new_lawd_cd"])
            if new_name is None:
                raise MappingUncertain(f"매핑 대상 {m['new_lawd_cd']}가 현재 districts에 없음")
            # 새 코드가 옛 코드에서 온 경우, 그 달에는 새 코드로 직접 조회하지 않는다.
            plan.direct.pop(new_name, None)
            plan.remap.setdefault(m["old_lawd_cd"], []).append((m["dongs"], new_name))
        plan.notes.append(
            f"{_label(deal_ymd)} 데이터는 행정구역 개편({eff}) 이전이라 당시 시/군/구 코드로 조회한 뒤, "
            f"법정동 기준으로 현재 {region['group_label']}에 다시 묶어 비교했습니다."
        )
    # 여러 번 개편된 코드는 가장 최근 개편의 매핑이 먼저 들어가므로, 옛 코드가 다시 옛 코드로
    # 매핑되는 연쇄는 지원하지 않는다 (필요해지면 code_history를 펼쳐서 적을 것).
    return plan


def assign_district(rules: List[tuple], umd_name: str, old_code: str) -> str:
    """옛 코드로 받은 거래 1건을 법정동 이름으로 새 시/군/구에 배정한다."""
    for dongs, new_name in rules:
        if dongs == "*" or umd_name in dongs:
            return new_name
    raise MappingUncertain(f"옛 코드 {old_code}의 법정동 '{umd_name}'이 매핑 표에 없음")


# ---------------------------------------------------------------- 건강 점검

@dataclass
class HealthReport:
    region_key: str
    label: str
    months: List[str]
    counts: Dict[str, Dict[str, int]]  # {LAWD_CD: {YYYYMM: 건수}}
    names: Dict[str, str]  # {LAWD_CD: 시/군/구 이름}
    abolished: Dict[str, str] = field(default_factory=dict)  # {0건 코드: 폐지일} (법정동코드 자료 기준)
    lookup_error: str | None = None
    candidates: List[dict] = field(default_factory=list)
    auto_applied: List[dict] = field(default_factory=list)  # 자동 반영된 확실한 후보

    @property
    def zero_codes(self) -> List[str]:
        return [c for c, by_month in self.counts.items() if sum(by_month.values()) == 0]

    @property
    def all_zero(self) -> bool:
        return bool(self.counts) and len(self.zero_codes) == len(self.counts)

    @property
    def healthy(self) -> bool:
        # 모든 코드가 0건이면 "코드 이상". 일부만 0건이어도 그 코드가 법정동코드 자료에서
        # 폐지된 것으로 확인되면 글에서 시/군/구가 통째로 빠지므로 역시 이상으로 본다
        # (2026-10 인천 글이 "10개 구/군"이라고 쓰고 6곳만 집계했던 사례).
        return not self.all_zero and not self.abolished

    def summary_line(self) -> str:
        if self.healthy:
            zero = f", 0건 코드 {len(self.zero_codes)}개(폐지 아님)" if self.zero_codes else ""
            return f"{self.label}: 정상 ({len(self.counts)}개 코드{zero})"
        if self.all_zero:
            return f"{self.label}: 코드 이상 — 최근 {'/'.join(self.months)} 모든 코드({len(self.counts)}개)가 0건"
        return f"{self.label}: 코드 이상 — 폐지된 코드 {', '.join(f'{c}({self.names[c]})' for c in self.abolished)}"

    def to_markdown(self) -> str:
        lines = [f"#### {self.region_key} ({self.label}) — {'정상' if self.healthy else '코드 이상'}", ""]
        lines.append("| LAWD_CD | 이름 | " + " | ".join(self.months) + " | 법정동코드 자료 |")
        lines.append("|---|---|" + "---:|" * len(self.months) + "---|")
        for code, by_month in self.counts.items():
            status = f"폐지 {self.abolished[code]}" if code in self.abolished else ""
            cells = " | ".join(f"{by_month.get(m, 0):,}" for m in self.months)
            lines.append(f"| {code} | {self.names[code]} | {cells} | {status} |")
        if self.lookup_error:
            lines += ["", f"- 법정동코드 자료 조회 실패: {self.lookup_error}"]
        if self.candidates:
            lines += ["", "**새 코드 후보** (확실한 것만 자동 반영, 나머지는 사람이 확인 필요)", ""]
            lines.append("| 옛 코드 | 후보 코드 | 판정 | 근거 |")
            lines.append("|---|---|---|---|")
            for cand in self.candidates:
                lines.append(
                    f"| {cand['old_lawd_cd']} {cand['old_name']} | {cand.get('new_lawd_cd') or '-'} {cand.get('new_name', '')} "
                    f"| {'확실' if cand['certain'] else '불확실'} | {'<br>'.join(cand['evidence'])} |"
                )
        return "\n".join(lines)


def evaluate_health(
    region_key: str, region: dict, months: List[str], counts: Dict[str, Dict[str, int]]
) -> HealthReport:
    """조회 결과(코드별·월별 건수)로 건강 상태를 판정한다. 0건 코드는 법정동코드 자료로 폐지 여부를 확인한다."""
    names = {d["lawd_cd"]: d["name"] for d in region["districts"]}
    report = HealthReport(region_key, region["label"], months, counts, names)
    if not report.zero_codes:
        return report
    try:
        known = {r.lawd_cd: r for r in lawd_codes.list_sgg_for_sidos(sorted({c[:2] for c in report.zero_codes}))}
    except lawd_codes.LawdCodeError as exc:
        report.lookup_error = str(exc)
        return report
    for code in report.zero_codes:
        row = known.get(code)
        if row is None:
            report.abolished[code] = "자료에 없음"
        elif row.abolished:
            report.abolished[code] = row.abolished
    return report


# ---------------------------------------------------------------- 새 코드 탐색

def find_candidates(report: HealthReport, region: dict) -> List[dict]:
    """이상 코드마다 법정동코드 자료에서 새 코드 후보를 찾고 '확실' 여부를 판정한다.

    확실 = 같은 이름의 현존 시/군/구가 정확히 하나이고, 생성일이 옛 코드 폐지일과 같으며,
    폐지 직전 법정동 구성과 현재 법정동 구성이 완전히 같음 (= 이름/코드만 바뀐 1:1 개편).
    분할/통합(동이 여러 곳으로 나뉨)이나 이름이 다른 경우는 근거와 함께 '불확실'로 남긴다.
    """
    targets = report.zero_codes if report.all_zero else list(report.abolished)
    if not targets:
        return []
    sidos = lawd_codes.list_sido()
    # 지역 이름이나 설정된 시/도로 검색 범위를 정한다. 통합으로 생긴 새 시/도(예: 전남광주통합특별시)는
    # 옛 시/도 이름의 일부("광주")를 이름에 포함하는 경우가 많다.
    old_sido_names = set()
    current = {r.lawd_cd: r for r in lawd_codes.list_sgg_for_sidos(sorted({c[:2] for c in targets}))}
    for code in targets:
        if code in current:
            old_sido_names.add(current[code].full_name.split(" ")[0])
    keywords = {n.replace("광역시", "").replace("특별시", "").replace("특별자치시", "").replace("도", "") for n in old_sido_names}
    search_sidos = set(region.get("sido_codes", [])) | {c[:2] for c in targets}
    for cd, name in sidos.items():
        if any(k and k in name for k in keywords):
            search_sidos.add(cd)
    pool = [r for r in lawd_codes.list_sgg_for_sidos(sorted(search_sidos)) if r.is_current]

    results = []
    for code in targets:
        old = current.get(code)
        old_name = report.names.get(code, code)
        if old is None or not old.abolished:
            results.append({"old_lawd_cd": code, "old_name": old_name, "certain": False,
                            "evidence": ["법정동코드 자료에서 폐지 기록을 찾지 못함 (API 장애/일시적 0건일 수 있음)"]})
            continue
        same_name = [r for r in pool if r.name == old.name and r.lawd_cd != code]
        if not same_name:
            new_on_date = [r for r in pool if r.created == old.abolished]
            evidence = [f"폐지일 {old.abolished}", "같은 이름의 현존 시/군/구 없음 (분할/통합 가능성)",
                        "같은 날 생성된 코드: " + (", ".join(f"{r.lawd_cd} {r.full_name}" for r in new_on_date) or "없음")]
            if new_on_date:
                # 분할/통합이면 옛 법정동이 어느 새 코드로 갔는지가 매핑의 근거가 된다 (이름 기준이라 동명이동은 사람이 확인).
                old_dongs = set(lawd_codes.list_umd_names(code, lawd_codes.day_before(old.abolished)))
                spread = []
                for r in new_on_date:
                    shared = old_dongs & set(lawd_codes.list_umd_names(r.lawd_cd))
                    if shared:
                        spread.append(f"{r.name} {len(shared)}개")
                evidence.append(f"옛 법정동 {len(old_dongs)}개의 행방(이름 기준): " + (", ".join(spread) or "찾지 못함"))
            results.append({"old_lawd_cd": code, "old_name": old.full_name, "certain": False, "evidence": evidence})
            continue
        for cand in same_name:
            evidence = [f"옛 {old.full_name} 폐지일 {old.abolished}", f"새 {cand.full_name} 생성일 {cand.created or '-'}"]
            old_dongs = set(lawd_codes.list_umd_names(code, lawd_codes.day_before(old.abolished)))
            new_dongs = set(lawd_codes.list_umd_names(cand.lawd_cd))
            same_dongs = old_dongs == new_dongs and bool(old_dongs)
            evidence.append(
                f"법정동 구성 일치 ({len(old_dongs)}개)" if same_dongs
                else f"법정동 구성 다름 (옛 {len(old_dongs)}개, 새 {len(new_dongs)}개, 공통 {len(old_dongs & new_dongs)}개)"
            )
            certain = len(same_name) == 1 and cand.created == old.abolished and same_dongs
            if len(same_name) > 1:
                evidence.append(f"같은 이름 후보가 {len(same_name)}개라 하나로 정할 수 없음")
            results.append({"old_lawd_cd": code, "old_name": old.full_name, "new_lawd_cd": cand.lawd_cd,
                            "new_name": cand.full_name, "effective_date": old.abolished, "certain": certain,
                            "evidence": evidence})
    return results


def apply_certain_candidates(region: dict, candidates: List[dict]) -> List[dict]:
    """확실한 후보만 region(dict)에 반영한다: districts 코드 교체 + code_history 기록.

    모든 이상 코드에 확실한 후보가 있을 때만 반영한다 (일부만 바꾸면 글이 여전히 불완전함).
    반영한 후보 목록을 돌려주며, 하나라도 불확실하면 아무것도 바꾸지 않고 빈 목록을 돌려준다.
    """
    by_old: Dict[str, List[dict]] = {}
    for cand in candidates:
        by_old.setdefault(cand["old_lawd_cd"], []).append(cand)
    chosen = []
    for old_code, cands in by_old.items():
        certain = [c for c in cands if c["certain"]]
        if len(certain) != 1:
            return []
        chosen.append(certain[0])
    if not chosen:
        return []

    by_date: Dict[str, List[dict]] = {}
    for cand in chosen:
        by_date.setdefault(cand["effective_date"], []).append(cand)
    for d in region["districts"]:
        for cand in chosen:
            if d["lawd_cd"] == cand["old_lawd_cd"]:
                d["lawd_cd"] = cand["new_lawd_cd"]
    for eff, cands in by_date.items():
        region.setdefault("code_history", []).append({
            "effective_date": f"{eff[:4]}-{eff[4:6]}-{eff[6:]}",
            "description": "자동 탐색으로 반영한 1:1 코드 변경",
            "status": "verified",
            "evidence": [f"{c['old_lawd_cd']}→{c['new_lawd_cd']}: " + "; ".join(c["evidence"]) for c in cands],
            "mapping": [{"old_lawd_cd": c["old_lawd_cd"], "old_name": c["old_name"],
                         "new_lawd_cd": c["new_lawd_cd"], "dongs": "*"} for c in cands],
        })
    return chosen
