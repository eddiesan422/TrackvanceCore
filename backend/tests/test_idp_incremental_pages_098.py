"""Provider pages survive later inference or adapter failure without publication."""
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2] / "deploy/idp"


@pytest.fixture
def runner():
    spec = importlib.util.spec_from_file_location("incremental_runner_test", ROOT / "paddle_runner.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def test_predictor_yields_first_page_without_eagerly_consuming_later_failure(runner, tmp_path):
    failure = RuntimeError("synthetic second-page failure")

    class Predictor:
        def predict(self, *args, **kwargs):
            pytest.fail("list-producing API must not be called")

        def predict_iter(self, *args, **kwargs):
            yield SimpleNamespace(json={"res": {"page_index": 0, "width": 100, "height": 100}})
            raise failure

    producer = runner.predictions(Predictor(), tmp_path / "document.pdf", [])
    page, result, region, _ = next(producer)
    request = {"source_sha256": "a" * 64, "regions": []}
    manifest = {"source_sha256": "a" * 64, "expected_pages": 2, "status": "INCOMPLETE", "pages": []}
    runner.persist_legacy_page(tmp_path, request, manifest, page=page, raw=result.json["res"],
        lines=[], geometry={"width": 100, "height": 100}, region=region)
    with pytest.raises(RuntimeError) as caught:
        next(producer)
    assert caught.value is failure
    checkpoint = json.loads((tmp_path / "legacy.manifest.json").read_bytes())
    assert checkpoint["status"] == "INCOMPLETE" and checkpoint["pages"][0]["page"] == 1
    original = json.loads((tmp_path / "legacy-page-001.json").read_bytes())
    assert original["original"] == result.json["res"] and original["complete_page"] is True
    assert not (tmp_path / "result.json").exists()


@pytest.mark.skipif(os.name != "posix", reason="Runner resource limits and native child filesystem contract")
def test_raw_page_is_durable_before_a_table_projection_failure(runner, tmp_path, monkeypatch):
    source = "a" * 64
    raw = {"page_index": 0, "width": 100, "height": 100,
           "parsing_res_list": [{"block_content": "<table>synthetic</table>", "block_label": "table", "block_bbox": [1, 1, 90, 90]}]}

    class Predictor:
        def __init__(self, **kwargs):
            pass

        def predict_iter(self, *args, **kwargs):
            yield SimpleNamespace(json={"res": raw})

    class BrokenProjection:
        def feed(self, value):
            raise RuntimeError("synthetic projection failed")

    paddle = SimpleNamespace(is_compiled_with_cuda=lambda: True,
        device=SimpleNamespace(cuda=SimpleNamespace(device_count=lambda: 1)))
    (tmp_path / "request.json").write_text(json.dumps({"filename": "document.pdf", "source_sha256": source, "regions": []}))
    monkeypatch.setitem(sys.modules, "paddle", paddle)
    monkeypatch.setitem(sys.modules, "paddleocr", SimpleNamespace(PaddleOCRVL=Predictor))
    monkeypatch.setattr(runner, "source_page_metadata", lambda _: [{"page": 1}])
    monkeypatch.setattr(runner, "TableText", BrokenProjection)
    monkeypatch.setattr(sys, "argv", [str(ROOT / "paddle_runner.py"), str(tmp_path)])
    with pytest.raises(RuntimeError, match="synthetic projection failed"):
        runner.main()
    preserved = json.loads((tmp_path / "legacy-page-001.json").read_bytes())
    assert preserved["original"] == raw
    assert preserved["source_sha256"] == source and preserved["page"] == 1
    assert json.loads((tmp_path / "legacy.manifest.json").read_bytes())["status"] == "INCOMPLETE"
    assert not (tmp_path / "result.json").exists()


def test_checkpoint_disk_failure_does_not_claim_page_or_complete_result(runner, tmp_path, monkeypatch):
    manifest = {"source_sha256": "a" * 64, "expected_pages": 1, "status": "INCOMPLETE", "pages": []}

    def fail(_descriptor):
        raise OSError("synthetic durable storage failure")

    monkeypatch.setattr(runner.os, "fsync", fail)
    with pytest.raises(OSError, match="synthetic durable storage failure"):
        runner.persist_legacy_page(tmp_path, {"source_sha256": "a" * 64, "regions": []}, manifest,
            page=1, raw={"synthetic": "raw"}, lines=[], geometry={}, region=[0, 0, 1, 1])
    assert manifest["pages"] == []
    assert not (tmp_path / "legacy.manifest.json").exists()
