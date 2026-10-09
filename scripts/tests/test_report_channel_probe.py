"""Diagnostic guards; native proof comes from the owned Linux receipt."""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import report_channel_probe as probe


def test_settings_hook_preserves_every_original_sandbox_byte_and_refuses_drift():
    original = (Path(__file__).resolve().parents[2] / "backend/src/trackvance/report_sandbox.py").read_text()
    changed = probe.private_child(original)
    assert changed.replace(probe.SETTINGS, "") == original
    compile(changed, "diagnostic", "exec")
    with pytest.raises(ValueError, match="drifted"):
        probe.private_child(original.replace(probe.HOOK, ""))


def test_rejected_line_receipt_has_only_safe_metadata_and_exact_hash():
    line = b'\r50% SYNTHETIC_PRIVATE_CELL\n'
    with pytest.raises(json.JSONDecodeError) as failure:
        json.loads(line)
    receipt = probe.line_failure(line, failure.value)
    assert receipt["sha256"] == hashlib.sha256(line).hexdigest()
    assert receipt["progress_marker"] and receipt["newline"] and receipt["carriage_returns"] == 1
    assert "SYNTHETIC_PRIVATE_CELL" not in json.dumps(receipt)


def test_partial_utf8_is_recorded_without_disclosing_content():
    line = b'{"kind":"batch","rows":[["\xc3'
    with pytest.raises(UnicodeDecodeError) as failure:
        json.loads(line)
    receipt = probe.line_failure(line, failure.value)
    assert receipt["class"] == "UnicodeDecodeError" and not receipt["newline"]
    assert not receipt["progress_marker"] and receipt["bytes"] == len(line)
