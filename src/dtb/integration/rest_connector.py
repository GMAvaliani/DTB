"""Generic REST connector: pulls JSON collections from HTTP endpoints.

Configuration maps ERP object types to URLs (+ optional field mapping), e.g.::

    RESTConnector(
        system="my_erp",
        endpoints={
            "material_stock": {"url": "https://erp.local/api/stocks",
                                "items_path": "data",
                                "field_map": {"sku": "utility_type_id", "qty": "quantity"}},
            "employee": {"url": "https://erp.local/api/staff"},
        },
        headers={"Authorization": "Bearer ..."},
    )

The response may be a JSON array or an object holding the array under
``items_path`` (dot-separated). Fetched rows flow through the same
normalization pipeline as any other connector.
"""

from __future__ import annotations

from typing import Any

import httpx


class RESTConnector:
    def __init__(
        self,
        system: str,
        endpoints: dict[str, dict[str, Any]],
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ):
        self.system = system
        self.endpoints = endpoints
        self.headers = headers or {}
        self.timeout_seconds = timeout_seconds

    def fetch(self) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for object_type, spec in self.endpoints.items():
            url = spec["url"]
            response = httpx.get(url, headers=self.headers, timeout=self.timeout_seconds)
            response.raise_for_status()
            payload = response.json()
            items = payload
            for part in (spec.get("items_path") or "").split("."):
                if part:
                    items = items[part]
            if not isinstance(items, list):
                raise ValueError(f"{object_type}: endpoint must return a JSON array")
            field_map: dict[str, str] = spec.get("field_map") or {}
            rows = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                row = dict(item)
                for src, dst in field_map.items():
                    if src in row:
                        row[dst] = row.pop(src)
                rows.append(row)
            out[object_type] = rows
        return out
