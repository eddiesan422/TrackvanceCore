"""Small mandatory browser coverage must keep ownership and an independent oracle."""
import json
from pathlib import Path

import browser_evidence
import docker_e2e_cycle as runner
import documents_095_receiver as receiver
import pytest
from ci import run_suite
from ci.common import load_manifest


@pytest.mark.parametrize("project", ["trackvance-certification", "trackvance-codex", "bikerwash", "unknown"])
def test_ci_document_fixture_rejects_permanent_or_unowned_projects(project, tmp_path):
    with pytest.raises(ValueError):
        runner.add_document_fixture([], {}, tmp_path, project)


def test_ci_document_fixture_bounds_only_owned_receiver_and_exact_origin(tmp_path, monkeypatch):
    import ci_images
    monkeypatch.setattr(ci_images, "verified_images", lambda _: {"backend": "sha256:" + "a" * 64})
    monkeypatch.setattr(runner, "available_port", lambda: 39895)
    project = "trackvance-v070-test-e2e-documents-0123456789ab"
    environment = {}
    compose = runner.add_document_fixture(["docker", "compose", "-p", project], environment, tmp_path, project)
    data = json.loads(Path(compose[-1]).read_text())
    fixture = data["services"]["document-fixture"]
    assert fixture["image"] == "sha256:" + "a" * 64
    assert fixture["ports"] == ["127.0.0.1:39895:18095"]
    assert fixture["mem_limit"] == "128m" and fixture["cpus"] == .25 and fixture["pids_limit"] == 32
    assert not any("credentials" in str(volume) for volume in fixture["volumes"])
    assert environment["TV_CORE095_TEST_PROJECT"] == project
    assert environment["TV_CORE095_DOCUMENT_E2E"] == "true"
    assert data["services"]["api"]["environment"]["TRACKVANCE_REST_ALLOWED_INTERNAL_ORIGINS"] == "http://document-fixture:18095"


@pytest.mark.parametrize("feature,module", [("documents", "documents_ocr"),
    ("acquisition", "acquisitions_multiformat"), ("resolution", "exceptions_resolution")])
def test_native_095_browser_is_mandatory_in_functional_commands_manifest_and_coverage(tmp_path, feature, module):
    commands = run_suite.commands_for("compose-functional", tmp_path)
    command = next(command for phase, command, _ in commands if phase == "browser-persistence")
    spec = f"tests-e2e/{feature}-095.spec.ts"
    assert spec in command and "--documents" in command
    manifest = load_manifest()
    group = next(g for g in manifest["groups"] if g["id"] == "compose-functional")
    scenario = next(s for s in group["scenarios"] if s["id"] == "functional-compose-browser-persistence")
    assert spec in scenario["browser_required_specs"]
    assert browser_evidence.CI_OPT_IN_SPECS[spec] == ("TV_CORE095_DOCUMENT_E2E", "compose-functional")
    functional = manifest["profiles"]["functional"]
    assert spec in functional["module_coverage"][module]["browser_specs"]


def test_receiver_checks_full_row_values_multiplicity_and_header_detail_link():
    oracle = [{"invoice": "000001", "total": "1234.50", "date": "2026-10-01"}]
    rows = [("headers", {"document_id": "document", **oracle[0]}),
            ("details", {"document_id": "document", "sku": "SKU1", "amount": "10.25"}),
            ("details", {"document_id": "document", "sku": "SKU2", "amount": "20.50"})]
    assert receiver.receipt(oracle, rows)["status"] == "PASS"
    assert receiver.receipt(oracle, rows[:2])["status"] == "INCOMPLETE"
    assert receiver.receipt(oracle, rows + [rows[1]])["status"] == "INCOMPLETE"
    wrong = [*rows[:2], ("details", {"document_id": "other-document", "sku": "SKU2", "amount": "20.50"})]
    assert receiver.receipt(oracle, wrong)["status"] == "INCOMPLETE"
    wrong = [("headers", {"document_id": "document", **oracle[0], "invoice": "1"}), *rows[1:]]
    assert receiver.receipt(oracle, wrong)["status"] == "INCOMPLETE"
