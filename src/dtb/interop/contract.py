"""Контракт стыковки движков имитации деятельности.

Назначение контракта — дать двум независимо сделанным движкам общий язык:

    модель + политика + сиды  →  траектория, экономика, журналы

Контракт не предписывает устройство движка. Он фиксирует, **что** подаётся на
вход и **что** должно вернуться, поэтому к нему можно подключить и другой
имитатор деятельности предприятия: если он отвечает по этому контракту, его
результат сравним с нашим на тех же входных данных и тех же сидах.

Версия контракта меняется только при несовместимом изменении полей; поля
добавляются аддитивно, и адаптер обязан игнорировать незнакомые поля запроса,
а не падать.

Запрос (JSON)::

    {"contract_version": "1.0",
     "model": {...},            # снимок модели предприятия (to_snapshot)
     "scenario": {...},         # сценарий: горизонт, потоки, воздействия
     "policies": [{...}],       # политики контура управления (могут быть в модели)
     "seeds": [11, 12, 13],     # по одному прогону на сид
     "options": {"trajectory_points": 60, "include_logs": true}}

Ответ (JSON)::

    {"contract_version": "1.0",
     "engine": {"name": "...", "version": "...", "capabilities": [...]},
     "runs": [
       {"seed": 11,
        "status": "completed",
        "trajectory": {"grid": [...], "kpis": {...}, "stocks": {...},
                       "pools": [[t, waiting, executing], ...]},
        "economics": {"currency": "...", "npv": ..., "net": ...,
                      "by_article": {...}},
        "logs": {"events": [...], "decisions": [...], "policies": [...]},
        "metrics": {...},
        "provenance": {...}}],
     "summary": {"runs": 3, "failed": 0}}

Обязательные поля ответа перечислены в ``REQUIRED_RUN_FIELDS``: без них
результат нельзя ни сравнить, ни проверить, поэтому адаптер считается
несоответствующим контракту.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

CONTRACT_VERSION = "1.0"

#: Поля прогона, без которых ответ не принимается.
REQUIRED_RUN_FIELDS = ("seed", "status", "trajectory", "economics", "metrics")

#: Поля траектории: сетка времени и значения на ней.
REQUIRED_TRAJECTORY_FIELDS = ("grid", "kpis")

#: Поля экономики: итог и его раскладка.
REQUIRED_ECONOMICS_FIELDS = ("npv", "net", "by_article")

#: Возможности, которые адаптер может объявить. Сравнение двух движков
#: опирается на пересечение их возможностей, а не на предположения.
CAPABILITIES = (
    "trajectory",  # траектории KPI на общей сетке
    "economics",  # экономика с раскладкой по статьям
    "logs",  # журналы событий, решений, политик
    "control_loop",  # моменты принятия решений и политики
    "observations",  # слой наблюдений (twin / pure)
    "calendars",  # графики работы
    "lookahead",  # просмотр вперёд
    "reproducible",  # одинаковые входы и сид дают одинаковый результат
)


class ContractError(ValueError):
    """Нарушение контракта: запрос или ответ не соответствуют описанию."""


@dataclass
class ContractRequest:
    """Вход контракта: модель, сценарий, политики, сиды."""

    model: dict[str, Any]
    scenario: dict[str, Any] = field(default_factory=dict)
    policies: list[dict[str, Any]] = field(default_factory=list)
    seeds: list[int] = field(default_factory=lambda: [0])
    options: dict[str, Any] = field(default_factory=dict)
    contract_version: str = CONTRACT_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "model": self.model,
            "scenario": self.scenario,
            "policies": self.policies,
            "seeds": list(self.seeds),
            "options": self.options,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ContractRequest":
        validate_request(data)
        return cls(
            model=data["model"],
            scenario=data.get("scenario") or {},
            policies=list(data.get("policies") or []),
            seeds=[int(s) for s in (data.get("seeds") or [0])],
            options=dict(data.get("options") or {}),
            contract_version=str(data.get("contract_version") or CONTRACT_VERSION),
        )


class EngineAdapter(Protocol):
    """Движок, отвечающий по контракту."""

    name: str

    def capabilities(self) -> list[str]:
        """Что движок умеет — подмножество ``CAPABILITIES``."""
        ...

    def run(self, request: ContractRequest) -> dict[str, Any]:
        """Выполнить прогоны по одному на каждый сид и вернуть ответ контракта."""
        ...


def _major(version: str) -> str:
    return str(version).split(".", 1)[0]


def validate_request(data: dict[str, Any]) -> None:
    """Проверить запрос контракта; поднимает ``ContractError``."""
    if not isinstance(data, dict):
        raise ContractError("запрос контракта должен быть объектом JSON")
    version = str(data.get("contract_version") or CONTRACT_VERSION)
    if _major(version) != _major(CONTRACT_VERSION):
        raise ContractError(
            f"версия контракта {version} несовместима с {CONTRACT_VERSION}"
        )
    model = data.get("model")
    if not isinstance(model, dict) or not model.get("enterprise"):
        raise ContractError("запрос контракта требует model со полем enterprise")
    seeds = data.get("seeds")
    if seeds is not None:
        if not isinstance(seeds, list) or not seeds:
            raise ContractError("seeds должен быть непустым списком целых чисел")
        for seed in seeds:
            if not isinstance(seed, int) or isinstance(seed, bool):
                raise ContractError(f"сид {seed!r} не целое число")
    for key in ("scenario", "options"):
        value = data.get(key)
        if value is not None and not isinstance(value, dict):
            raise ContractError(f"поле {key} должно быть объектом")
    policies = data.get("policies")
    if policies is not None and not isinstance(policies, list):
        raise ContractError("policies должен быть списком объектов")


def validate_response(data: dict[str, Any], request: ContractRequest) -> list[str]:
    """Проверить ответ контракта; возвращает список нарушений (пустой — ок)."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return ["ответ контракта должен быть объектом JSON"]
    version = str(data.get("contract_version") or "")
    if _major(version) != _major(CONTRACT_VERSION):
        problems.append(
            f"версия контракта в ответе {version!r} несовместима с {CONTRACT_VERSION}"
        )
    engine = data.get("engine")
    if not isinstance(engine, dict) or not engine.get("name"):
        problems.append("ответ должен содержать engine.name")
    runs = data.get("runs")
    if not isinstance(runs, list) or not runs:
        problems.append("ответ должен содержать непустой список runs")
        return problems
    if len(runs) != len(request.seeds):
        problems.append(
            f"прогонов в ответе {len(runs)}, а сидов в запросе {len(request.seeds)}"
        )
    seen_seeds = []
    for index, run in enumerate(runs):
        label = f"runs[{index}]"
        if not isinstance(run, dict):
            problems.append(f"{label} должен быть объектом")
            continue
        for key in REQUIRED_RUN_FIELDS:
            if key not in run:
                problems.append(f"{label}: отсутствует поле {key}")
        seen_seeds.append(run.get("seed"))
        trajectory = run.get("trajectory")
        if isinstance(trajectory, dict):
            for key in REQUIRED_TRAJECTORY_FIELDS:
                if key not in trajectory:
                    problems.append(f"{label}.trajectory: отсутствует поле {key}")
            grid = trajectory.get("grid")
            kpis = trajectory.get("kpis")
            if isinstance(grid, list) and isinstance(kpis, dict):
                for kpi_id, series in kpis.items():
                    if not isinstance(series, list) or len(series) != len(grid):
                        problems.append(
                            f"{label}.trajectory.kpis[{kpi_id}]: длина не совпадает "
                            f"с сеткой ({len(grid)})"
                        )
        elif "trajectory" in run:
            problems.append(f"{label}.trajectory должна быть объектом")
        economics = run.get("economics")
        if isinstance(economics, dict):
            for key in REQUIRED_ECONOMICS_FIELDS:
                if key not in economics:
                    problems.append(f"{label}.economics: отсутствует поле {key}")
        elif "economics" in run:
            problems.append(f"{label}.economics должна быть объектом")
    if seen_seeds and [s for s in seen_seeds if isinstance(s, int)] and set(
        s for s in seen_seeds if isinstance(s, int)
    ) != set(request.seeds):
        problems.append(
            f"сиды прогонов {sorted(s for s in seen_seeds if isinstance(s, int))} "
            f"не совпадают с запрошенными {sorted(request.seeds)}"
        )
    return problems
