"""Проверка движка на соответствие контракту и сравнение двух движков.

Проверка — не вопрос доверия, а батарея прогонов: движку подают заведомо
корректные запросы и смотрят, выполняются ли обещания контракта. Отдельно
проверяется воспроизводимость (один и тот же сид — один и тот же ответ) и
чувствительность к сиду (разные сиды — разные траектории: движок действительно
использует случайность, а не выдаёт заготовку).

Сравнение двух движков идёт на одних входных данных и одних сидах, поэтому
разность итогов — это разность моделей расчёта, а не разность случайностей.
"""

from __future__ import annotations

import json
from typing import Any

from dtb.analysis.comparison import paired_difference
from dtb.interop.contract import (
    CAPABILITIES,
    CONTRACT_VERSION,
    ContractError,
    ContractRequest,
    EngineAdapter,
    validate_response,
)


def _digest(payload: Any) -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]


def _comparable(run: dict[str, Any]) -> dict[str, Any]:
    """Часть прогона, по которой сравнивают движки (без служебных отметок)."""
    return {
        "trajectory": run.get("trajectory"),
        "economics": {
            k: v for k, v in (run.get("economics") or {}).items()
            if k not in ("measured_to", "measured_from", "entries")
        },
        "final_kpis": run.get("final_kpis"),
    }


def certify(
    adapter: EngineAdapter,
    request: ContractRequest,
    check_reproducibility: bool = True,
    check_seed_sensitivity: bool = True,
) -> dict[str, Any]:
    """Прогнать батарею проверок контракта и вернуть отчёт."""
    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, detail: str = "") -> None:
        checks.append({"id": check_id, "passed": bool(passed), "detail": detail})

    declared = list(adapter.capabilities())
    unknown = [c for c in declared if c not in CAPABILITIES]
    add(
        "capabilities_known",
        not unknown,
        f"неизвестные возможности: {', '.join(unknown)}" if unknown else
        f"объявлено: {', '.join(declared)}",
    )

    try:
        response = adapter.run(request)
    except ContractError as exc:
        add("responds", False, str(exc))
        return _report(adapter, request, checks, None)
    except Exception as exc:  # noqa: BLE001 — любая ошибка движка это нарушение
        add("responds", False, f"{type(exc).__name__}: {exc}")
        return _report(adapter, request, checks, None)
    add("responds", True, "движок ответил на корректный запрос")

    problems = validate_response(response, request)
    add("schema", not problems, "; ".join(problems) if problems else "ответ полон")

    runs = response.get("runs") or []
    add(
        "runs_per_seed",
        len(runs) == len(request.seeds),
        f"прогонов {len(runs)}, сидов {len(request.seeds)}",
    )
    finite = all(
        isinstance(run.get("economics", {}).get("npv"), (int, float))
        for run in runs
        if isinstance(run, dict)
    )
    add("economics_numeric", finite, "NPV каждого прогона — число")

    if check_reproducibility:
        try:
            again = adapter.run(request)
            same = [_digest(_comparable(r)) for r in (response.get("runs") or [])] == [
                _digest(_comparable(r)) for r in (again.get("runs") or [])
            ]
            add(
                "reproducible",
                same,
                "повторный запрос с теми же сидами дал тот же результат"
                if same
                else "повторный запрос дал другой результат — воспроизводимости нет",
            )
        except Exception as exc:  # noqa: BLE001
            add("reproducible", False, f"повторный запрос не удался: {exc}")

    if check_seed_sensitivity and len(request.seeds) > 1:
        digests = {_digest(_comparable(r)) for r in runs if isinstance(r, dict)}
        add(
            "seed_sensitive",
            len(digests) > 1,
            "разные сиды дали разные траектории"
            if len(digests) > 1
            else "разные сиды дали одинаковый результат — случайность не используется",
        )

    return _report(adapter, request, checks, response)


def _report(
    adapter: EngineAdapter,
    request: ContractRequest,
    checks: list[dict[str, Any]],
    response: dict[str, Any] | None,
) -> dict[str, Any]:
    return {
        "engine": adapter.name,
        "contract_version": CONTRACT_VERSION,
        "declared_capabilities": list(adapter.capabilities()),
        "seeds": list(request.seeds),
        "passed": all(c["passed"] for c in checks),
        "total": len(checks),
        "failed": sum(1 for c in checks if not c["passed"]),
        "checks": checks,
        "engine_info": (response or {}).get("engine", {}),
        "summary": (response or {}).get("summary", {}),
    }


def compare_engines(
    left: EngineAdapter,
    right: EngineAdapter,
    request: ContractRequest,
    kpi_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Сравнить два движка на одних входных данных и одних сидах."""
    left_response = left.run(request)
    right_response = right.run(request)

    def finals(response: dict[str, Any]) -> dict[int, dict[str, float]]:
        out: dict[int, dict[str, float]] = {}
        for run in response.get("runs") or []:
            if not isinstance(run, dict):
                continue
            seed = run.get("seed")
            if isinstance(seed, int):
                out[seed] = dict(run.get("final_kpis") or {})
        return out

    def npvs(response: dict[str, Any]) -> dict[int, float]:
        out: dict[int, float] = {}
        for run in response.get("runs") or []:
            if isinstance(run, dict) and isinstance(run.get("seed"), int):
                value = (run.get("economics") or {}).get("npv")
                if isinstance(value, (int, float)):
                    out[run["seed"]] = float(value)
        return out

    left_finals, right_finals = finals(left_response), finals(right_response)
    shared = sorted(set(left_finals) & set(right_finals))
    ids = kpi_ids or sorted(
        {k for values in left_finals.values() for k in values}
        & {k for values in right_finals.values() for k in values}
    )
    kpis: dict[str, Any] = {}
    for kpi_id in ids:
        a = [left_finals[s].get(kpi_id, 0.0) for s in shared]
        b = [right_finals[s].get(kpi_id, 0.0) for s in shared]
        stats = paired_difference(a, b)
        stats["left_mean"] = round(sum(a) / len(a), 6) if a else None
        stats["right_mean"] = round(sum(b) / len(b), 6) if b else None
        kpis[kpi_id] = stats
    left_npv, right_npv = npvs(left_response), npvs(right_response)
    npv_seeds = sorted(set(left_npv) & set(right_npv))
    npv_stats = paired_difference(
        [left_npv[s] for s in npv_seeds], [right_npv[s] for s in npv_seeds]
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "left": {
            "engine": left.name,
            "capabilities": list(left.capabilities()),
            "info": left_response.get("engine", {}),
        },
        "right": {
            "engine": right.name,
            "capabilities": list(right.capabilities()),
            "info": right_response.get("engine", {}),
        },
        "shared_capabilities": sorted(
            set(left.capabilities()) & set(right.capabilities())
        ),
        "paired_seeds": shared,
        "kpis": kpis,
        "npv": npv_stats,
    }
