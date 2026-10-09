"""Linux ancestry sampling includes children created by non-main threads."""
import importlib.util
import os
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("host_resources", Path(__file__).parents[1] / "ci/host_resources.py")
assert SPEC and SPEC.loader
resources = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(resources)


def process(root, pid, parent, children, *, thread=None):
    path = root / str(pid)
    path.mkdir()
    fields = ["0"] * 22
    fields[0], fields[1], fields[11], fields[12], fields[21] = "S", str(parent), "20", "10", "5"
    (path / "stat").write_text(f"{pid} (comm with ) spaces) " + " ".join(fields), encoding="utf-8")
    task = path / "task" / str(thread or pid)
    task.mkdir(parents=True)
    (task / "children").write_text(" ".join(map(str, children)), encoding="ascii")


def test_proc_follows_only_owned_children_including_other_threads(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "sysconf", lambda key: 4096 if key == "SC_PAGE_SIZE" else 100, raising=False)
    process(tmp_path, 10, 1, [20, 30], thread=11)
    process(tmp_path, 20, 10, [40])
    process(tmp_path, 30, 10, [])
    process(tmp_path, 40, 20, [])
    # A stale child PID has been reused by a foreign parent and is excluded.
    other = tmp_path / "10/task/10"
    other.mkdir()
    (other / "children").write_text("50 99", encoding="ascii")
    process(tmp_path, 50, 2, [])
    result = resources._linux(10, proc_root=tmp_path)
    assert result["status"] == "PASS"
    assert {row["pid"] for row in result["processes"]} == {10, 20, 30, 40}
    assert all(row["rss_bytes"] == 5 * 4096 and row["cpu_seconds"] == 0.3 for row in result["processes"])


def test_proc_does_not_claim_measurement_for_missing_root(tmp_path, monkeypatch):
    monkeypatch.setattr(os, "sysconf", lambda _: 100, raising=False)
    assert resources._linux(10, proc_root=tmp_path)["status"] == "UNAVAILABLE"
