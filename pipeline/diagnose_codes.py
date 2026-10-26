"""시군구 코드(LAWD_CD)별로 실거래가 API가 실제로 데이터를 돌려주는지 직접 호출해 확인한다.

행정구역 개편 때 "옛 코드와 새 코드 중 API가 무엇을 받는지"를 추측이 아니라 호출 결과로
판단하기 위한 진단 도구다. 서비스키가 필요하다(.env 또는 환경변수).

사용 예:
    python pipeline/diagnose_codes.py --codes 29110,12210 --months 202609,202606,202509
    python pipeline/diagnose_codes.py --region gwangju   # regions.json의 현재 코드 + 옛 코드 전부
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

PIPELINE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPELINE_DIR))
load_dotenv(PIPELINE_DIR.parent / ".env")

from config import DATASETS, REGIONS  # noqa: E402
from src import molit_api  # noqa: E402
from src.molit_api import ApiKey  # noqa: E402

DEFAULT_MONTHS = ["202609", "202608", "202607", "202606", "202509"]
DEFAULT_DATASETS = ["apt_rent", "apt_trade"]


def region_codes(region_key: str) -> dict[str, str]:
    """{코드: 설명} — 현재 코드와 code_history에 적힌 옛 코드를 모두 모은다."""
    region = REGIONS[region_key]
    codes = {d["lawd_cd"]: f"현재 {d['name']}" for d in region["districts"]}
    for change in region.get("code_history", []):
        for old in change["old_codes"]:
            codes.setdefault(old["lawd_cd"], f"옛 {old['name']} (~{change['effective_date']} 폐지)")
    return codes


def main() -> None:
    parser = argparse.ArgumentParser(description="LAWD_CD별 실거래가 API 응답 건수 진단")
    parser.add_argument("--codes", help="쉼표로 구분한 5자리 코드 목록")
    parser.add_argument("--region", choices=sorted(REGIONS.keys()))
    parser.add_argument("--months", default=",".join(DEFAULT_MONTHS))
    parser.add_argument("--datasets", default=",".join(DEFAULT_DATASETS))
    args = parser.parse_args()

    api_key = ApiKey.from_env()
    if not api_key.is_configured():
        print("서비스키(DATA_GO_KR_DECODING_KEY/ENCODING_KEY)가 없습니다.", file=sys.stderr)
        sys.exit(1)

    codes: dict[str, str] = {}
    if args.region:
        codes.update(region_codes(args.region))
    if args.codes:
        for code in args.codes.split(","):
            codes.setdefault(code.strip(), "")
    if not codes:
        parser.error("--codes 또는 --region 중 하나는 필요합니다.")

    months = [m.strip() for m in args.months.split(",") if m.strip()]
    for ds_key in [d.strip() for d in args.datasets.split(",") if d.strip()]:
        ds = DATASETS[ds_key]
        print(f"\n### {ds['label']} ({ds['operation'].split('/')[0]})")
        print("| LAWD_CD | 설명 | " + " | ".join(months) + " | 샘플 sggCd/umdNm |")
        print("|---|---|" + "---:|" * len(months) + "---|")
        for code, desc in codes.items():
            cells, sample_desc = [], ""
            for ymd in months:
                try:
                    total, sample = molit_api.count_district_month(ds["operation"], code, ymd, api_key)
                    cells.append(f"{total:,}")
                    if sample and not sample_desc:
                        sample_desc = f"{ymd}: {sample.get('sggCd', '?')}/{sample.get('umdNm', '?')}"
                except molit_api.MolitApiError as exc:
                    cells.append(f"오류({str(exc)[:40]})")
                time.sleep(0.2)
            print(f"| {code} | {desc} | " + " | ".join(cells) + f" | {sample_desc} |")


if __name__ == "__main__":
    main()
