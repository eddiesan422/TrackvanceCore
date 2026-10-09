"""Current contracts and historical evidence keep separate document authority."""

from __future__ import annotations

import builtins
import copy
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

GENERATOR = Path(__file__).resolve().parents[1] / "docs/build_specification.py"
CORRECTIONS = {
    "version": "0.8.0",
    "summary": [{"label": "Antecedente 0.7: C01", "result": "Histórico; no recertificación 0.8."}],
}
VOLUME = {"Versión": "0.8.0", "Ensayo": "PASS histórico; fuente y alcance conservados."}


@pytest.fixture
def generator(monkeypatch):
    real_import = builtins.__import__

    def without_pdf(name, *args, **kwargs):
        if name.partition(".")[0] in {"reportlab", "pypdf"}:
            raise ModuleNotFoundError("PDF authoring dependencies are absent", name=name)
        return real_import(name, *args, **kwargs)

    spec = importlib.util.spec_from_file_location("specification_provenance", GENERATOR)
    module = importlib.util.module_from_spec(spec)
    with monkeypatch.context() as importing:
        importing.setattr(builtins, "__import__", without_pdf)
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("name", ["model_contract", "permission_contract", "parameters"])
def test_current_contract_requires_explicit_current_version(generator, name):
    value = {"version": "0.8.5"}
    assert generator.validate_input_document(name, value) is value
    for invalid in ({}, {"version": "0.8.0"}, {"version": "0.8.6"}, []):
        with pytest.raises((TypeError, ValueError)):
            generator.validate_input_document(name, invalid)


@pytest.mark.parametrize("name,value", [("corrections_results", CORRECTIONS), ("volume_results", VOLUME)])
def test_only_declared_historical_version_is_accepted_without_mutation(generator, name, value):
    before = copy.deepcopy(value)
    assert generator.validate_input_document(name, value) is value
    assert value == before
    version_field = "version" if name == "corrections_results" else "Versión"
    for invalid_version in (None, "0.7.0", "0.8.5", "0.8.6"):
        invalid = {**value, version_field: invalid_version}
        with pytest.raises(ValueError, match="0.8.0"):
            generator.validate_input_document(name, invalid)


@pytest.mark.parametrize("summary", [None, [], [None], [{"label": 7, "result": "PASS"}],
                                     [{"label": "C01", "result": {"status": "PASS"}}]])
def test_malformed_corrections_cannot_supply_evidence(generator, summary):
    with pytest.raises(ValueError, match="etiquetas y resultados"):
        generator.validate_input_document("corrections_results", {"version": "0.8.0", "summary": summary})


def test_malformed_volume_and_unknown_input_are_rejected(generator):
    with pytest.raises(ValueError, match="etiquetas y resultados"):
        generator.validate_input_document("volume_results", {**VOLUME, "Ensayo": 42})
    with pytest.raises(ValueError, match="desconocido"):
        generator.validate_input_document("unrecognized_results", {"version": "0.8.0"})


@pytest.mark.parametrize("name,filename,value", [
    ("corrections_results", "corrections-results.json", CORRECTIONS),
    ("volume_results", "volume-results.json", VOLUME),
])
def test_loading_keeps_historical_filename_and_exact_bytes(generator, monkeypatch, tmp_path, name, filename, value):
    monkeypatch.setattr(generator, "DOCS_ROOT", tmp_path, raising=False)
    path = tmp_path / "specification/inputs" / filename
    path.parent.mkdir(parents=True)
    original = (json.dumps(value, ensure_ascii=False, indent=3) + "\n\n").encode("utf-8")
    path.write_bytes(original)
    assert generator.input_document(name) == value
    assert path.read_bytes() == original
    assert sorted(p.name for p in path.parent.iterdir()) == [filename]


@pytest.mark.parametrize("name", ["model_contract", "permission_contract"])
def test_runtime_contract_uses_current_versioned_path(generator, monkeypatch, tmp_path, name):
    monkeypatch.setattr(generator, "REPO", tmp_path, raising=False)
    path = tmp_path / "docs/specification" / f"{name}_0.8.5.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"version": "0.8.5"}', encoding="utf-8")
    (path.parent / f"{name}_0.8.0.json").write_text('{"version": "0.8.0"}', encoding="utf-8")
    assert generator.input_document(name)["version"] == "0.8.5"


def test_model_labels_follow_product_authority(generator, monkeypatch):
    assert generator.model_evolution_label("NEW") == "Nueva en 0.8.5"
    assert generator.model_evolution_label("MODIFIED") == "Evolucionada en 0.8.5"
    assert generator.model_evolution_label("PRESERVED") == "Estructura histórica preservada"
    monkeypatch.setattr(generator, "VERSION", "9.9.9")
    assert generator.model_evolution_label("NEW") == "Nueva en 9.9.9"


def test_both_directives_render_antecedent_notice_and_original_rows(generator, monkeypatch, tmp_path):
    source = tmp_path / "source.md"
    source.write_text("## 1. Evidencia\n@corrections\n@volume\n", encoding="utf-8")
    monkeypatch.setattr(generator, "SOURCE", source, raising=False)
    monkeypatch.setattr(generator, "REPO", tmp_path, raising=False)
    monkeypatch.setattr(generator, "require_authoring_runtime", lambda: None)
    monkeypatch.setattr(generator, "input_document", lambda name: copy.deepcopy(
        CORRECTIONS if name == "corrections_results" else VOLUME
    ))
    rendered = []
    mocks = {
        "paragraph": lambda text, style="body": ("paragraph", text),
        "table": lambda rows, widths=None: ("table", rows),
        "Paragraph": lambda *args, **kwargs: None,
        "ParagraphStyle": lambda *args, **kwargs: None,
        "STYLES": {"h1": None},
        "NAVY": None,
        "INK": None,
        "MUTED": None,
        "Image": lambda *args, **kwargs: None,
        "Spacer": lambda *args: None,
        "PageBreak": lambda: None,
        "TableOfContents": lambda: SimpleNamespace(),
        "SpecDocument": lambda candidate: SimpleNamespace(multiBuild=rendered.extend),
    }
    for name, value in mocks.items():
        monkeypatch.setattr(generator, name, value, raising=False)
    generator.build(tmp_path / "candidate.pdf", {}, True)
    notices = [item[1] for item in rendered if isinstance(item, tuple)
               and item[0] == "paragraph" and item[1].startswith("Antecedente de ")]
    assert len(notices) == 2
    assert all("0.8.0" in notice and "No certifica la implementación 0.8.5" in notice for notice in notices)
    tables = [item[1] for item in rendered if isinstance(item, tuple) and item[0] == "table"]
    assert [CORRECTIONS["summary"][0]["label"], CORRECTIONS["summary"][0]["result"]] in tables[-2]
    assert ["Versión", "0.8.0"] in tables[-1]
    assert ["Ensayo", VOLUME["Ensayo"]] in tables[-1]


def test_missing_authoring_runtime_fails_before_output_creation(generator, monkeypatch, tmp_path):
    assert isinstance(generator.AUTHORING_IMPORT_ERROR, ModuleNotFoundError)
    results = tmp_path / "results.json"
    results.write_text('{"Estado": "Pendiente"}', encoding="utf-8")
    output = tmp_path / "output"
    monkeypatch.setattr(sys, "argv", [str(GENERATOR), "--repo", str(tmp_path), "--docs-root", str(tmp_path),
                                     "--results", str(results), "--output-dir", str(output)])
    with pytest.raises(RuntimeError, match="runtime documental"):
        generator.main()
    assert not output.exists()


@pytest.mark.parametrize("kind", ["delivery-flow", "delivery-preparation"])
def test_delivery_diagrams_explain_temporary_native_pk_validation(generator, monkeypatch, kind):
    boxes = []
    monkeypatch.setattr(generator.Diagram, "box", lambda self, *args: boxes.append(args[4:]))
    monkeypatch.setattr(generator.Diagram, "arrow", lambda *args: None)
    generator.Diagram(kind).draw()
    labels = " ".join(text for box in boxes for text in box)
    assert "TEMP" in labels
    assert "rollback" in labels or "revertido" in labels
    assert "Solo lectura" not in labels
