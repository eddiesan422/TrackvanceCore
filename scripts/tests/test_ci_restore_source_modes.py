"""The Linux source image honors Git modes instead of Windows COPY modes."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci import restore_source_modes as source_modes


def index_entry(mode, name, stage="0"):
    return f"{mode} {'a' * 40} {stage}\t{name}\0".encode()


def mock_posix_chmod(monkeypatch):
    changed = {}
    original = Path.lstat
    def chmod(path, mode, *, follow_symlinks):
        assert follow_symlinks is False
        changed[Path(path)] = mode
    def lstat(path):
        observed = original(path)
        if path not in changed:
            return observed
        values = list(observed)
        values[0] = stat.S_IFMT(observed.st_mode) | changed[path]
        return os.stat_result(values)
    monkeypatch.setattr(source_modes.os, "chmod", chmod)
    monkeypatch.setattr(Path, "lstat", lstat)
    return changed


def test_windows_copy_modes_are_restored_from_committed_index_only(tmp_path, monkeypatch):
    ordinary = tmp_path / "ordinary.py"
    executable = tmp_path / "executable.py"
    untracked = tmp_path / "untracked.py"
    for path in (ordinary, executable, untracked):
        path.write_text("print('unchanged')\n")
    monkeypatch.setattr(source_modes.subprocess, "check_output", lambda _args:
        index_entry("100644", ordinary.name) + index_entry("100755", executable.name))
    before = {path: path.read_bytes() for path in (ordinary, executable, untracked)}
    if os.name == "posix":
        ordinary.chmod(0o755)
        executable.chmod(0o644)
        untracked.chmod(0o755)
        report = source_modes.restore_source_modes(tmp_path)
        assert stat.S_IMODE(ordinary.lstat().st_mode) == 0o644
        assert stat.S_IMODE(executable.lstat().st_mode) == 0o755
        assert stat.S_IMODE(untracked.lstat().st_mode) == 0o755
    else:
        changed = mock_posix_chmod(monkeypatch)
        report = source_modes.restore_source_modes(tmp_path)
        assert changed == {ordinary: 0o644, executable: 0o755}
    assert {path: path.read_bytes() for path in before} == before
    assert report["status"] == "PASS" and report["regular_files"] == 2 and report["executable_files"] == 1


@pytest.mark.parametrize("mode,stage", [("160000", "0"), ("100600", "0"), ("100644", "1")])
def test_unknown_or_unmerged_modes_fail_before_any_permissions_change(tmp_path, monkeypatch, mode, stage):
    (tmp_path / "valid.py").write_text("pass\n")
    (tmp_path / "unknown.py").write_text("pass\n")
    monkeypatch.setattr(source_modes.subprocess, "check_output", lambda _args:
        index_entry("100644", "valid.py") + index_entry(mode, "unknown.py", stage))
    monkeypatch.setattr(source_modes.os, "chmod", lambda *_args, **_kwargs: pytest.fail("Invalid index must not mutate files"))
    with pytest.raises(ValueError, match="Unknown or unmerged"):
        source_modes.restore_source_modes(tmp_path)


def test_materialized_symlink_is_rejected_without_following_or_chmod(tmp_path, monkeypatch):
    (tmp_path / "link").write_text("../outside\n")
    monkeypatch.setattr(source_modes.subprocess, "check_output", lambda _args: index_entry("120000", "link"))
    monkeypatch.setattr(source_modes.os, "chmod", lambda *_args, **_kwargs: pytest.fail("Symlink must not be chmodded"))
    with pytest.raises(ValueError, match="symlink was materialized"):
        source_modes.restore_source_modes(tmp_path)


def test_real_symlink_is_untouched_and_its_target_cannot_receive_permissions(tmp_path, monkeypatch):
    target = tmp_path / "outside.py"
    target.write_text("untouched target")
    if os.name == "posix":
        target.chmod(0o600)
        (tmp_path / "link").symlink_to(target)
    else:
        (tmp_path / "link").write_text("placeholder")
        real_lstat = Path.lstat
        def lstat(path):
            values = list(real_lstat(path))
            if path == tmp_path / "link":
                values[0] = stat.S_IFLNK | 0o777
            return os.stat_result(values)
        monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(source_modes.subprocess, "check_output", lambda _args: index_entry("120000", "link"))
    monkeypatch.setattr(source_modes.os, "chmod", lambda *_args, **_kwargs: pytest.fail("Symlink target must remain untouched"))
    report = source_modes.restore_source_modes(tmp_path)
    assert report["symlinks_untouched"] == 1 and report["regular_files"] == 0
    assert target.read_text() == "untouched target"
    if os.name == "posix":
        assert stat.S_IMODE(target.lstat().st_mode) == 0o600


def test_parent_traversal_cannot_grant_permissions_outside_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(source_modes.subprocess, "check_output", lambda _args: index_entry("100644", "../outside.py"))
    monkeypatch.setattr(source_modes.os, "chmod", lambda *_args, **_kwargs: pytest.fail("Outside checkout must not be modified"))
    with pytest.raises(ValueError, match="escapes"):
        source_modes.restore_source_modes(tmp_path)
