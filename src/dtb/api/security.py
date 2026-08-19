"""API security: optional API-key auth with roles, tenant scoping, rate limiting.

``DTB_API_KEYS`` = ``key:role[:enterprise_id],key2:role2,...`` where role ∈
{viewer, editor, admin}. When unset the server runs in open development mode
(every request acts as admin) and logs a warning at startup. A key scoped to an
enterprise id may only touch that enterprise (tenant isolation).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

from fastapi import Header, HTTPException, Request

_ROLE_LEVEL = {"viewer": 1, "editor": 2, "admin": 3}


@dataclass
class Principal:
    actor: str
    role: str
    enterprise_scope: str | None = None

    def level(self) -> int:
        return _ROLE_LEVEL.get(self.role, 0)


def _parse_keys() -> dict[str, Principal]:
    raw = os.environ.get("DTB_API_KEYS", "").strip()
    if not raw:
        return {}
    out: dict[str, Principal] = {}
    for index, chunk in enumerate(raw.split(",")):
        parts = chunk.strip().split(":")
        if len(parts) < 2 or parts[1] not in _ROLE_LEVEL:
            continue
        out[parts[0]] = Principal(
            actor=f"key_{index}_{parts[1]}",
            role=parts[1],
            enterprise_scope=parts[2] if len(parts) > 2 and parts[2] else None,
        )
    return out


def auth_enabled() -> bool:
    return bool(_parse_keys())


def resolve_principal(x_api_key: str | None) -> Principal:
    keys = _parse_keys()
    if not keys:
        return Principal(actor="dev", role="admin")
    if not x_api_key or x_api_key not in keys:
        raise HTTPException(status_code=401, detail="invalid or missing API key")
    return keys[x_api_key]


def require_role(minimum: str):
    """FastAPI dependency: authenticate + check role level."""

    async def dependency(
        request: Request, x_api_key: str | None = Header(default=None)
    ) -> Principal:
        principal = resolve_principal(x_api_key)
        if principal.level() < _ROLE_LEVEL[minimum]:
            raise HTTPException(status_code=403, detail=f"requires role {minimum}")
        eid = request.path_params.get("eid")
        if principal.enterprise_scope and eid and eid != principal.enterprise_scope:
            raise HTTPException(status_code=403, detail="enterprise out of key scope")
        request.state.principal = principal
        return principal

    return dependency


class RateLimiter:
    """Simple in-memory sliding-window limiter (per actor + bucket)."""

    def __init__(self, limit: int, window_seconds: float = 60.0):
        self.limit = limit
        self.window = window_seconds
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.limit:
                raise HTTPException(status_code=429, detail="rate limit exceeded")
            hits.append(now)
            self._hits[key] = hits


import_limiter = RateLimiter(limit=30)
simulation_limiter = RateLimiter(limit=60)
