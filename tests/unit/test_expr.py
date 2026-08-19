import pytest

from dtb.common.expr import ExpressionError, SafeExpression, evaluate


def test_arithmetic_and_functions():
    assert evaluate("2 + 2 * 2") == 6
    assert evaluate("max(1, 5, 3)") == 5
    assert evaluate("sqrt(9) + floor(1.7)") == 4.0
    assert evaluate("100 * 3 / max(1, 0)") == 300


def test_context_functions_and_names():
    ctx = {"stock": lambda u: {"steel": 42.0}.get(u, 0.0), "quantity": 3}
    assert evaluate("stock('steel') * quantity", ctx) == 126.0
    assert evaluate("stock('steel') > 40 and quantity == 3", ctx) is True


def test_conditional_and_comparison():
    assert evaluate("1 if 2 > 1 else 0") == 1
    assert evaluate("'pass' == 'pass'") is True


def test_division_by_zero_is_zero():
    assert evaluate("1 / 0") == 0.0


@pytest.mark.parametrize(
    "bad",
    [
        "__import__('os')",
        "().__class__",
        "[x for x in range(3)]",
        "lambda: 1",
        "open('/etc/passwd')",  # `open` is never in the evaluation context
        "a.b",
        "x[0]",
        "f(x=1)",
    ],
)
def test_forbidden_constructs(bad):
    with pytest.raises(ExpressionError):
        SafeExpression(bad).evaluate({"a": 1, "x": [1], "f": lambda **kw: 1})


def test_unknown_name_raises():
    with pytest.raises(ExpressionError):
        evaluate("unknown_var + 1")


def test_names_introspection():
    assert SafeExpression("stock('a') + rate * 2").names == ["rate", "stock"]
