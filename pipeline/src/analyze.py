"""아파트 전월세 실거래 데이터 정제 및 분석."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd


def _has_batchim(word: str) -> bool:
    """단어 마지막 글자에 받침이 있는지 (조사 은/는, 이/가 선택용)."""
    if not word:
        return False
    code = ord(word[-1])
    if 0xAC00 <= code <= 0xD7A3:
        return (code - 0xAC00) % 28 != 0
    return False


def josa_eun_neun(word: str) -> str:
    return "은" if _has_batchim(word) else "는"


def josa_i_ga(word: str) -> str:
    return "이" if _has_batchim(word) else "가"


def to_dataframe(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    """API에서 받은 raw dict 목록을 정제된 DataFrame으로 변환한다."""
    if not rows:
        return pd.DataFrame(
            columns=["구명", "법정동", "전용면적", "층", "보증금액", "월세금액", "년", "월", "일"]
        )

    df = pd.DataFrame(rows)

    def to_int(series: pd.Series) -> pd.Series:
        return (
            series.astype(str)
            .str.replace(",", "", regex=False)
            .str.strip()
            .replace("", "0")
            .astype(int)
        )

    df["보증금액"] = to_int(df["보증금액"])
    df["월세금액"] = to_int(df["월세금액"])
    df["전용면적"] = df["전용면적"].astype(float)
    df["층"] = pd.to_numeric(df.get("층", 0), errors="coerce").fillna(0).astype(int)
    df["년"] = df["년"].astype(int)
    df["월"] = df["월"].astype(int)

    # 전월세전환율 관행치(연 12% 통용)를 적용한 전세환산보증금. 월세 계약도 전세가와
    # 나란히 비교할 수 있도록 만든 근사값이며, 기사에는 "환산" 수치임을 명시한다.
    df["전세환산보증금"] = df["보증금액"] + (df["월세금액"] * 100)
    df["평당보증금"] = df["전세환산보증금"] / (df["전용면적"] / 3.3058)

    return df


def filter_pure_jeonse(df: pd.DataFrame) -> pd.DataFrame:
    """월세 없이 순수 전세로 계약된 건만 남긴다."""
    return df[df["월세금액"] == 0].copy()


def filter_wolse(df: pd.DataFrame) -> pd.DataFrame:
    """월세(반전세 포함, 월세금액 > 0)로 계약된 건만 남긴다."""
    return df[df["월세금액"] > 0].copy()


# 전용면적(㎡) 구간(평형대). 국민주택규모(85㎡) 등 통상적으로 쓰이는 구간을 따른다.
AREA_BANDS: List[Tuple[float, float, str]] = [
    (0, 60, "60㎡ 이하"),
    (60, 85, "60~85㎡"),
    (85, 135, "85~135㎡"),
    (135, float("inf"), "135㎡ 초과"),
]


def _area_band_label(area: float) -> str:
    for lower, upper, label in AREA_BANDS:
        if lower < area <= upper:
            return label
    return AREA_BANDS[0][2]


def build_area_band_df(df_all: pd.DataFrame) -> pd.DataFrame:
    """순수 전세 계약을 전용면적 구간(평형대)별로 나눈다."""
    jeonse_df = filter_pure_jeonse(df_all)
    if jeonse_df.empty:
        return jeonse_df.assign(평형대=pd.Series(dtype=str))
    jeonse_df = jeonse_df.copy()
    jeonse_df["평형대"] = jeonse_df["전용면적"].apply(_area_band_label)
    return jeonse_df


def build_conversion_df(df_all: pd.DataFrame) -> pd.DataFrame:
    """반전세/월세 계약의 전월세전환율(연 환산, %)을 추정한다.

    실제 시세 대신 같은 구·같은 달 순수 전세 평균 보증금을 기준가로 삼는 근사치이므로,
    표본이 적은 구나 기준가보다 보증금이 큰 경우(계산 불가)는 제외한다.
    """
    jeonse_df = filter_pure_jeonse(df_all)
    wolse_df = filter_wolse(df_all)
    if jeonse_df.empty or wolse_df.empty:
        return wolse_df.iloc[0:0].assign(전환율=pd.Series(dtype=float))

    reference_deposit = jeonse_df.groupby("구명")["보증금액"].mean()
    df = wolse_df.copy()
    df["기준전세가"] = df["구명"].map(reference_deposit)
    df = df.dropna(subset=["기준전세가"])
    df = df[df["기준전세가"] > df["보증금액"]]
    df["전환율"] = (df["월세금액"] * 12) / (df["기준전세가"] - df["보증금액"]) * 100
    return df[(df["전환율"] > 0) & (df["전환율"] <= 30)].copy()


def build_volume_price_table(this_df_all: pd.DataFrame, prev_df_all: pd.DataFrame) -> pd.DataFrame:
    """구별 순수 전세 거래건수와 평균 보증금 변동률(전월 대비, %)을 함께 정리한다."""
    this_jeonse = filter_pure_jeonse(this_df_all)
    prev_jeonse = filter_pure_jeonse(prev_df_all)
    if this_jeonse.empty:
        return pd.DataFrame(columns=["구명", "거래건수", "가격변동률"])

    this_mean = this_jeonse.groupby("구명")["보증금액"].mean()
    prev_mean = prev_jeonse.groupby("구명")["보증금액"].mean()
    counts = this_jeonse.groupby("구명").size()

    rows = []
    for district, count in counts.items():
        if district not in prev_mean.index or prev_mean[district] == 0:
            continue
        change_pct = (this_mean[district] - prev_mean[district]) / prev_mean[district] * 100
        rows.append({"구명": district, "거래건수": int(count), "가격변동률": round(change_pct, 2)})

    if not rows:
        return pd.DataFrame(columns=["구명", "거래건수", "가격변동률"])
    return pd.DataFrame(rows).sort_values("거래건수", ascending=False).reset_index(drop=True)


def transaction_price_correlation(volume_price_df: pd.DataFrame) -> float | None:
    """거래건수와 가격 변동률의 상관계수(피어슨). 표본이 너무 적으면 None."""
    if len(volume_price_df) < 5:
        return None
    corr = volume_price_df["거래건수"].corr(volume_price_df["가격변동률"])
    return None if pd.isna(corr) else round(float(corr), 3)


def summarize_by_group(
    df: pd.DataFrame,
    group_col: str,
    value_col: str,
    out_prefix: str,
    round_ndigits: int = 0,
) -> pd.DataFrame:
    """{group_col} 기준 평균/중위 {out_prefix}와 거래건수를 정리한다."""
    mean_col, median_col = f"평균 {out_prefix}", f"중위 {out_prefix}"
    if df.empty:
        return pd.DataFrame(columns=[group_col, mean_col, median_col, "거래건수"])

    grouped = (
        df.groupby(group_col)[value_col]
        .agg(**{mean_col: "mean", median_col: "median", "거래건수": "count"})
        .reset_index()
    )
    grouped[mean_col] = grouped[mean_col].round(round_ndigits)
    grouped[median_col] = grouped[median_col].round(round_ndigits)
    if round_ndigits == 0:
        grouped[mean_col] = grouped[mean_col].astype(int)
        grouped[median_col] = grouped[median_col].astype(int)
    return grouped.sort_values(mean_col, ascending=False).reset_index(drop=True)


def summarize_by_district(df: pd.DataFrame, value_col: str, out_prefix: str) -> pd.DataFrame:
    """구별 평균/중위 {out_prefix}와 거래건수를 정리한다."""
    return summarize_by_group(df, "구명", value_col, out_prefix)


def month_over_month(
    this_month_df: pd.DataFrame, prev_month_df: pd.DataFrame, value_col: str
) -> Tuple[float, float]:
    """이번 달 vs 전월 평균값과 증감률(%)을 반환한다."""
    this_avg = this_month_df[value_col].mean() if not this_month_df.empty else 0.0
    prev_avg = prev_month_df[value_col].mean() if not prev_month_df.empty else 0.0
    if prev_avg == 0:
        return this_avg, 0.0
    change_pct = (this_avg - prev_avg) / prev_avg * 100
    return this_avg, change_pct


def build_insights(
    this_month_df: pd.DataFrame,
    prev_month_df: pd.DataFrame,
    group_summary: pd.DataFrame,
    region_label: str,
    month_label: str,
    *,
    value_col: str,
    out_prefix: str,
    unit_divisor: float,
    unit_name: str,
    deal_desc: str,
    exclude_desc: str,
    group_col: str = "구명",
    group_count_desc: str = "25개 자치구",
    group_noun: str = "곳",
    compare_label: str = "전월",
) -> List[str]:
    """분석 결과로부터 사람이 읽을 인사이트 문장 목록을 만든다."""
    insights: List[str] = []
    mean_col = f"평균 {out_prefix}"

    if this_month_df.empty:
        insights.append(f"{month_label} {region_label} {deal_desc} 실거래 데이터가 조회되지 않았습니다.")
        return insights

    this_avg, change_pct = month_over_month(this_month_df, prev_month_df, value_col)
    direction = "상승" if change_pct > 0 else ("하락" if change_pct < 0 else "보합")
    insights.append(
        f"{month_label} {region_label} 아파트 {deal_desc} 평균 {out_prefix}{josa_eun_neun(out_prefix)} "
        f"약 {this_avg / unit_divisor:.1f}{unit_name}으로, {compare_label} 대비 {abs(change_pct):.1f}% {direction}했습니다."
    )

    if not group_summary.empty:
        top = group_summary.iloc[0]
        bottom = group_summary.iloc[-1]
        insights.append(
            f"{group_count_desc} 중 평균 {out_prefix}{josa_i_ga(out_prefix)} 가장 높은 {group_noun}은 {top[group_col]}"
            f"(약 {top[mean_col] / unit_divisor:.1f}{unit_name})이었고, "
            f"가장 낮은 {group_noun}은 {bottom[group_col]}(약 {bottom[mean_col] / unit_divisor:.1f}{unit_name})이었습니다."
        )

        most_traded = group_summary.sort_values("거래건수", ascending=False).iloc[0]
        insights.append(
            f"거래량 기준으로는 {most_traded[group_col]}에서 {int(most_traded['거래건수'])}건으로 "
            f"가장 활발하게 {deal_desc} 계약이 체결됐습니다."
        )

    total_contracts = len(this_month_df)
    insights.append(
        f"{month_label} {region_label}에서 신고된 {deal_desc} 계약은 총 {total_contracts}건입니다 "
        f"({exclude_desc})."
    )

    return insights
