"""Scenario, simulation, forecast and optimization routes + SSE progress."""

from __future__ import annotations

import asyncio
import json
import queue
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from dtb.ai import AIAssistant
from dtb.analysis import build_forecast, compare_results, run_monte_carlo
from dtb.analysis.bottlenecks import analyze_bottlenecks
from dtb.analysis.montecarlo import MonteCarloResult, wrap_single
from dtb.api.deps import actor_of, build_graph, ctx, get_model_or_404
from dtb.api.security import require_role, simulation_limiter
from dtb.common.ids import new_id
from dtb.domain import Scenario
from dtb.domain.kpi import KPIDirection
from dtb.optimization import (
    Constraint,
    DecisionVariable,
    OptimizationProblem,
    get_optimizer,
)
from dtb.simulation import SimulationResult, simulate
from dtb.validation import validate_model, validate_scenario
from dtb.verification import BENCHMARKS, run_benchmarks, verify_run

router = APIRouter()

VIEW = Depends(require_role("viewer"))
EDIT = Depends(require_role("editor"))


# -------------------------------------------------------------------- scenarios --


@router.get("/scenarios", dependencies=[VIEW])
def list_scenarios(request: Request, enterprise_id: str | None = None) -> list[dict[str, Any]]:
    rows = ctx(request).db.list_scenarios(enterprise_id)
    return [row["data"] for row in rows]


@router.post("/scenarios", dependencies=[EDIT], status_code=201)
def create_scenario(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    context = ctx(request)
    if not body.get("id"):
        body["id"] = new_id("scn")
    try:
        scenario = Scenario.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json()))
    model = get_model_or_404(context, scenario.enterprise_id)
    report = validate_scenario(model, scenario)
    if not report.ok_to_simulate:
        raise HTTPException(status_code=422, detail=report.to_dict())
    context.db.save_scenario(scenario)
    context.db.audit(actor_of(request), "scenario_created", scenario.enterprise_id,
                     "scenario", scenario.id)
    return {"id": scenario.id}


@router.get("/scenarios/{scenario_id}", dependencies=[VIEW])
def get_scenario(request: Request, scenario_id: str) -> dict[str, Any]:
    scenario = ctx(request).db.load_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"scenario {scenario_id!r} not found")
    return scenario.model_dump(mode="json")


@router.put("/scenarios/{scenario_id}", dependencies=[EDIT])
def update_scenario(request: Request, scenario_id: str, body: dict[str, Any]) -> dict[str, Any]:
    context = ctx(request)
    existing = context.db.load_scenario(scenario_id)
    if existing is None:
        raise HTTPException(status_code=404, detail=f"scenario {scenario_id!r} not found")
    body["id"] = scenario_id
    body.setdefault("version", existing.version + 1)
    try:
        scenario = Scenario.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=json.loads(exc.json()))
    model = get_model_or_404(context, scenario.enterprise_id)
    report = validate_scenario(model, scenario)
    if not report.ok_to_simulate:
        raise HTTPException(status_code=422, detail=report.to_dict())
    context.db.save_scenario(scenario)
    context.db.audit(actor_of(request), "scenario_updated", scenario.enterprise_id,
                     "scenario", scenario.id)
    return {"id": scenario.id, "version": scenario.version}


@router.delete("/scenarios/{scenario_id}", dependencies=[EDIT])
def delete_scenario(request: Request, scenario_id: str) -> dict[str, Any]:
    context = ctx(request)
    scenario = context.db.load_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"scenario {scenario_id!r} not found")
    context.db.delete_scenario(scenario_id)
    context.db.audit(actor_of(request), "scenario_deleted", scenario.enterprise_id,
                     "scenario", scenario_id)
    return {"deleted": scenario_id}


class WhatIfRequest(BaseModel):
    name: str = ""
    description: str = ""
    initial_stock_overrides: dict[str, float] = Field(default_factory=dict)
    event_rate_multipliers: dict[str, float] = Field(default_factory=dict)
    sed_duration_scales: dict[str, float] = Field(default_factory=dict)
    resource_capacity_overrides: dict[str, float] = Field(default_factory=dict)
    parameter_overrides: dict[str, Any] = Field(default_factory=dict)


@router.post("/scenarios/{scenario_id}/whatif", dependencies=[EDIT], status_code=201)
def create_whatif(request: Request, scenario_id: str, body: WhatIfRequest) -> dict[str, Any]:
    """Derive an alternative scenario from a base one (what-if analysis)."""
    context = ctx(request)
    base = context.db.load_scenario(scenario_id)
    if base is None:
        raise HTTPException(status_code=404, detail=f"scenario {scenario_id!r} not found")
    derived = base.model_copy(deep=True)
    derived.id = new_id("scn")
    derived.name = body.name or f"{base.name} (what-if)"
    derived.description = body.description
    derived.base_scenario_id = base.id
    derived.version = 1
    derived.initial_stock_overrides.update(body.initial_stock_overrides)
    derived.event_rate_multipliers.update(body.event_rate_multipliers)
    derived.resource_capacity_overrides.update(body.resource_capacity_overrides)
    derived.parameter_overrides.update(body.parameter_overrides)
    from dtb.domain.scenario import SEDOverride

    for sed_id, scale in body.sed_duration_scales.items():
        override = derived.sed_overrides.get(sed_id) or SEDOverride()
        override.duration_scale = scale
        derived.sed_overrides[sed_id] = override
    model = get_model_or_404(context, derived.enterprise_id)
    report = validate_scenario(model, derived)
    if not report.ok_to_simulate:
        raise HTTPException(status_code=422, detail=report.to_dict())
    context.db.save_scenario(derived)
    context.db.audit(actor_of(request), "whatif_created", derived.enterprise_id,
                     "scenario", derived.id, details={"base": base.id})
    return {"id": derived.id, "base_scenario_id": base.id}


# ------------------------------------------------------------------ simulations --


class SimulationRequest(BaseModel):
    scenario_id: str
    kind: str = "monte_carlo"  # single | monte_carlo
    runs: int | None = Field(default=None, ge=1, le=1000)
    seed: int | None = None
    label: str = ""


def _load_for_run(context, scenario_id: str):
    scenario = context.db.load_scenario(scenario_id)
    if scenario is None:
        raise HTTPException(status_code=404, detail=f"scenario {scenario_id!r} not found")
    model = context.db.load_model(scenario.enterprise_id)
    if model is None:
        raise HTTPException(status_code=404, detail="enterprise not found")
    model_report = validate_model(model)
    scenario_report = validate_scenario(model, scenario)
    if not model_report.ok_to_simulate or not scenario_report.ok_to_simulate:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "модель содержит критические ошибки — симуляция запрещена",
                "model_validation": model_report.to_dict(),
                "scenario_validation": scenario_report.to_dict(),
            },
        )
    return model, scenario


@router.post("/simulations", dependencies=[EDIT], status_code=202)
def start_simulation(request: Request, body: SimulationRequest) -> dict[str, Any]:
    context = ctx(request)
    simulation_limiter.check(actor_of(request))
    if body.kind not in ("single", "monte_carlo"):
        raise HTTPException(status_code=422, detail="kind must be single|monte_carlo")
    model, scenario = _load_for_run(context, body.scenario_id)
    sim_id = new_id("sim")
    context.db.create_simulation(
        sim_id, scenario.enterprise_id, scenario.id, body.kind, label=body.label
    )
    context.db.audit(actor_of(request), "simulation_started", scenario.enterprise_id,
                     "simulation", sim_id, details={"kind": body.kind})
    seed = body.seed if body.seed is not None else scenario.seed
    runs = body.runs if body.runs is not None else scenario.monte_carlo_runs

    def work(job) -> None:
        context.db.update_simulation(sim_id, status="running")

        def progress(event: dict[str, Any]) -> None:
            job.publish(event)

        if body.kind == "single":
            result = simulate(model, scenario, seed=seed, control=job.control,
                              progress_cb=progress)
            payload = {"kind": "single", "result": result.to_dict()}
            status = result.status if result.status != "truncated" else "completed"
        else:
            mc = run_monte_carlo(model, scenario, runs=runs, master_seed=seed,
                                 control=job.control, progress_cb=progress)
            payload = {"kind": "monte_carlo", "result": mc.to_dict()}
            status = "cancelled" if job.control.cancelled else "completed"
        context.db.update_simulation(sim_id, status=status, result=payload, finished=True)
        if job.control.cancelled:
            job.status = "cancelled"

    context.jobs.start(sim_id, body.kind, work)
    return {"id": sim_id, "status": "accepted", "kind": body.kind}


@router.get("/simulations", dependencies=[VIEW])
def list_simulations(
    request: Request, enterprise_id: str | None = None, scenario_id: str | None = None
) -> list[dict[str, Any]]:
    return ctx(request).db.list_simulations(enterprise_id, scenario_id)


@router.get("/simulations/compare", dependencies=[VIEW])
def compare_simulations(request: Request, ids: str) -> dict[str, Any]:
    context = ctx(request)
    entries = []
    for sim_id in [s for s in ids.split(",") if s]:
        record = context.db.get_simulation(sim_id)
        if record is None or not record.get("result"):
            raise HTTPException(status_code=404, detail=f"simulation {sim_id!r} has no result")
        mc = _as_mc(record)
        scenario = context.db.load_scenario(record["scenario_id"])
        label = scenario.name if scenario else record["scenario_id"]
        entries.append((f"{label} [{sim_id[-6:]}]", mc))
    return compare_results(entries)


@router.get("/simulations/{sim_id}", dependencies=[VIEW])
def get_simulation(request: Request, sim_id: str) -> dict[str, Any]:
    context = ctx(request)
    record = context.db.get_simulation(sim_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"simulation {sim_id!r} not found")
    job = context.jobs.get(sim_id)
    out = {k: v for k, v in record.items() if k != "result"}
    out["has_result"] = record.get("result") is not None
    if record.get("result"):
        provenance = record["result"].get("result", {}).get("provenance")
        out["provenance"] = provenance
    if job:
        out["progress"] = job.progress
        out["job_status"] = job.status
    return out


def _record_or_404(context, sim_id: str) -> dict[str, Any]:
    record = context.db.get_simulation(sim_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"simulation {sim_id!r} not found")
    return record


def _result_payload(record: dict[str, Any]) -> dict[str, Any]:
    if not record.get("result"):
        raise HTTPException(status_code=409, detail="simulation has no result yet")
    return record["result"]


def _as_mc(record: dict[str, Any]) -> MonteCarloResult:
    payload = _result_payload(record)
    if payload["kind"] == "monte_carlo":
        return MonteCarloResult.from_dict(payload["result"])
    return wrap_single(SimulationResult.from_dict(payload["result"]))


def _sample_result(record: dict[str, Any]) -> SimulationResult:
    payload = _result_payload(record)
    if payload["kind"] == "single":
        return SimulationResult.from_dict(payload["result"])
    sample = payload["result"].get("sample_result")
    if not sample:
        raise HTTPException(status_code=409, detail="no sample run stored")
    return SimulationResult.from_dict(sample)


@router.post("/simulations/{sim_id}/pause", dependencies=[EDIT])
def pause_simulation(request: Request, sim_id: str) -> dict[str, Any]:
    if not ctx(request).jobs.pause(sim_id):
        raise HTTPException(status_code=409, detail="simulation is not running")
    return {"status": "paused"}


@router.post("/simulations/{sim_id}/resume", dependencies=[EDIT])
def resume_simulation(request: Request, sim_id: str) -> dict[str, Any]:
    if not ctx(request).jobs.resume(sim_id):
        raise HTTPException(status_code=409, detail="simulation is not paused")
    return {"status": "running"}


@router.post("/simulations/{sim_id}/cancel", dependencies=[EDIT])
def cancel_simulation(request: Request, sim_id: str) -> dict[str, Any]:
    context = ctx(request)
    _record_or_404(context, sim_id)
    if not context.jobs.cancel(sim_id):
        raise HTTPException(status_code=409, detail="simulation is not active")
    context.db.update_simulation(sim_id, status="cancelled", finished=True)
    return {"status": "cancelling"}


@router.get("/simulations/{sim_id}/state", dependencies=[VIEW])
def simulation_state(request: Request, sim_id: str) -> dict[str, Any]:
    context = ctx(request)
    record = _record_or_404(context, sim_id)
    job = context.jobs.get(sim_id)
    if job and record["status"] in ("pending", "running", "paused"):
        return {"status": job.status, "progress": job.progress}
    result = _sample_result(record)
    return {
        "status": record["status"],
        "final_stocks": result.final_stocks,
        "final_kpis": result.final_kpis,
        "metrics": result.metrics,
        "goal_achievement": result.goal_achievement,
    }


@router.get("/simulations/{sim_id}/events", dependencies=[VIEW])
def simulation_events(request: Request, sim_id: str, limit: int = 1000) -> dict[str, Any]:
    record = _record_or_404(ctx(request), sim_id)
    result = _sample_result(record)
    return {
        "event_log": result.event_log[: min(limit, len(result.event_log))],
        "truncated": result.event_log_truncated,
    }


@router.get("/simulations/{sim_id}/timeline", dependencies=[VIEW])
def simulation_timeline(request: Request, sim_id: str) -> dict[str, Any]:
    record = _record_or_404(ctx(request), sim_id)
    result = _sample_result(record)
    return {
        "kpi_series": {k: [[t, v] for t, v in s] for k, s in result.kpi_series.items()},
        "stock_series": {k: [[t, v] for t, v in s] for k, s in result.stock_series.items()},
        "pool_series": [[t, w, e] for t, w, e in result.pool_series],
        "sed_stats": result.sed_stats,
        "resource_stats": result.resource_stats,
    }


@router.get("/simulations/{sim_id}/results", dependencies=[VIEW])
def simulation_results(request: Request, sim_id: str) -> dict[str, Any]:
    record = _record_or_404(ctx(request), sim_id)
    return _result_payload(record)


@router.get("/simulations/{sim_id}/decisions", dependencies=[VIEW])
def simulation_decisions(request: Request, sim_id: str, limit: int = 2000) -> dict[str, Any]:
    """Журнал моментов принятия решений (МР) с длиной интервала между ними."""
    record = _record_or_404(ctx(request), sim_id)
    result = _sample_result(record)
    log = result.decision_log
    return {
        "decision_log": log[: min(limit, len(log))],
        "decision_moments": result.metrics.get("decision_moments", 0),
        "switching_moments": result.metrics.get("switching_moments", 0),
        "truncated": len(log) > limit,
    }


@router.get("/simulations/{sim_id}/policy-trace", dependencies=[VIEW])
def simulation_policy_trace(
    request: Request, sim_id: str, limit: int = 2000, policy_id: str | None = None
) -> dict[str, Any]:
    """Трассировка вызовов политик: условие, факт сработки, применённое действие."""
    record = _record_or_404(ctx(request), sim_id)
    result = _sample_result(record)
    trace = result.policy_trace
    if policy_id:
        trace = [entry for entry in trace if entry.get("policy") == policy_id]
    by_loop: dict[str, dict[str, int]] = {}
    for entry in trace:
        bucket = by_loop.setdefault(entry.get("loop", "regulation"), {"calls": 0, "fired": 0})
        bucket["calls"] += 1
        if entry.get("fired"):
            bucket["fired"] += 1
    return {
        "policy_trace": trace[: min(limit, len(trace))],
        "policy_firings": result.metrics.get("policy_firings", 0),
        "by_loop": by_loop,
        "truncated": len(trace) > limit,
    }


@router.get("/verification/benchmarks", dependencies=[VIEW])
def verification_benchmarks(
    request: Request, names: str | None = None, seed: int = 20260101,
    horizon: float | None = None, replications: int = 1,
) -> dict[str, Any]:
    """Аналитические эталоны: M/M/1, M/M/c, D/D/1, дисконтирование, закон Литтла.

    Прогоны строятся на собственных эталонных моделях и не зависят от модели
    предприятия: это проверка самого движка.
    """
    selected = [n.strip() for n in names.split(",") if n.strip()] if names else None
    unknown = [n for n in (selected or []) if n not in BENCHMARKS]
    if unknown:
        raise HTTPException(
            status_code=404, detail=f"unknown benchmarks: {', '.join(unknown)}"
        )
    return run_benchmarks(
        selected, seed=seed, horizon=horizon, replications=max(1, min(10, replications))
    ).to_dict()


@router.get("/simulations/{sim_id}/verification", dependencies=[VIEW])
def simulation_verification(request: Request, sim_id: str) -> dict[str, Any]:
    """Отчёт верификации, приложенный к прогону: инварианты на его данных."""
    context = ctx(request)
    record = _record_or_404(context, sim_id)
    result = _sample_result(record)
    model = context.db.load_model(record["enterprise_id"])
    if model is None:
        raise HTTPException(status_code=404, detail="enterprise model not found")
    return verify_run(result, model).to_dict()


@router.get("/simulations/{sim_id}/economics", dependencies=[VIEW])
def simulation_economics(request: Request, sim_id: str) -> dict[str, Any]:
    """Экономика прогона: NPV, раскладка по статьям и видам начисления."""
    record = _record_or_404(ctx(request), sim_id)
    result = _sample_result(record)
    return {
        "economics": result.economics,
        "control_costs": result.control_costs,
        "warmup": result.warmup,
    }


@router.get("/simulations/{sim_id}/rejected-actions", dependencies=[VIEW])
def simulation_rejected_actions(request: Request, sim_id: str) -> dict[str, Any]:
    """Действия, отклонённые из-за полномочий субъекта или отсутствия субъекта."""
    record = _record_or_404(ctx(request), sim_id)
    result = _sample_result(record)
    by_reason: dict[str, int] = {}
    for entry in result.rejected_actions:
        reason = entry.get("reason", "unknown")
        by_reason[reason] = by_reason.get(reason, 0) + 1
    failed = [
        {"t": e["t"], "action": e["name"], **e.get("details", {})}
        for e in result.event_log
        if e.get("type") == "control_action_failed"
    ]
    return {
        "rejected_actions": result.rejected_actions,
        "by_reason": by_reason,
        "failed_actions": failed,
        "control_costs": result.control_costs,
    }


@router.get("/simulations/{sim_id}/observation-error", dependencies=[VIEW])
def simulation_observation_error(request: Request, sim_id: str) -> dict[str, Any]:
    """Погрешность наблюдения по компонентам состояния: |Y − Ŷ| за прогон."""
    record = _record_or_404(ctx(request), sim_id)
    result = _sample_result(record)
    return {
        "observation_mode": result.metrics.get("observation_mode", "twin"),
        "observations": result.observation_error,
    }


@router.get("/simulations/{sim_id}/bottlenecks", dependencies=[VIEW])
def simulation_bottlenecks(request: Request, sim_id: str) -> list[dict[str, Any]]:
    context = ctx(request)
    record = _record_or_404(context, sim_id)
    result = _sample_result(record)
    model = context.db.load_model(record["enterprise_id"])
    return analyze_bottlenecks(result, model)


@router.get("/simulations/{sim_id}/stream", dependencies=[VIEW])
async def simulation_stream(request: Request, sim_id: str) -> StreamingResponse:
    """SSE stream of simulation progress events."""
    context = ctx(request)
    record = _record_or_404(context, sim_id)
    job = context.jobs.get(sim_id)

    async def generator():
        if job is None:
            yield f"data: {json.dumps({'status': record['status'], 'done': True})}\n\n"
            return
        q = job.subscribe()
        try:
            while True:
                try:
                    event = await asyncio.to_thread(q.get, True, 15.0)
                except queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                if event.get("done"):
                    break
                if await request.is_disconnected():
                    break
        finally:
            job.unsubscribe(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------- forecast --


@router.get("/forecasts", dependencies=[VIEW])
def get_forecast(
    request: Request, scenario_id: str, simulation_id: str | None = None
) -> dict[str, Any]:
    """Forecast (View 3) from the latest completed simulation of a scenario."""
    context = ctx(request)
    if simulation_id:
        record = _record_or_404(context, simulation_id)
    else:
        completed = [
            r
            for r in context.db.list_simulations(scenario_id=scenario_id)
            if r["status"] == "completed" and r["kind"] in ("single", "monte_carlo")
        ]
        if not completed:
            raise HTTPException(status_code=404,
                                detail="нет завершённых симуляций для сценария")
        record = _record_or_404(context, completed[0]["id"])
    mc = _as_mc(record)
    model = get_model_or_404(context, record["enterprise_id"])
    forecast = build_forecast(model, mc, scenario_id)
    forecast["simulation_id"] = record["id"]
    return forecast


@router.get("/forecasts/deviations", dependencies=[VIEW])
def forecast_deviations(request: Request, scenario_id: str) -> list[dict[str, Any]]:
    forecast = get_forecast(request, scenario_id)
    context = ctx(request)
    model = get_model_or_404(context, forecast["provenance"].get("enterprise_id", ""))
    assistant = AIAssistant(model, build_graph(model))
    return assistant.analyze_deviations(forecast)


# ----------------------------------------------------------------- optimization --


class OptimizationRequest(BaseModel):
    scenario_id: str
    variables: list[dict[str, Any]] = Field(min_length=1)
    objective_kpi_id: str | None = None
    objective_expression: str | None = None
    direction: str = "maximize"
    constraints: list[str] = Field(default_factory=list)
    runs_per_evaluation: int = Field(default=5, ge=1, le=100)
    seed: int = 0
    optimizer: str = "grid_search"
    label: str = ""


@router.post("/optimizations", dependencies=[EDIT], status_code=202)
def start_optimization(request: Request, body: OptimizationRequest) -> dict[str, Any]:
    context = ctx(request)
    simulation_limiter.check(actor_of(request))
    model, scenario = _load_for_run(context, body.scenario_id)
    try:
        problem = OptimizationProblem(
            variables=[DecisionVariable.model_validate(v) for v in body.variables],
            objective_kpi_id=body.objective_kpi_id,
            objective_expression=body.objective_expression,
            direction=KPIDirection(body.direction),
            constraints=[Constraint(expression=c) for c in body.constraints],
            runs_per_evaluation=body.runs_per_evaluation,
            seed=body.seed,
        )
        problem.build_objective()
        for var in problem.variables:
            var.validate_path()
        optimizer = get_optimizer(body.optimizer)
    except (ValidationError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    opt_id = new_id("opt")
    context.db.create_simulation(
        opt_id, scenario.enterprise_id, scenario.id, "optimization", label=body.label
    )
    context.db.audit(actor_of(request), "optimization_started", scenario.enterprise_id,
                     "optimization", opt_id)

    def work(job) -> None:
        context.db.update_simulation(opt_id, status="running")
        result = optimizer.optimize(
            model, scenario, problem, control=job.control, progress_cb=job.publish
        )
        status = "cancelled" if result.status == "cancelled" else "completed"
        context.db.update_simulation(
            opt_id, status=status,
            result={"kind": "optimization", "result": result.to_dict()},
            finished=True,
        )

    context.jobs.start(opt_id, "optimization", work)
    return {"id": opt_id, "status": "accepted"}


@router.get("/optimizations/{opt_id}", dependencies=[VIEW])
def get_optimization(request: Request, opt_id: str) -> dict[str, Any]:
    context = ctx(request)
    record = _record_or_404(context, opt_id)
    job = context.jobs.get(opt_id)
    out = {k: v for k, v in record.items() if k != "result"}
    if job and record["status"] in ("pending", "running", "paused"):
        out["progress"] = job.progress
    if record.get("result"):
        out["result"] = record["result"]["result"]
    return out
