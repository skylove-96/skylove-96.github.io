"""법정동코드 최신 자료와 regions.json을 비교해 폐지·신설된 시/군/구 코드를 찾는다 (월 1회 실행).

- regions.json에 있는 코드가 폐지됐거나 자료에 없으면 → "폐지" (그대로 두면 글에서 그 구가 빠짐)
- 각 지역의 시/도(sido_codes) 안에서 codes_verified_on 이후 생성된 코드 → "신설" (추가 검토 대상)
- 전국에서 최근(--days, 기본 62일) 생성·폐지된 코드 → 참고용 (새 시/도 통합처럼 sido_codes 밖의 변화)

결과는 마크다운으로 출력하고, 변경이 있으면 region_codes_report.md를 남긴다 (CI가 Issue로 올림).
서비스키가 필요 없다.

사용 예:
    python pipeline/check_region_codes.py
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

PIPELINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_DIR))

from src import lawd_codes  # noqa: E402
from src import regions as region_cfg  # noqa: E402

REPORT_FILE = PIPELINE_DIR.parent / "region_codes_report.md"
KST = ZoneInfo("Asia/Seoul")


def main() -> None:
    parser = argparse.ArgumentParser(description="법정동코드 자료 ↔ regions.json 월간 비교")
    parser.add_argument("--days", type=int, default=62, help="전국 참고 목록에 넣을 최근 변경 기간(일)")
    args = parser.parse_args()

    cfg = region_cfg.load_config()
    today = datetime.now(KST)
    since = (today - timedelta(days=args.days)).strftime("%Y%m%d")

    try:
        sidos = lawd_codes.list_sido()
        all_rows = {}
        for sido_cd in sorted(sidos):
            for row in lawd_codes.list_sgg(sido_cd):
                all_rows[row.lawd_cd] = row
    except lawd_codes.LawdCodeError as exc:
        print(f"법정동코드 자료 조회 실패: {exc}", file=sys.stderr)
        sys.exit(3)

    problems: list[str] = []
    lines = [f"## 법정동코드 ↔ regions.json 월간 비교 ({today:%Y-%m-%d})", ""]
    for key, region in cfg["regions"].items():
        configured = {d["lawd_cd"]: d["name"] for d in region.get("districts", [])}
        verified = region.get("codes_verified_on", "2000-01-01").replace("-", "")
        abolished, missing, created = [], [], []
        for code, name in configured.items():
            row = all_rows.get(code)
            if row is None:
                missing.append(f"{code} {name}")
            elif row.abolished:
                abolished.append(f"{code} {row.full_name} (폐지 {row.abolished})")
        for row in all_rows.values():
            if row.lawd_cd[:2] in region.get("sido_codes", []) and row.is_current and row.lawd_cd not in configured:
                if row.created and row.created > verified:
                    created.append(f"{row.lawd_cd} {row.full_name} (생성 {row.created})")
        status = "변경 있음" if (abolished or missing or created) else "이상 없음"
        lines.append(f"### {key} ({region['label']}) — {status}")
        for title, items in (("폐지된 코드", abolished), ("자료에 없는 코드", missing), (f"{verified} 이후 신설", created)):
            if items:
                lines.append(f"- **{title}**: " + ", ".join(items))
                problems.extend(f"{key}: {title} {item}" for item in items)
        lines.append("")

    recent = sorted(
        (r for r in all_rows.values() if (r.created and r.created >= since) or (r.abolished and r.abolished >= since)),
        key=lambda r: r.lawd_cd,
    )
    lines.append(f"### 참고: 전국 최근 {args.days}일 생성·폐지 ({since} 이후)")
    lines.append("")
    if recent:
        lines += ["| 코드 | 이름 | 생성일 | 폐지일 |", "|---|---|---|---|"]
        lines += [f"| {r.lawd_cd} | {r.full_name} | {r.created or '-'} | {r.abolished or '-'} |" for r in recent]
    else:
        lines.append("- 없음")
    lines += [
        "",
        "폐지된 코드는 `pipeline/regions.json`의 districts를 새 코드로 바꾸고, 옛 코드 → 새 코드 매핑을 "
        "code_history에 근거와 함께 적어주세요 (전년 동월 비교에 쓰임).",
        "API가 어느 코드를 받는지는 `python pipeline/diagnose_codes.py --codes <옛코드>,<새코드>`로 확인할 수 있습니다.",
    ]
    text = "\n".join(lines) + "\n"
    print(text)
    if problems:
        REPORT_FILE.write_text(text, encoding="utf-8")
        print(f"::region-codes-changed::{len(problems)}")
    else:
        REPORT_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
