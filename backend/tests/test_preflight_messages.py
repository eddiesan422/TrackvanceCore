import ast
from pathlib import Path

import pytest

from trackvance import delivery_service
from trackvance.preflight_messages import MESSAGES, preflight_message


@pytest.mark.parametrize("code", sorted(MESSAGES))
def test_every_outcome_has_a_different_functional_message(code):
    checks = []
    delivery_service._check(checks, code, True, "password=never-render-driver-error")
    delivery_service._check(checks, code, False, "password=never-render-driver-error")
    assert [c["status"] for c in checks] == ["PASS", "FAIL"]
    assert [c["code"] for c in checks] == [code, code]
    assert checks[0]["message"] != checks[1]["message"]
    assert all("never-render" not in c["message"] for c in checks)


def test_catalog_covers_all_static_preflight_codes():
    tree = ast.parse(Path(delivery_service.__file__).read_text(encoding="utf-8"))
    codes = {
        node.args[1].value for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "_check" and len(node.args) >= 2
        and isinstance(node.args[1], ast.Constant)
    }
    assert codes <= MESSAGES.keys()


def test_context_is_schema_only_and_unknown_error_is_closed():
    assert "importe" in preflight_message("DECIMAL_COMPATIBLE", False, {"column": "importe"})
    assert "secret" not in preflight_message("DRIVER_ERROR", False, {"exception": "secret"})
    assert "\n" not in preflight_message("PERMISSIONS", False, {"column": "x\npassword=secret"})
    denied = preflight_message("PERMISSIONS", False)
    assert "No se verificaron" in denied
    assert "no conceden privilegios SQL" in denied
