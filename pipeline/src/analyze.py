"""아파트 전월세 실거래 데이터 정제 및 분석."""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import pandas as pd


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


def summarize_by_district(df: pd.DataFrame, value_col: str = "보증금액") -> pd.DataFrame:
    """구별 평균/중위 전세보증금과 거래건수를 정리한다."""
    if df.empty:
        return pd.DataFrame(columns=["구명", "평균보증금", "중위보증금", "거래건수"])

    grouped = (
        df.groupby("구명")[value_col]
        .agg(평균보증금="mean", 중위보증금="median", 거래건수="count")
        .reset_index()
    )
    grouped["평균보증금"] = grouped["평균보증금"].round(0).astype(int)
    grouped["중위보증금"] = grouped["중위보증금"].round(0).astype(int)
    return grouped.sort_values("평균보증금", ascending=False).reset_index(drop=True)


def month_over_month(
    this_month_df: pd.DataFrame, prev_month_df: pd.DataFrame
) -> Tuple[float, float]:
    """이번 달 vs 전월 순수전세 평균보증금과 증감률(%)을 반환한다."""
    this_avg = this_month_df["보증금액"].mean() if not this_month_df.empty else 0.0
    prev_avg = prev_month_df["보증금액"].mean() if not prev_month_df.empty else 0.0
    if prev_avg == 0:
        return this_avg, 0.0
    change_pct = (this_avg - prev_avg) / prev_avg * 100
    return this_avg, change_pct


def build_insights(
    this_month_df: pd.DataFrame,
    prev_month_df: pd.DataFrame,
    district_summary: pd.DataFrame,
    region_label: str,
    month_label: str,
) -> List[str]:
    """분석 결과로부터 사람이 읽을 인사이트 문장 목록을 만든다."""
    insights: List[str] = []

    if this_month_df.empty:
        insights.append(f"{month_label} {region_label} 순수 전세 실거래 데이터가 조회되지 않았습니다.")
        return insights

    this_avg, change_pct = month_over_month(this_month_df, prev_month_df)
    direction = "상승" if change_pct > 0 else ("하락" if change_pct < 0 else "보합")
    insights.append(
        f"{month_label} {region_label} 아파트 순수 전세 평균 보증금은 "
        f"약 {this_avg / 10000:.1f}억 원으로, 전월 대비 {abs(change_pct):.1f}% {direction}했습니다."
    )

    if not district_summary.empty:
        top = district_summary.iloc[0]
        bottom = district_summary.iloc[-1]
        insights.append(
            f"25개 자치구 중 평균 전세보증금이 가장 높은 곳은 {top['구명']}"
            f"(약 {top['평균보증금'] / 10000:.1f}억 원)이었고, "
            f"가장 낮은 곳은 {bottom['구명']}(약 {bottom['평균보증금'] / 10000:.1f}억 원)이었습니다."
        )

        most_traded = district_summary.sort_values("거래건수", ascending=False).iloc[0]
        insights.append(
            f"거래량 기준으로는 {most_traded['구명']}에서 {int(most_traded['거래건수'])}건으로 "
            f"가장 활발하게 전세 계약이 체결됐습니다."
        )

    total_contracts = len(this_month_df)
    insights.append(
        f"{month_label} {region_label}에서 신고된 순수 전세 계약은 총 {total_contracts}건입니다 "
        f"(월세를 낀 반전세/월세 계약은 제외, 전용면적 기준 단순 비교)."
    )

    return insights
