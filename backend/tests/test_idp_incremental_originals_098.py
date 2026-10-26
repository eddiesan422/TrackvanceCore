"""Child persistence/render tests using synthetic pages, never model inference."""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def runner():
    path = Path(__file__).resolve().parents[2] / "deploy/idp/paddle_runner.py"
    spec = importlib.util.spec_from_file_location("incremental_runner_fixture", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_original_page_and_html_are_atomic_and_survive_next_page_failure(tmp_path, monkeypatch):
    module = runner()
    html = "<table><tr><th rowspan='2'>A|B<br>C</th><td></td></tr><tr><td colspan='2'>x\ny</td></tr></table>"
    original = {"res": {"page_index": 0, "width": 200, "height": 100,
        "parsing_res_list": [{"block_id": 7, "block_label": "table", "block_bbox": [2, 3, 90, 80],
            "block_content": html, "block_order": 2}]}}
    encoded = json.dumps(original, ensure_ascii=False).encode()
    class Result:
        def save_to_json(self, path, **_kwargs):
            assert str(path).endswith(".json")
            Path(path).write_bytes(encoded)
    class Pipeline:
        def predict(self, _source):
            pytest.fail("Materializing predict() must not be used by the structured route")
        def predict_iter(self, _source):
            yield Result()
            raise RuntimeError("synthetic later page fault")
    monkeypatch.setattr(module, "source_page_metadata", lambda _: [
        {"page": 1, "width": 50, "height": 100, "unit": "PDF_POINT", "rotation_degrees": 90},
        {"page": 2, "width": 50, "height": 100, "unit": "PDF_POINT", "rotation_degrees": 90}])
    request = {"filename": "document.pdf", "source_sha256": "a" * 64}
    marks = []
    with pytest.raises(RuntimeError, match="later page"):
        module.structured_pages(Pipeline(), tmp_path, request, SimpleNamespace(mark=lambda *args: marks.append(args)))
    manifest = json.loads((tmp_path / "pages.manifest.json").read_bytes())
    assert manifest["status"] == "INCOMPLETE" and manifest["expected_pages"] == 2
    assert [page["page"] for page in manifest["pages"]] == [1]
    records = manifest["pages"][0]["artifacts"]
    assert {item["kind"] for item in records} == {"PADDLE_PAGE_JSON", "PADDLE_TABLE_HTML"}
    for item in records:
        raw = (tmp_path / item["path"]).read_bytes()
        assert len(raw) == item["bytes"] and hashlib.sha256(raw).hexdigest() == item["sha256"]
        assert raw == (encoded if item["kind"] == "PADDLE_PAGE_JSON" else html.encode())
    assert not list(tmp_path.rglob("*.pending"))
    assert not list(tmp_path.rglob("*.pending.json"))
    assert manifest["pages"][0]["page_geometry"]["rotation_degrees"] == 90
    assert ("PERSIST_ORIGINAL_PAGE", 1) in marks


def test_intrinsic_reader_rejects_excess_pages_and_closes_native_document(tmp_path, monkeypatch):
    module = runner()
    class Document:
        closed = False
        def __len__(self):
            return 51
        def close(self):
            self.closed = True
    document = Document()
    monkeypatch.setitem(sys.modules, "pypdfium2", SimpleNamespace(PdfDocument=lambda _: document))
    with pytest.raises(ValueError, match="DOCUMENT_PAGE_LIMIT"):
        module.source_page_metadata(tmp_path / "document.pdf")
    assert document.closed
