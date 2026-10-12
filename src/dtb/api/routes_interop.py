"""Маршруты стыковки движков: контракт, прогон по контракту, проверка, сравнение.

Платформа выступает в обеих ролях: отдаёт прогон по контракту (значит, другой
движок может вызвать нас) и вызывает внешний движок по URI (значит, мы можем
вызвать его). Проверка соответствия и сравнение работают с любой из сторон.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from dtb.api.deps import ctx, get_model_or_404
from dtb.api.security import require_role, simulation_limiter
from dtb.api.deps import actor_of
from dtb.interop import (
    CAPABILITIES,
    CONTRACT_VERSION,
    REQUIRED_ECONOMICS_FIELDS,
    REQUIRED_RUN_FIELDS,
    REQUIRED_TRAJECTORY_FIELDS,
    CallableEngineAdapter,
    ContractError,
    ContractRequest,
    DTBEngineAdapter,
    HTTPEngineAdapter,
    certify,
    compare_engines,
)

router = APIRouter()

VIEW = Depends(require_role("viewer"))
EDIT = Depends(require_role("editor"))


class ExternalEngine(BaseModel):
    """Внешний движок: URI и объявленные возможности."""

    uri: str
    name: str = "external"
    timeout: float = Field(default=120.0, gt=0, le=600)
    capabilities: list[str] = Field(default_factory=lambda: ["trajectory", "economics"])


class ContractRunRequest(BaseModel):
    """Прогон по контракту для модели, уже сохранённой в платформе."""

    enterprise_id: str
    scenario_id: str | None = None
    seeds: list[int] = Field(default_factory=lambda: [0], min_length=1, max_length=50)
    horizon_hours: float | None = Field(default=None, gt=0)
    trajectory_points: int = Field(default=60, ge=1, le=2000)
    include_logs: bool = True


class CertifyRequest(ContractRunRequest):
    engine: ExternalEngine | None = None  # пусто — проверяем собственный движок


class CompareRequest(ContractRunRequest):
    engine: ExternalEngine  # с кем сравниваем: второй стороной всегда DTB


@router.get("/interop/contract", dependencies=[VIEW])
def interop_contract(request: Request) -> dict[str, Any]:
    """Описание контракта: версия, обязательные поля, возможности."""
    adapter = DTBEngineAdapter()
    return {
        "contract_version": CONTRACT_VERSION,
        "formula": "модель + политика + сиды → траектория, экономика, журналы",
        "request_fields": {
            "model": "снимок модели предприятия (GET /enterprises/{id}/model)",
            "scenario": "сценарий прогона: горизонт, потоки, воздействия",
            "policies": "политики контура управления (могут быть внутри модели)",
            "seeds": "список целых сидов — по прогону на каждый",
            "options": "trajectory_points, include_logs, log_limit",
        },
        "required_run_fields": list(REQUIRED_RUN_FIELDS),
        "required_trajectory_fields": list(REQUIRED_TRAJECTORY_FIELDS),
        "required_economics_fields": list(REQUIRED_ECONOMICS_FIELDS),
        "capabilities": list(CAPABILITIES),
        "this_engine": {
            "name": adapter.name,
            "capabilities": adapter.capabilities(),
        },
    }


def _build_request(context, body: ContractRunRequest) -> ContractRequest:
    model = get_model_or_404(context, body.enterprise_id)
    scenario: dict[str, Any] = {}
    if body.scenario_id:
        stored = context.db.load_scenario(body.scenario_id)
        if stored is None:
            raise HTTPException(status_code=404, detail="scenario not found")
        if stored.enterprise_id != body.enterprise_id:
            raise HTTPException(status_code=422, detail="scenario belongs to another enterprise")
        scenario = stored.model_dump(mode="json")
    if body.horizon_hours:
        scenario["horizon_hours"] = body.horizon_hours
    return ContractRequest(
        model=model.to_snapshot(),
        scenario=scenario,
        seeds=list(body.seeds),
        options={
            "trajectory_points": body.trajectory_points,
            "include_logs": body.include_logs,
        },
    )


@router.post("/interop/run", dependencies=[VIEW])
def interop_run(request: Request, body: ContractRunRequest) -> dict[str, Any]:
    """Выполнить прогон по контракту нашим движком."""
    context = ctx(request)
    simulation_limiter.check(actor_of(request))
    contract_request = _build_request(context, body)
    try:
        return DTBEngineAdapter().run(contract_request)
    except ContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc))


@router.post("/interop/run-raw", dependencies=[VIEW])
def interop_run_raw(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """Прогон по контракту из внешнего запроса: модель приходит целиком.

    Это серверная сторона контракта: другой движок (или обёртка вокруг него)
    вызывает нас так же, как мы вызываем его.
    """
    simulation_limiter.check(actor_of(request))
    try:
        contract_request = ContractRequest.from_dict(body)
        return DTBEngineAdapter().run(contract_request)
    except ContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 — некорректная модель во входе
        raise HTTPException(status_code=422, detail=f"{type(exc).__name__}: {exc}")


@router.post("/interop/certify", dependencies=[EDIT])
def interop_certify(request: Request, body: CertifyRequest) -> dict[str, Any]:
    """Проверить движок на соответствие контракту (свой либо внешний)."""
    context = ctx(request)
    simulation_limiter.check(actor_of(request))
    contract_request = _build_request(context, body)
    adapter = (
        HTTPEngineAdapter(
            body.engine.uri, name=body.engine.name, timeout=body.engine.timeout,
            capabilities=body.engine.capabilities,
        )
        if body.engine
        else DTBEngineAdapter()
    )
    report = certify(adapter, contract_request)
    context.db.audit(
        actor_of(request), "engine_certified", body.enterprise_id, "engine", adapter.name
    )
    return report


@router.post("/interop/compare", dependencies=[EDIT])
def interop_compare(request: Request, body: CompareRequest) -> dict[str, Any]:
    """Сравнить наш движок с внешним на одних данных и одних сидах."""
    context = ctx(request)
    simulation_limiter.check(actor_of(request))
    contract_request = _build_request(context, body)
    external = HTTPEngineAdapter(
        body.engine.uri, name=body.engine.name, timeout=body.engine.timeout,
        capabilities=body.engine.capabilities,
    )
    try:
        return compare_engines(DTBEngineAdapter(), external, contract_request)
    except ContractError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
