"""Database facade: repositories for enterprises, scenarios, simulations,
model versions, audit trail and time series."""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import create_engine, delete, insert, select, update
from sqlalchemy.engine import Engine

from dtb.domain.base import utcnow_iso
from dtb.domain.model import EnterpriseModel
from dtb.domain.scenario import Scenario
from dtb.persistence import tables as t


def _default_url() -> str:
    url = os.environ.get("DTB_DATABASE_URL")
    if url:
        return url
    data_dir = os.environ.get("DTB_DATA_DIR", os.path.join(os.getcwd(), "data"))
    os.makedirs(data_dir, exist_ok=True)
    return f"sqlite:///{os.path.join(data_dir, 'dtb.db')}"


class Database:
    def __init__(self, url: str | None = None, engine: Engine | None = None):
        self.engine = engine or create_engine(
            url or _default_url(),
            future=True,
            connect_args={"check_same_thread": False}
            if (url or _default_url()).startswith("sqlite")
            else {},
        )
        t.metadata.create_all(self.engine)

    # ------------------------------------------------------------- enterprises --

    def save_model(self, model: EnterpriseModel) -> None:
        snapshot = model.to_snapshot()
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(t.enterprises.c.id).where(t.enterprises.c.id == model.enterprise.id)
            ).first()
            if existing:
                conn.execute(
                    update(t.enterprises)
                    .where(t.enterprises.c.id == model.enterprise.id)
                    .values(name=model.enterprise.name, model=snapshot, updated_at=utcnow_iso())
                )
            else:
                conn.execute(
                    insert(t.enterprises).values(
                        id=model.enterprise.id,
                        name=model.enterprise.name,
                        model=snapshot,
                        updated_at=utcnow_iso(),
                    )
                )

    def load_model(self, enterprise_id: str) -> EnterpriseModel | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.enterprises.c.model).where(t.enterprises.c.id == enterprise_id)
            ).first()
        return EnterpriseModel.from_snapshot(row.model) if row else None

    def list_enterprises(self) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(t.enterprises.c.id, t.enterprises.c.name, t.enterprises.c.updated_at)
            ).all()
        return [dict(r._mapping) for r in rows]

    def delete_enterprise(self, enterprise_id: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(
                delete(t.enterprises).where(t.enterprises.c.id == enterprise_id)
            )
            for table in (t.scenarios, t.simulations, t.model_versions, t.series):
                conn.execute(delete(table).where(table.c.enterprise_id == enterprise_id))
        return result.rowcount > 0

    # ----------------------------------------------------------------- versions --

    def save_version(
        self, model: EnterpriseModel, author: str = "", comment: str = ""
    ) -> int:
        version = model.enterprise.model_version
        with self.engine.begin() as conn:
            conn.execute(
                insert(t.model_versions).values(
                    enterprise_id=model.enterprise.id,
                    version=version,
                    snapshot=model.to_snapshot(),
                    content_hash=model.content_hash(),
                    author=author,
                    comment=comment,
                    created_at=utcnow_iso(),
                )
            )
        return version

    def list_versions(self, enterprise_id: str) -> list[dict[str, Any]]:
        with self.engine.connect() as conn:
            rows = conn.execute(
                select(
                    t.model_versions.c.version,
                    t.model_versions.c.content_hash,
                    t.model_versions.c.author,
                    t.model_versions.c.comment,
                    t.model_versions.c.created_at,
                )
                .where(t.model_versions.c.enterprise_id == enterprise_id)
                .order_by(t.model_versions.c.version.desc(), t.model_versions.c.pk.desc())
            ).all()
        return [dict(r._mapping) for r in rows]

    def get_version_snapshot(self, enterprise_id: str, version: int) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.model_versions.c.snapshot)
                .where(
                    (t.model_versions.c.enterprise_id == enterprise_id)
                    & (t.model_versions.c.version == version)
                )
                .order_by(t.model_versions.c.pk.desc())
            ).first()
        return row.snapshot if row else None

    # ---------------------------------------------------------------- scenarios --

    def save_scenario(self, scenario: Scenario) -> None:
        data = scenario.model_dump(mode="json")
        with self.engine.begin() as conn:
            existing = conn.execute(
                select(t.scenarios.c.id).where(t.scenarios.c.id == scenario.id)
            ).first()
            if existing:
                conn.execute(
                    update(t.scenarios)
                    .where(t.scenarios.c.id == scenario.id)
                    .values(data=data, enterprise_id=scenario.enterprise_id, updated_at=utcnow_iso())
                )
            else:
                conn.execute(
                    insert(t.scenarios).values(
                        id=scenario.id,
                        enterprise_id=scenario.enterprise_id,
                        data=data,
                        updated_at=utcnow_iso(),
                    )
                )

    def load_scenario(self, scenario_id: str) -> Scenario | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.scenarios.c.data).where(t.scenarios.c.id == scenario_id)
            ).first()
        return Scenario.model_validate(row.data) if row else None

    def list_scenarios(self, enterprise_id: str | None = None) -> list[dict[str, Any]]:
        stmt = select(t.scenarios.c.id, t.scenarios.c.enterprise_id, t.scenarios.c.data, t.scenarios.c.updated_at)
        if enterprise_id:
            stmt = stmt.where(t.scenarios.c.enterprise_id == enterprise_id)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [dict(r._mapping) for r in rows]

    def delete_scenario(self, scenario_id: str) -> bool:
        with self.engine.begin() as conn:
            result = conn.execute(delete(t.scenarios).where(t.scenarios.c.id == scenario_id))
        return result.rowcount > 0

    # -------------------------------------------------------------- simulations --

    def create_simulation(
        self,
        sim_id: str,
        enterprise_id: str,
        scenario_id: str,
        kind: str,
        label: str = "",
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(t.simulations).values(
                    id=sim_id,
                    enterprise_id=enterprise_id,
                    scenario_id=scenario_id,
                    kind=kind,
                    status="pending",
                    label=label,
                    created_at=utcnow_iso(),
                )
            )

    def update_simulation(
        self,
        sim_id: str,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        finished: bool = False,
    ) -> None:
        values: dict[str, Any] = {}
        if status is not None:
            values["status"] = status
        if result is not None:
            values["result"] = result
        if finished:
            values["finished_at"] = utcnow_iso()
        if not values:
            return
        with self.engine.begin() as conn:
            conn.execute(
                update(t.simulations).where(t.simulations.c.id == sim_id).values(**values)
            )

    def get_simulation(self, sim_id: str) -> dict[str, Any] | None:
        with self.engine.connect() as conn:
            row = conn.execute(
                select(t.simulations).where(t.simulations.c.id == sim_id)
            ).first()
        return dict(row._mapping) if row else None

    def list_simulations(
        self, enterprise_id: str | None = None, scenario_id: str | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(
            t.simulations.c.id,
            t.simulations.c.enterprise_id,
            t.simulations.c.scenario_id,
            t.simulations.c.kind,
            t.simulations.c.status,
            t.simulations.c.label,
            t.simulations.c.created_at,
            t.simulations.c.finished_at,
        ).order_by(t.simulations.c.created_at.desc())
        if enterprise_id:
            stmt = stmt.where(t.simulations.c.enterprise_id == enterprise_id)
        if scenario_id:
            stmt = stmt.where(t.simulations.c.scenario_id == scenario_id)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [dict(r._mapping) for r in rows]

    # -------------------------------------------------------------------- audit --

    def audit(
        self,
        actor: str,
        action: str,
        enterprise_id: str = "",
        entity_type: str = "",
        entity_id: str = "",
        details: dict[str, Any] | None = None,
    ) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                insert(t.audit_log).values(
                    ts=utcnow_iso(),
                    actor=actor,
                    action=action,
                    enterprise_id=enterprise_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    details=details,
                )
            )

    def audit_entries(
        self, enterprise_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        stmt = select(t.audit_log).order_by(t.audit_log.c.pk.desc()).limit(limit)
        if enterprise_id:
            stmt = stmt.where(t.audit_log.c.enterprise_id == enterprise_id)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [dict(r._mapping) for r in rows]

    # ------------------------------------------------------------------- series --

    def append_series(
        self,
        enterprise_id: str,
        kind: str,
        indicator: str,
        points: list[tuple[float, float]],
        source: str = "",
        replace: bool = True,
    ) -> None:
        with self.engine.begin() as conn:
            if replace:
                conn.execute(
                    delete(t.series).where(
                        (t.series.c.enterprise_id == enterprise_id)
                        & (t.series.c.kind == kind)
                        & (t.series.c.indicator == indicator)
                    )
                )
            if points:
                conn.execute(
                    insert(t.series),
                    [
                        {
                            "enterprise_id": enterprise_id,
                            "kind": kind,
                            "indicator": indicator,
                            "t": p[0],
                            "value": p[1],
                            "source": source,
                        }
                        for p in points
                    ],
                )

    def get_series(
        self, enterprise_id: str, kind: str | None = None, indicator: str | None = None
    ) -> list[dict[str, Any]]:
        stmt = select(
            t.series.c.kind, t.series.c.indicator, t.series.c.t, t.series.c.value, t.series.c.source
        ).where(t.series.c.enterprise_id == enterprise_id).order_by(
            t.series.c.indicator, t.series.c.t
        )
        if kind:
            stmt = stmt.where(t.series.c.kind == kind)
        if indicator:
            stmt = stmt.where(t.series.c.indicator == indicator)
        with self.engine.connect() as conn:
            rows = conn.execute(stmt).all()
        return [dict(r._mapping) for r in rows]
