"""Persistence: transactional storage (SQLAlchemy), versioning, audit, series.

SQLite by default (``DTB_DATABASE_URL`` overrides — PostgreSQL-ready, ADR-0002).
"""

from dtb.persistence.db import Database
from dtb.persistence.versioning import diff_snapshots

__all__ = ["Database", "diff_snapshots"]
