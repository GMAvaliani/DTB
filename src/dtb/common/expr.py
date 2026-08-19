"""Safe arithmetic/boolean expression evaluation over a restricted AST.

Used for KPI definitions, trigger/effect count expressions, start/continuation
conditions, goal constraints and objective penalties. Only literals, names,
arithmetic, comparisons, boolean logic, conditional expressions and calls to
whitelisted functions are allowed — no attribute access, subscripts,
comprehensions or imports, so model data can never execute arbitrary code.
"""

from __future__ import annotations

import ast
import math
from typing import Any, Callable, Mapping

_ALLOWED_NODES = (
    ast.Expression,
    ast.Constant,
    ast.Name,
    ast.Load,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.Call,
    ast.Add,
    ast.Sub,
    ast.Mult,
    ast.Div,
    ast.FloorDiv,
    ast.Mod,
    ast.Pow,
    ast.USub,
    ast.UAdd,
    ast.Not,
    ast.And,
    ast.Or,
    ast.Eq,
    ast.NotEq,
    ast.Lt,
    ast.LtE,
    ast.Gt,
    ast.GtE,
)

_SAFE_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "min": min,
    "max": max,
    "abs": abs,
    "round": round,
    "floor": math.floor,
    "ceil": math.ceil,
    "sqrt": math.sqrt,
    "log": math.log,
    "exp": math.exp,
}

_SAFE_CONSTANTS: dict[str, Any] = {
    "pi": math.pi,
    "e": math.e,
    "true": True,
    "false": False,
    "True": True,
    "False": False,
}


class ExpressionError(ValueError):
    """Raised when an expression is syntactically or semantically invalid."""


class SafeExpression:
    """A pre-compiled safe expression.

    Context names are resolved at evaluation time from the supplied mapping;
    callables in the context can be invoked (e.g. ``stock('steel')``).
    """

    def __init__(self, source: str):
        self.source = source
        try:
            tree = ast.parse(source, mode="eval")
        except SyntaxError as exc:
            raise ExpressionError(f"invalid expression {source!r}: {exc}") from exc
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED_NODES):
                raise ExpressionError(
                    f"expression {source!r}: construct {type(node).__name__} is not allowed"
                )
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name):
                    raise ExpressionError(
                        f"expression {source!r}: only direct function calls are allowed"
                    )
                if node.keywords:
                    raise ExpressionError(
                        f"expression {source!r}: keyword arguments are not allowed"
                    )
        self._code = compile(tree, "<dtb-expr>", "eval")
        self.names = sorted(
            {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            - set(_SAFE_FUNCTIONS)
            - set(_SAFE_CONSTANTS)
        )

    def evaluate(self, context: Mapping[str, Any] | None = None) -> Any:
        env: dict[str, Any] = dict(_SAFE_CONSTANTS)
        env.update(_SAFE_FUNCTIONS)
        if context:
            env.update(context)
        try:
            return eval(self._code, {"__builtins__": {}}, env)  # noqa: S307 — restricted AST
        except NameError as exc:
            raise ExpressionError(f"expression {self.source!r}: unknown name ({exc})") from exc
        except ZeroDivisionError:
            return 0.0

    def __repr__(self) -> str:  # pragma: no cover
        return f"SafeExpression({self.source!r})"


def validate_expression(source: str) -> list[str]:
    """Return the unresolved context names, raising ExpressionError if invalid."""
    return SafeExpression(source).names


def evaluate(source: str, context: Mapping[str, Any] | None = None) -> Any:
    return SafeExpression(source).evaluate(context)
