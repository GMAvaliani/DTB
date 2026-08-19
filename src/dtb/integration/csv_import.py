"""CSV import: one file per entity collection (same columns as the workbook)."""

from __future__ import annotations

import csv
import io

from dtb.domain.model import EnterpriseModel
from dtb.integration.base import ImportReport
from dtb.integration.tabular import apply_rows


def import_csv(
    model: EnterpriseModel,
    sheet: str,
    content: str | bytes,
    delimiter: str | None = None,
) -> ImportReport:
    """Apply one CSV file to the model. `sheet` names the collection
    (goals | seds | utility_types | resources | org_units | employees |
    equipment | event_kinds | kpis | erp_objects | erp_mappings |
    initial_state | plans | actuals)."""
    report = ImportReport(source=f"csv:{sheet}")
    if isinstance(content, bytes):
        content = content.decode("utf-8-sig")
    if delimiter is None:
        # detect from the header line only — data cells legitimately contain ';'
        header_line = content.splitlines()[0] if content.splitlines() else ""
        delimiter = ";" if ";" in header_line and "," not in header_line else ","
    reader = csv.DictReader(io.StringIO(content), delimiter=delimiter)
    if not reader.fieldnames:
        report.error(sheet, "пустой CSV-файл")
        return report
    rows = [
        {(k or "").strip().lower(): v for k, v in row.items()} for row in reader
    ]
    apply_rows(model, sheet, rows, report)
    return report
