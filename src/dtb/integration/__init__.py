"""ERP integration layer: ERP → Connector → Normalization → Domain Model → Twin.

The ERP is a *source of atomic data* (views 4–7), never re-implemented here.
Shipped connectors: CSV/Excel/JSON import, a mock ERP connector (realistic
fixture data through the real normalization pipeline) and a generic REST
connector. 1С/Галактика/MES/… adapters implement the same `ERPConnector`
interface (see ERP_INTEGRATION.md).
"""

from dtb.integration.base import ERPConnector, ImportReport, sync_connector
from dtb.integration.csv_import import import_csv
from dtb.integration.excel_import import export_workbook, import_workbook
from dtb.integration.json_import import export_graph_json, export_model_json, import_model_json
from dtb.integration.mock_erp import MockERPConnector
from dtb.integration.rest_connector import RESTConnector

__all__ = [
    "ERPConnector",
    "ImportReport",
    "sync_connector",
    "import_csv",
    "import_workbook",
    "export_workbook",
    "import_model_json",
    "export_model_json",
    "export_graph_json",
    "MockERPConnector",
    "RESTConnector",
]
