"""Верификация расчёта: инварианты прогона и аналитические эталоны."""

from dtb.verification.benchmarks import BENCHMARKS, run_benchmarks
from dtb.verification.checks import (
    VerificationCheck,
    VerificationReport,
    check_control_costs,
    check_economics_consistency,
    check_little_law,
    check_resource_accounting,
    check_utility_balance,
    verify_run,
)

__all__ = [
    "BENCHMARKS",
    "VerificationCheck",
    "VerificationReport",
    "check_control_costs",
    "check_economics_consistency",
    "check_little_law",
    "check_resource_accounting",
    "check_utility_balance",
    "run_benchmarks",
    "verify_run",
]
