"""Стыковка с другими движками имитации деятельности.

Контракт «модель + политика + сиды → траектория, экономика, журналы» и
адаптеры к нему: наш движок, внешний движок по URI, движок-функция.
"""

from dtb.interop.adapters import (
    CallableEngineAdapter,
    DTBEngineAdapter,
    HTTPEngineAdapter,
)
from dtb.interop.certify import certify, compare_engines
from dtb.interop.contract import (
    CAPABILITIES,
    CONTRACT_VERSION,
    REQUIRED_ECONOMICS_FIELDS,
    REQUIRED_RUN_FIELDS,
    REQUIRED_TRAJECTORY_FIELDS,
    ContractError,
    ContractRequest,
    EngineAdapter,
    validate_request,
    validate_response,
)

__all__ = [
    "CAPABILITIES",
    "CONTRACT_VERSION",
    "REQUIRED_ECONOMICS_FIELDS",
    "REQUIRED_RUN_FIELDS",
    "REQUIRED_TRAJECTORY_FIELDS",
    "CallableEngineAdapter",
    "ContractError",
    "ContractRequest",
    "DTBEngineAdapter",
    "EngineAdapter",
    "HTTPEngineAdapter",
    "certify",
    "compare_engines",
    "validate_request",
    "validate_response",
]
