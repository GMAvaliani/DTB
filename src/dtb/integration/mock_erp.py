"""Mock ERP connector — fixture data through the *real* normalization pipeline.

Simulates an external ERP (1С-like structure) for demos and tests: material
stocks, employees, equipment, org units and KPI actuals. It is an honest
connector implementation, not a faked integration: replace `fetch` with real
system calls to build a production adapter.
"""

from __future__ import annotations

from typing import Any


class MockERPConnector:
    system = "mock_1c"

    def __init__(self, dataset: dict[str, list[dict[str, Any]]] | None = None):
        self._dataset = dataset if dataset is not None else _DEFAULT_DATASET

    def fetch(self) -> dict[str, list[dict[str, Any]]]:
        return self._dataset


_DEFAULT_DATASET: dict[str, list[dict[str, Any]]] = {
    "org_unit": [
        {"id": "ou_sales", "name": "Отдел продаж", "kind": "department"},
        {"id": "ou_production", "name": "Производство", "kind": "department"},
        {"id": "ou_logistics", "name": "Логистика", "kind": "department"},
    ],
    "employee": [
        {"id": "emp_ivanova", "name": "Иванова А.П.", "org_unit_id": "ou_sales",
         "position": "Менеджер по продажам", "skills": ["sales"], "resource_id": "sales_team"},
        {"id": "emp_petrov", "name": "Петров С.Н.", "org_unit_id": "ou_production",
         "position": "Оператор линии", "skills": ["assembly"], "resource_id": "production_line"},
        {"id": "emp_sidorov", "name": "Сидоров В.К.", "org_unit_id": "ou_logistics",
         "position": "Водитель", "skills": ["delivery"], "resource_id": "delivery_trucks"},
    ],
    "equipment": [
        {"id": "eq_line_1", "name": "Сборочная линия №1", "tech_place": "Цех 1 / ТМ-01",
         "org_unit_id": "ou_production", "resource_id": "production_line"},
        {"id": "eq_line_2", "name": "Сборочная линия №2", "tech_place": "Цех 1 / ТМ-02",
         "org_unit_id": "ou_production", "resource_id": "production_line"},
    ],
    "material_stock": [
        {"utility_type_id": "raw_material", "quantity": 800, "warehouse": "Склад сырья"},
        {"utility_type_id": "components", "quantity": 400, "warehouse": "Склад комплектующих"},
    ],
    "kpi_actual": [
        {"kpi_id": "revenue", "t": -168.0, "value": 41000.0},
        {"kpi_id": "revenue", "t": -72.0, "value": 22500.0},
        {"kpi_id": "revenue", "t": 0.0, "value": 0.0},
    ],
}
