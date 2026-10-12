"""Адаптеры движков под контракт стыковки.

``DTBEngineAdapter`` — наш движок за контрактом: он же эталонная реализация,
по которой проверяется, что контракт выполним и достаточен.
``HTTPEngineAdapter`` — внешний движок по URI: тело запроса и ответа те же,
ответ проверяется на соответствие контракту до того, как попадёт в сравнение.
``CallableEngineAdapter`` — движок, переданный функцией: нужен для тестов и
для встроенных обёрток вокруг библиотек, работающих в том же процессе.
"""

from __future__ import annotations

import os
from typing import Any, Callable
from urllib.parse import urlparse

from dtb import ENGINE_VERSION
from dtb.analysis.montecarlo import _step_value
from dtb.domain.model import EnterpriseModel
from dtb.domain.scenario import Scenario
from dtb.interop.contract import (
    CONTRACT_VERSION,
    ContractError,
    ContractRequest,
    validate_response,
)
from dtb.simulation import simulate
from dtb.simulation.result import SimulationResult


def _grid(horizon: float, points: int) -> list[float]:
    points = max(1, int(points))
    return [round(horizon * i / points, 6) for i in range(points + 1)]


class DTBEngineAdapter:
    """Движок DTB за контрактом стыковки."""

    name = "dtb"

    def capabilities(self) -> list[str]:
        return [
            "trajectory", "economics", "logs", "control_loop", "observations",
            "calendars", "lookahead", "reproducible",
        ]

    def run(self, request: ContractRequest) -> dict[str, Any]:
        model = EnterpriseModel.from_snapshot(dict(request.model))
        # политики запроса дополняют модель: так контур управления можно
        # передать отдельно от модели, не переписывая её
        if request.policies:
            from dtb.domain.control import Policy

            for raw in request.policies:
                policy = Policy.model_validate(raw)
                model.policies[policy.id] = policy
        scenario_data = dict(request.scenario or {})
        scenario_data.setdefault("id", "contract")
        scenario_data.setdefault("name", "Прогон по контракту")
        scenario_data.setdefault("enterprise_id", model.enterprise.id)
        scenario = Scenario.model_validate(scenario_data)
        options = request.options or {}
        points = int(options.get("trajectory_points", 60))
        include_logs = bool(options.get("include_logs", True))
        log_limit = int(options.get("log_limit", 500))
        horizon = float(scenario.horizon_hours or model.enterprise.planning_horizon_hours)
        grid = _grid(horizon, points)

        runs: list[dict[str, Any]] = []
        failed = 0
        for seed in request.seeds:
            result = simulate(model, scenario, seed=seed)
            if result.status == "failed":
                failed += 1
            runs.append(self._run_payload(result, grid, include_logs, log_limit))
        return {
            "contract_version": CONTRACT_VERSION,
            "engine": {
                "name": self.name,
                "version": ENGINE_VERSION,
                "capabilities": self.capabilities(),
            },
            "runs": runs,
            "summary": {"runs": len(runs), "failed": failed},
        }

    def _run_payload(
        self,
        result: SimulationResult,
        grid: list[float],
        include_logs: bool,
        log_limit: int,
    ) -> dict[str, Any]:
        trajectory = {
            "grid": grid,
            "kpis": {
                kpi_id: [round(_step_value(series, t), 6) for t in grid]
                for kpi_id, series in sorted(result.kpi_series.items())
            },
            "stocks": {
                utype: [round(_step_value(series, t), 6) for t in grid]
                for utype, series in sorted(result.stock_series.items())
            },
            "pools": [[t, w, e] for t, w, e in result.pool_series],
        }
        payload: dict[str, Any] = {
            "seed": result.provenance.seed,
            "status": result.status,
            "trajectory": trajectory,
            "economics": result.economics
            or {"npv": 0.0, "net": 0.0, "by_article": {}, "currency": ""},
            "metrics": result.metrics,
            "final_kpis": result.final_kpis,
            "provenance": result.provenance.to_dict(),
            "content_digest": result.content_digest(),
        }
        if include_logs:
            payload["logs"] = {
                "events": result.event_log[:log_limit],
                "decisions": result.decision_log[:log_limit],
                "policies": result.policy_trace[:log_limit],
                "lookahead": result.lookahead_log[:log_limit],
                "rejected_actions": result.rejected_actions[:log_limit],
            }
        if result.error:
            payload["error"] = result.error
        return payload


def _allowed_hosts() -> list[str] | None:
    raw = os.environ.get("DTB_INTEROP_HOSTS", "").strip()
    if not raw:
        return None
    return [h.strip() for h in raw.split(",") if h.strip()]


class HTTPEngineAdapter:
    """Внешний движок по URI: тот же контракт поверх HTTP(S)."""

    def __init__(
        self,
        uri: str,
        name: str = "external",
        timeout: float = 120.0,
        capabilities: list[str] | None = None,
    ):
        self.uri = uri
        self.name = name
        self.timeout = timeout
        self._capabilities = capabilities or ["trajectory", "economics"]

    def capabilities(self) -> list[str]:
        return list(self._capabilities)

    def run(self, request: ContractRequest) -> dict[str, Any]:
        import httpx

        parsed = urlparse(self.uri)
        if parsed.scheme not in ("http", "https"):
            raise ContractError(f"URI движка {self.uri!r}: ожидается http(s)")
        allowed = _allowed_hosts()
        if allowed is not None and parsed.hostname not in allowed:
            raise ContractError(
                f"хост {parsed.hostname!r} не входит в список DTB_INTEROP_HOSTS"
            )
        try:
            response = httpx.post(
                self.uri, json=request.to_dict(), timeout=self.timeout
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # noqa: BLE001 — внешний движок недоступен
            raise ContractError(f"движок {self.name!r} недоступен: {exc}") from exc
        problems = validate_response(data, request)
        if problems:
            raise ContractError(
                f"ответ движка {self.name!r} нарушает контракт: " + "; ".join(problems)
            )
        return data


class CallableEngineAdapter:
    """Движок, заданный функцией в том же процессе (тесты, встроенные обёртки)."""

    def __init__(
        self,
        fn: Callable[[dict[str, Any]], dict[str, Any]],
        name: str = "callable",
        capabilities: list[str] | None = None,
        validate: bool = True,
    ):
        self.fn = fn
        self.name = name
        self._capabilities = capabilities or ["trajectory", "economics"]
        self.validate = validate

    def capabilities(self) -> list[str]:
        return list(self._capabilities)

    def run(self, request: ContractRequest) -> dict[str, Any]:
        data = self.fn(request.to_dict())
        if self.validate:
            problems = validate_response(data, request)
            if problems:
                raise ContractError(
                    f"ответ движка {self.name!r} нарушает контракт: "
                    + "; ".join(problems)
                )
        return data
