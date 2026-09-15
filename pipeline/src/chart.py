"""분석 결과를 보여주는 matplotlib 차트 생성."""
from __future__ import annotations

from pathlib import Path

import matplotlib
import matplotlib.pyplot as plt
import pandas as pd

matplotlib.use("Agg")

# Windows 기본 한글 폰트. 다른 OS라면 설치된 한글 폰트 이름으로 바꿔주세요.
plt.rcParams["font.family"] = "Malgun Gothic"
plt.rcParams["axes.unicode_minus"] = False


def district_bar_chart(
    district_summary: pd.DataFrame,
    out_path: Path,
    title: str,
    value_col: str = "평균보증금",
    top_n: int = 25,
) -> Path:
    """구별 평균 전세보증금 막대그래프를 그려 PNG로 저장한다."""
    data = district_summary.head(top_n).copy()
    data[f"{value_col}_억"] = data[value_col] / 10000

    fig, ax = plt.subplots(figsize=(11, 6.5))
    bars = ax.bar(data["구명"], data[f"{value_col}_억"], color="#3b6ea5")

    ax.set_title(title, fontsize=15, fontweight="bold", pad=14)
    ax.set_ylabel("평균 전세보증금 (억 원)")
    ax.set_xticks(range(len(data)))
    ax.set_xticklabels(data["구명"], rotation=45, ha="right")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    for bar, value in zip(bars, data[f"{value_col}_억"]):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height(),
            f"{value:.1f}",
            ha="center",
            va="bottom",
            fontsize=8,
        )

    fig.tight_layout()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    return out_path
