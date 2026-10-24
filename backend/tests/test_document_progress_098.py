"""Concurrent child progress cannot fail a document or follow a child symlink."""
import os
from pathlib import Path

import pytest

from trackvance.document_engine import write_progress
from trackvance.document_worker import _read_engine_progress
from trackvance.operations_common import OperationError


def test_missing_and_transient_empty_progress_keep_last_observation(tmp_path):
    path = tmp_path / "progress"
    assert _read_engine_progress(path, 37) == 37
    path.write_bytes(b"")
    assert _read_engine_progress(path, 37) == 37
    path.write_bytes(b"49")
    assert _read_engine_progress(path, 37) == 49
    path.write_bytes(b"3")
    assert _read_engine_progress(path, 49) == 49
    path.write_bytes(b"100")
    assert _read_engine_progress(path, 49) == 100


def test_progress_reader_sees_complete_old_value_during_atomic_publication(tmp_path, monkeypatch):
    path = tmp_path / "progress"
    path.write_bytes(b"17")
    original = Path.replace
    observed = []

    def interleave(temporary, target):
        observed.append(_read_engine_progress(path, 0))
        return original(temporary, target)

    monkeypatch.setattr(Path, "replace", interleave)
    write_progress(path, 82)
    assert observed == [17]
    assert _read_engine_progress(path, 17) == 82


@pytest.mark.parametrize("content", [b"-1", b"101", b"4x", b"\xff", b"7\n", b"0" * 8192])
def test_invalid_progress_is_bounded_and_fails_with_safe_error(tmp_path, content):
    path = tmp_path / "progress"
    path.write_bytes(content)
    with pytest.raises(OperationError) as caught:
        _read_engine_progress(path, 0)
    assert caught.value.code == "DOCUMENT_ENGINE_FAILURE"
    assert caught.value.status == 422


def test_progress_rejects_child_symlink_without_reading_target(tmp_path):
    target = tmp_path / "private"
    target.write_text("73", encoding="ascii")
    path = tmp_path / "progress"
    path.symlink_to(target)
    with pytest.raises(OperationError, match="progreso inválido"):
        _read_engine_progress(path, 0)


def test_progress_rejects_fifo_without_blocking(tmp_path):
    path = tmp_path / "progress"
    os.mkfifo(path)
    with pytest.raises(OperationError, match="progreso inválido"):
        _read_engine_progress(path, 0)
