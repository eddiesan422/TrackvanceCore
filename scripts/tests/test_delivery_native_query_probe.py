"""Guards for the private diagnostic channel; not native certification evidence."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import delivery_native_query_probe as probe


def test_private_hook_changes_only_duckdb_exception_stderr_and_refuses_drift():
    sandbox = Path(__file__).resolve().parents[2] / "backend/src/trackvance/report_sandbox.py"
    original = sandbox.read_text(encoding="utf-8")
    changed = probe.private_child(original)
    assert changed.replace(probe.DIAGNOSTIC, "") == original
    assert changed[:changed.index(probe.HOOK)] == original[:original.index(probe.HOOK)]
    with pytest.raises(ValueError, match="refused"):
        probe.private_child(original.replace(probe.HOOK, ""))


def test_bounded_pipe_drains_stdout_and_stderr_without_truncating_valid_data():
    status, stdout, stderr = probe.bounded_process([sys.executable, "-c",
        "import sys; sys.stdout.write('o'*32000); sys.stderr.write('e'*32000)"])
    assert status == 0 and stdout == b"o" * 32000 and stderr == b"e" * 32000


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
def test_bounded_pipe_rejects_overflow(channel):
    with pytest.raises(ValueError, match="bound"):
        probe.bounded_process([sys.executable, "-c", f"import sys; sys.{channel}.write('x'*100000)"])
