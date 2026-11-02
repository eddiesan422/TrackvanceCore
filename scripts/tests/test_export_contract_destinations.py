"""Runtime contracts stay executable fixtures; prose goes to docs or CI scratch."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
BASELINES = REPOSITORY / "backend/tests/fixtures/contracts"


@pytest.fixture
def exporter(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("export_contract_destinations", REPOSITORY / "scripts/export_contracts.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    product = tmp_path / "product"
    product.mkdir()
    monkeypatch.setattr(module, "ROOT", product)
    return module


@pytest.mark.parametrize("relative", [".", "docs", "docs/specification", "backend/reports", ".codex-local/../docs"])
def test_product_documentation_targets_rejected_before_import_or_write(exporter, monkeypatch, relative):
    import builtins
    original = builtins.__import__

    def deny_runtime(name, *args, **kwargs):
        if name.partition(".")[0] == "trackvance":
            pytest.fail("Rejected destination must not initialize the runtime")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_runtime)
    with pytest.raises(SystemExit) as raised:
        exporter.main(["--docs-root", str(exporter.ROOT / relative)])
    assert raised.value.code == 2
    assert list(exporter.ROOT.iterdir()) == []


@pytest.mark.parametrize("external", [False, True])
def test_real_export_preserves_reviewed_contracts_and_never_writes_product_docs(exporter, tmp_path, external):
    before = {path.name: path.read_bytes() for path in BASELINES.iterdir()}
    docs = tmp_path / "official-docs/docs" if external else exporter.ROOT / ".codex-local/ci/generated-contracts/docs"
    args = ["--check-baselines", str(BASELINES)]
    if external:
        args += ["--docs-root", str(docs)]
    exporter.main(args)
    assert not (exporter.ROOT / "docs").exists()
    assert (exporter.ROOT / "backend/openapi.json").is_file()
    assert {path.name for path in (docs / "specification").iterdir()} == set(before)
    for name, content in before.items():
        assert json.loads((docs / "specification" / name).read_text(encoding="utf-8")) == json.loads(content)
        assert (BASELINES / name).read_bytes() == content
    assert "# Matriz de permisos HTTP" in (docs / "development/permission-matrix.md").read_text(encoding="utf-8")


def test_contract_drift_fails_without_rebaselining_or_partial_outputs(exporter, tmp_path):
    baselines = tmp_path / "reviewed-fixtures"
    baselines.mkdir()
    for path in BASELINES.iterdir():
        (baselines / path.name).write_bytes(path.read_bytes())
    path = baselines / "permission_contract_0.9.8.json"
    content = json.loads(path.read_text(encoding="utf-8"))
    content["routes"] = content["routes"][:-1]
    path.write_text(json.dumps(content), encoding="utf-8")
    before = path.read_bytes()
    docs = tmp_path / "official-docs/docs"
    with pytest.raises(ValueError, match="differs from its reviewed machine contract fixture"):
        exporter.main(["--docs-root", str(docs), "--check-baselines", str(baselines)])
    assert path.read_bytes() == before
    assert not docs.exists()
    assert list(exporter.ROOT.iterdir()) == []


def test_ci_uses_transient_contract_outputs_and_keeps_strict_machine_baselines(tmp_path):
    sys.path.insert(0, str(REPOSITORY / "scripts"))
    from ci.run_suite import collect_sources, commands_for

    commands = commands_for("backend", tmp_path, profile="functional")
    contract_command = next(command for phase, command, _ in commands if phase == "contracts")
    assert contract_command[contract_command.index("--docs-root") + 1] == str(tmp_path / "generated-contracts/docs")
    assert contract_command[contract_command.index("--check-baselines") + 1] == "tests/fixtures/contracts"
    assert "backend/openapi.json" in next(command for phase, command, _ in commands if phase == "contracts-diff")
    assert not any("docs/specification" in token or "TrackvanceCore-docs" in token for token in contract_command)
    (tmp_path / "runtime-isolation.private.log").write_text('{"status":"PASS"}', encoding="utf-8")
    sources = collect_sources("backend", tmp_path, set())
    assert sources["model-contract"] == tmp_path / "generated-contracts/docs/specification/model_contract_0.9.8.json"
    assert sources["permission-contract"] == tmp_path / "generated-contracts/docs/specification/permission_contract_0.9.8.json"
    assert "permission-matrix" not in sources  # Markdown is a raw attachment, never a JSON result.


def test_markdown_attachment_preserves_bytes_without_becoming_verified_result(tmp_path, monkeypatch):
    import hashlib
    sys.path.insert(0, str(REPOSITORY / "scripts"))
    from ci import evidence
    from ci.common import EvidenceError

    validated = []
    monkeypatch.setattr(evidence, "validate_content", lambda scenario, result, **kwargs: validated.append(result))
    manifest = {"groups": [{"id": "raw-audit", "required_sources": ["result"],
                            "scenarios": [{"id": "raw-audit-bytes"}]}]}
    source = tmp_path / "result.json"
    source.write_text('{"verified_result": "fixture"}', encoding="utf-8")
    markdown = tmp_path / "permission-matrix.md"
    content = b"# Human contract\nNot JSON. Observaciones conservadas.\n"
    markdown.write_bytes(content)
    arguments = {"source_sha": "a" * 40, "ci": {"run_id": "1", "run_attempt": "1", "job_id": "2"},
                 "started_at": "2026-11-02T00:00:00+00:00", "completed_at": "2026-11-02T00:00:01+00:00",
                 "duration_seconds": 1.0, "manifest": manifest}
    output = tmp_path / "retained"
    receipts = evidence.wrap_group("raw-audit", {"result": source}, output,
                                   raw_attachments=[markdown], **arguments)
    receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    attachment = next(item for item in receipt["evidence"] if item["kind"] == "raw-supplement")
    assert (output / attachment["path"]).read_bytes() == content
    assert attachment["sha256"] == hashlib.sha256(content).hexdigest()
    assert all(item["documents"] == {"result": {"verified_result": "fixture"}} for item in validated)
    with pytest.raises((EvidenceError, json.JSONDecodeError)):
        evidence.wrap_group("raw-audit", {"result": markdown}, tmp_path / "strict-json", **arguments)
    with pytest.raises(EvidenceError, match="MISSING_SUITE_ATTACHMENT"):
        evidence.wrap_group("raw-audit", {"result": source}, tmp_path / "missing",
                            raw_attachments=[tmp_path / "absent.md"], **arguments)
