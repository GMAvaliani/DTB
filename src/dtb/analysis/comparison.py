"""Сравнение сценариев: n-сторонняя разность итогов прогонов и Монте-Карло.

Кроме разности средних считаются **парные разности**: прогоны двух сценариев
с одинаковым сидом используют общие случайные числа, поэтому разность
``d_i = KPI_b(i) − KPI_a(i)`` избавлена от общей случайности и её дисперсия
намного меньше. По выборке разностей строится доверительный интервал
Стьюдента, и вывод «сценарий лучше» становится статистически обоснованным, а
не следствием удачного сида.
"""

from __future__ import annotations

import math
from typing import Any

from dtb.analysis.montecarlo import MonteCarloResult

#: Квантили распределения Стьюдента t_{0.975}(ν) для малых выборок;
#: при ν > 30 используется нормальное приближение.
_T_975 = {
    1: 12.706, 2: 4.303, 3: 3.182, 4: 2.776, 5: 2.571, 6: 2.447, 7: 2.365,
    8: 2.306, 9: 2.262, 10: 2.228, 11: 2.201, 12: 2.179, 13: 2.160, 14: 2.145,
    15: 2.131, 16: 2.120, 17: 2.110, 18: 2.101, 19: 2.093, 20: 2.086,
    21: 2.080, 22: 2.074, 23: 2.069, 24: 2.064, 25: 2.060, 26: 2.056,
    27: 2.052, 28: 2.048, 29: 2.045, 30: 2.042,
}
_Z_975 = 1.959964


def t_quantile_975(df: int) -> float:
    """Двусторонний 95%-квантиль Стьюдента для df степеней свободы."""
    if df <= 0:
        return 0.0
    return _T_975.get(df, _Z_975)


def paired_difference(values_a: list[float], values_b: list[float]) -> dict[str, Any]:
    """Парная разность b − a по сопряжённым прогонам с 95%-ДИ Стьюдента."""
    n = min(len(values_a), len(values_b))
    if n == 0:
        return {"n_pairs": 0, "mean": None, "ci_low": None, "ci_high": None,
                "std": None, "significant": None, "share_better": None}
    diffs = [values_b[i] - values_a[i] for i in range(n)]
    mean = sum(diffs) / n
    if n > 1:
        var = sum((d - mean) ** 2 for d in diffs) / (n - 1)
        std = math.sqrt(var)
        half = t_quantile_975(n - 1) * std / math.sqrt(n)
    else:
        std = 0.0
        half = 0.0
    low, high = mean - half, mean + half
    return {
        "n_pairs": n,
        "mean": round(mean, 6),
        "std": round(std, 6),
        "ci_low": round(low, 6),
        "ci_high": round(high, 6),
        # значимо, если ноль вне интервала (для n=1 интервал вырожден)
        "significant": bool(n > 1 and (low > 0 or high < 0)),
        "share_better": round(sum(1 for d in diffs if d > 0) / n, 4),
        "share_worse": round(sum(1 for d in diffs if d < 0) / n, 4),
    }


def _align_by_seed(
    base: MonteCarloResult, other: MonteCarloResult, values_key: str, kpi_id: str | None
) -> tuple[list[float], list[float], bool]:
    """Сопоставить прогоны по сиду; при отсутствии сидов — по порядку."""

    def series(mc: MonteCarloResult) -> list[float]:
        if values_key == "npv":
            return list(mc.run_npv)
        return list(mc.run_finals.get(kpi_id or "", []))

    a_values, b_values = series(base), series(other)
    a_seeds, b_seeds = list(base.run_seeds), list(other.run_seeds)
    if a_seeds and b_seeds and len(a_seeds) == len(a_values) and len(b_seeds) == len(b_values):
        index = {seed: i for i, seed in enumerate(b_seeds)}
        pairs = [
            (a_values[i], b_values[index[seed]])
            for i, seed in enumerate(a_seeds)
            if seed in index
        ]
        if pairs:
            return [p[0] for p in pairs], [p[1] for p in pairs], True
    n = min(len(a_values), len(b_values))
    return a_values[:n], b_values[:n], False


def compare_results(
    entries: list[tuple[str, MonteCarloResult]],
    baseline_label: str | None = None,
) -> dict[str, Any]:
    """entries: [(label, MonteCarloResult)] — first entry is baseline unless given."""
    if not entries:
        return {"kpis": {}, "goals": {}, "entries": []}
    labels = [label for label, _ in entries]
    baseline = baseline_label if baseline_label in labels else labels[0]
    base_mc = dict(entries)[baseline]

    kpi_ids = sorted({k for _, mc in entries for k in mc.final_kpis})
    kpis: dict[str, Any] = {}
    for kid in kpi_ids:
        base_mean = base_mc.final_kpis.get(kid, {}).get("mean")
        rows = []
        for label, mc in entries:
            final = mc.final_kpis.get(kid, {})
            mean = final.get("mean")
            delta = (
                round(mean - base_mean, 6)
                if mean is not None and base_mean is not None
                else None
            )
            rel = (
                round(delta / abs(base_mean), 4)
                if delta is not None and base_mean not in (None, 0)
                else None
            )
            rows.append(
                {
                    "label": label,
                    "mean": mean,
                    "p5": final.get("p5"),
                    "p95": final.get("p95"),
                    "delta_vs_baseline": delta,
                    "relative_delta": rel,
                    "series_mean": mc.kpi_stats.get(kid, {}).get("mean", []),
                    "grid": mc.grid,
                }
            )
        kpis[kid] = rows

    goal_ids = sorted({g for _, mc in entries for g in mc.goal_achievement_probability})
    goals: dict[str, Any] = {}
    for gid in goal_ids:
        base_p = base_mc.goal_achievement_probability.get(gid)
        goals[gid] = [
            {
                "label": label,
                "probability": mc.goal_achievement_probability.get(gid),
                "delta_vs_baseline": (
                    round(mc.goal_achievement_probability.get(gid, 0) - base_p, 4)
                    if base_p is not None
                    else None
                ),
            }
            for label, mc in entries
        ]

    # --- парные разности по сопряжённым прогонам -------------------------------
    paired: dict[str, Any] = {}
    for kid in kpi_ids:
        rows = []
        for label, mc in entries:
            if label == baseline:
                continue
            a_values, b_values, by_seed = _align_by_seed(base_mc, mc, "finals", kid)
            stats = paired_difference(a_values, b_values)
            stats.update({"label": label, "paired_by_seed": by_seed})
            rows.append(stats)
        if rows:
            paired[kid] = rows
    npv_rows = []
    for label, mc in entries:
        if label == baseline:
            continue
        a_values, b_values, by_seed = _align_by_seed(base_mc, mc, "npv", None)
        if not a_values or not b_values:
            continue
        stats = paired_difference(a_values, b_values)
        stats.update({"label": label, "paired_by_seed": by_seed})
        npv_rows.append(stats)

    economics = []
    for label, mc in entries:
        sample = mc.sample_result or {}
        econ = sample.get("economics") or {}
        if econ:
            economics.append(
                {
                    "label": label,
                    "npv": econ.get("npv"),
                    "net": econ.get("net"),
                    "income": econ.get("income"),
                    "expense": econ.get("expense"),
                    "currency": econ.get("currency"),
                    "by_article": econ.get("by_article", {}),
                    "warmup_hours": econ.get("warmup_hours"),
                }
            )

    return {
        "baseline": baseline,
        "entries": [
            {"label": label, "runs": mc.runs, "provenance": mc.provenance}
            for label, mc in entries
        ],
        "kpis": kpis,
        "goals": goals,
        "paired_differences": paired,
        "paired_npv": npv_rows,
        "economics": economics,
        "confidence_level": 0.95,
    }
