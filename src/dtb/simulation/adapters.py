"""External model adapters (Mode B).

A SED's execution — or an NP-event stream — may be computed by an external,
arbitrarily detailed model (CAD/CAE/equipment model/ML service/…) addressed by
URI. The engine consumes only this interface; the core never executes code from
model data (ADR-0004).

HTTP contract (``HTTPModelAdapter``), JSON over POST:

``execute_sed`` request::

    {"op": "execute_sed", "sed_id": ..., "instance_id": ..., "time": ...,
     "payload": {...}, "params": {...}, "seed": ..., "inputs": [...]}

expected response::

    {"duration": <hours>, "outputs": [{"utility_type_id": ..., "quantity": ...}],
     "branch_label": "..."}   # outputs/branch optional

``next_event`` request::

    {"op": "next_event", "event_kind_id": ..., "time": ..., "params": {...},
     "seed": ...}

expected response::

    {"time": <absolute model hours> | null, "payload": {...}}   # null = no more
"""

from __future__ import annotations

import os
from typing import Any, Callable, Protocol

from dtb.domain.seds import ExternalModelRef


class ExternalModelError(RuntimeError):
    """External model unreachable or returned an invalid response.

    By design this fails the simulation run — no silent fallback that would
    fabricate results.
    """


class ExternalModelAdapter(Protocol):
    def execute_sed(self, ref: ExternalModelRef, request: dict[str, Any]) -> dict[str, Any]:
        """Return {"duration": float, "outputs": [...]?, "branch_label": str?}."""
        ...

    def next_event(self, ref: ExternalModelRef, request: dict[str, Any]) -> dict[str, Any]:
        """Return {"time": float | None, "payload": dict}."""
        ...


def _allowed_hosts() -> list[str] | None:
    raw = os.environ.get("DTB_EXTERNAL_MODEL_HOSTS", "").strip()
    if not raw:
        return None  # no allowlist configured — any http(s) host
    return [h.strip() for h in raw.split(",") if h.strip()]


class HTTPModelAdapter:
    """Calls the external model over HTTP(S) with timeouts and a host allowlist."""

    def _post(self, ref: ExternalModelRef, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        from urllib.parse import urlparse

        parsed = urlparse(ref.uri)
        if parsed.scheme not in ("http", "https"):
            raise ExternalModelError(f"URI scheme {parsed.scheme!r} is not allowed")
        allowed = _allowed_hosts()
        if allowed is not None and parsed.hostname not in allowed:
            raise ExternalModelError(
                f"host {parsed.hostname!r} is not in DTB_EXTERNAL_MODEL_HOSTS"
            )
        try:
            resp = httpx.post(ref.uri, json=body, timeout=ref.timeout_seconds)
            resp.raise_for_status()
            data = resp.json()
        except ExternalModelError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalised into domain error
            raise ExternalModelError(f"external model {ref.uri}: {exc}") from exc
        if not isinstance(data, dict):
            raise ExternalModelError(f"external model {ref.uri}: non-object response")
        return data

    def execute_sed(self, ref: ExternalModelRef, request: dict[str, Any]) -> dict[str, Any]:
        return self._post(ref, {"op": "execute_sed", "params": ref.params, **request})

    def next_event(self, ref: ExternalModelRef, request: dict[str, Any]) -> dict[str, Any]:
        return self._post(ref, {"op": "next_event", "params": ref.params, **request})


class CallableAdapter:
    """In-process plugin adapter: functions registered explicitly in code.

    URI form: ``callable:<name>``. Used by tests and bundled models; the API
    never registers callables from user input.
    """

    def __init__(self) -> None:
        self._fns: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}

    def register(self, name: str, fn: Callable[[dict[str, Any]], dict[str, Any]]) -> None:
        self._fns[name] = fn

    def _call(self, ref: ExternalModelRef, request: dict[str, Any]) -> dict[str, Any]:
        name = ref.uri.split(":", 1)[1] if ":" in ref.uri else ref.uri
        fn = self._fns.get(name)
        if fn is None:
            raise ExternalModelError(f"callable model {name!r} is not registered")
        result = fn({"params": ref.params, **request})
        if not isinstance(result, dict):
            raise ExternalModelError(f"callable model {name!r}: non-dict response")
        return result

    def execute_sed(self, ref: ExternalModelRef, request: dict[str, Any]) -> dict[str, Any]:
        return self._call(ref, {"op": "execute_sed", **request})

    def next_event(self, ref: ExternalModelRef, request: dict[str, Any]) -> dict[str, Any]:
        return self._call(ref, {"op": "next_event", **request})


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ExternalModelAdapter] = {}

    def register(self, name: str, adapter: ExternalModelAdapter) -> None:
        self._adapters[name] = adapter

    def get(self, name: str) -> ExternalModelAdapter:
        adapter = self._adapters.get(name)
        if adapter is None:
            raise ExternalModelError(f"no external model adapter named {name!r}")
        return adapter


def default_registry() -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register("http", HTTPModelAdapter())
    registry.register("callable", CallableAdapter())
    return registry
