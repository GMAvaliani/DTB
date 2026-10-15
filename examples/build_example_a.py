"""Пример А: простейшее предприятие из одного СЭДа — сборка Excel-книги.

Запуск:  python examples/build_example_a.py
Результат: examples/example_A_one_sed.xlsx — книгу можно импортировать в
платформу (Импорт / Экспорт → Импорт Excel) или прочитать как образец.
"""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook

SHEETS: dict[str, list[list[object]]] = {
    "utility_types": [
        ["id", "name", "kind", "unit"],
        ["order", "Заказ", "information", "шт"],
        ["money", "Денежные средства", "money", "руб"],
    ],
    "resources": [
        ["id", "name", "kind", "capacity", "unit"],
        ["master", "Мастер", "personnel", 1, "чел"],
    ],
    "event_kinds": [
        ["id", "name", "category", "stream_kind", "rate_per_hour", "effects"],
        ["order_in", "Поступил заказ", "external", "poisson", 0.4,
         json.dumps([{"kind": "add_utility", "utility_type_id": "order",
                      "quantity_expression": "1"}], ensure_ascii=False)],
    ],
    "seds": [
        ["id", "name", "inputs", "outputs", "resources", "duration", "triggers"],
        ["service", "Выполнение заказа", "order:1", "money:5000", "master:1",
         "triangular:1..3@2", "on_event:order_in"],
    ],
    "kpis": [
        ["id", "name", "expression", "unit"],
        ["revenue", "Выручка", "produced('money')", "руб"],
        ["done", "Выполнено заказов", "completed('service')", "шт"],
        ["load", "Загрузка мастера", "100 * utilization('master')", "%"],
        ["queue", "Заказов в очереди", "waiting_count('service')", "шт"],
    ],
    "goals": [
        ["id", "name", "targets", "sed_ids"],
        ["g_revenue", "Выручка за месяц", "revenue:>=:1000000", "service"],
    ],
    "economic_items": [
        ["id", "name", "article", "kind", "basis", "direction", "ref", "rate"],
        ["income", "Оплата заказов", "выручка", "one_time", "utility_produced",
         "income", "money", 1],
        ["wage", "Оплата мастера", "фонд оплаты труда", "rate",
         "resource_capacity", "expense", "master", 400],
    ],
    "economics": [
        ["currency", "discount_rate_annual", "warmup_hours"],
        ["руб", 0.15, 0],
    ],
}


def build(path: Path) -> Path:
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in SHEETS.items():
        ws = wb.create_sheet(title=title)
        for row in rows:
            ws.append(row)
        ws.freeze_panes = "A2"
    wb.save(path)
    return path


if __name__ == "__main__":
    out = build(Path(__file__).with_name("example_A_one_sed.xlsx"))
    print(f"Книга сохранена: {out}")
