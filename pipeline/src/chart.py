"""분석 결과를 보여주는 matplotlib 차트 생성."""
from __future__ import annotations

from pathlib import Path

import matplotlib
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import pandas as pd

matplotlib.use("Agg")

# Windows에는 맑은 고딕, GitHub Actions(Ubuntu, fonts-nanum 설치 후)에는 나눔고딕이 있다.
# 설치된 폰트 중 먼저 발견되는 걸 쓰고, 하나도 없으면 기본 폰트로 두어(글자는 깨지지만) 빌드 자체는 실패하지 않게 한다.
_KOREAN_FONT_CANDIDATES = ["Malgun Gothic", "NanumGothic", "AppleGothic", "Noto Sans CJK KR"]
_available_fonts = {f.name for f in fm.fontManager.ttflist}
for _name in _KOREAN_FONT_CANDIDATES:
    if _name in _available_fonts:
        plt.rcParams["font.family"] = _name
        break

plt.rcParams["axes.unicode_minus"] = False


def district_bar_chart(
    district_summary: pd.DataFrame,
    out_path: Path,
    title: str,
    mean_col: str,
    unit_divisor: float,
    unit_name: str,
    top_n: int = 25,
    group_col: str = "구명",
) -> Path:
    """{group_col} 기준 평균값 막대그래프를 그려 PNG로 저장한다."""
    data = district_summary.head(top_n).copy()
    scaled_col = f"{mean_col}_scaled"
    data[scaled_col] = data[mean_col] / unit_divisor

    fig, ax = plt.subplots(figsize=(11, 6.5))
    bars = ax.bar(data[group_col], data[scaled_col], color="#3b6ea5")

    ax.set_title(title, fontsize=15, fontweight="bold", pad=14)
    ax.set_ylabel(f"{mean_col} ({unit_name})")
    ax.set_xticks(range(len(data)))
    ax.set_xticklabels(data[group_col], rotation=45, ha="right")
    ax.grid(axis="y", linestyle="--", alpha=0.4)

    for bar, value in zip(bars, data[scaled_col]):
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
