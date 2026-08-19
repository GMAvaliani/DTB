"""SQLAlchemy table definitions (portable across SQLite/PostgreSQL)."""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
)

metadata = MetaData()

enterprises = Table(
    "enterprises",
    metadata,
    Column("id", String(200), primary_key=True),
    Column("name", String(500), nullable=False, default=""),
    Column("model", JSON, nullable=False),  # current working EnterpriseModel snapshot
    Column("updated_at", String(40), nullable=False),
)

model_versions = Table(
    "model_versions",
    metadata,
    Column("pk", Integer, primary_key=True, autoincrement=True),
    Column("enterprise_id", String(200), nullable=False, index=True),
    Column("version", Integer, nullable=False),
    Column("snapshot", JSON, nullable=False),
    Column("content_hash", String(64), nullable=False),
    Column("author", String(200), nullable=False, default=""),
    Column("comment", Text, nullable=False, default=""),
    Column("created_at", String(40), nullable=False),
)

scenarios = Table(
    "scenarios",
    metadata,
    Column("id", String(200), primary_key=True),
    Column("enterprise_id", String(200), nullable=False, index=True),
    Column("data", JSON, nullable=False),
    Column("updated_at", String(40), nullable=False),
)

simulations = Table(
    "simulations",
    metadata,
    Column("id", String(200), primary_key=True),
    Column("enterprise_id", String(200), nullable=False, index=True),
    Column("scenario_id", String(200), nullable=False, index=True),
    Column("kind", String(30), nullable=False, default="single"),  # single|monte_carlo|optimization
    Column("status", String(30), nullable=False, default="pending"),
    Column("label", String(500), nullable=False, default=""),
    Column("result", JSON, nullable=True),
    Column("created_at", String(40), nullable=False),
    Column("finished_at", String(40), nullable=True),
)

audit_log = Table(
    "audit_log",
    metadata,
    Column("pk", Integer, primary_key=True, autoincrement=True),
    Column("ts", String(40), nullable=False),
    Column("actor", String(200), nullable=False, default=""),
    Column("action", String(100), nullable=False),
    Column("enterprise_id", String(200), nullable=False, default="", index=True),
    Column("entity_type", String(100), nullable=False, default=""),
    Column("entity_id", String(200), nullable=False, default=""),
    Column("details", JSON, nullable=True),
)

# Dedicated time-series storage (actual / plan / forecast), swappable backend.
series = Table(
    "series",
    metadata,
    Column("pk", Integer, primary_key=True, autoincrement=True),
    Column("enterprise_id", String(200), nullable=False, index=True),
    Column("kind", String(20), nullable=False),  # actual | plan | forecast
    Column("indicator", String(200), nullable=False, index=True),
    Column("t", Float, nullable=False),
    Column("value", Float, nullable=False),
    Column("source", String(200), nullable=False, default=""),
)
