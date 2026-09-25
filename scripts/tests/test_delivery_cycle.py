import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).with_name("delivery_cycle.py")
SPEC = importlib.util.spec_from_file_location("delivery_cycle", SCRIPT)
assert SPEC and SPEC.loader
delivery_cycle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(delivery_cycle)


def test_project_name_requires_disposable_prefix():
    assert (
        delivery_cycle.validated_project_name("trackvance-delivery-e2e-123-abc")
        == "trackvance-delivery-e2e-123-abc"
    )
    with pytest.raises(ValueError):
        delivery_cycle.validated_project_name("trackvance-core")


def test_redact_removes_all_generated_credentials():
    assert delivery_cycle.redact("alpha then beta", ["alpha", "beta"]) == (
        "[REDACTED] then [REDACTED]"
    )


def test_delivery_draft_separates_target_and_strategy():
    destination = {"id": "destination-1", "destination_version_id": "version-1"}
    draft = delivery_cycle.delivery_draft(
        "dataset-version-1",
        destination,
        mode="EXISTING_TABLE",
        schema_name="public",
        table_name="records",
        strategy="UPSERT",
    )
    assert draft["target"] == {
        "mode": "EXISTING_TABLE",
        "schema_name": "public",
        "table_name": "records",
        "create_schema": False,
    }
    assert draft["write_strategy"] == "UPSERT"
    assert draft["upsert_keys"] == ["record_id"]
    assert [column["ordinal"] for column in draft["columns"]] == list(range(8))


def test_target_commands_never_embed_passwords():
    postgres, postgres_input = delivery_cycle.target_command("POSTGRESQL", "SELECT 1")
    sqlserver, sqlserver_input = delivery_cycle.target_command("SQLSERVER", "SELECT 1")
    assert "password" not in " ".join(postgres).casefold()
    assert "$MSSQL_SA_PASSWORD" in " ".join(sqlserver)
    assert "SELECT 1" in postgres_input and "SELECT 1" in sqlserver_input


def test_destructive_runner_detects_a_preexisting_network():
    calls = []

    def command(arguments, **_kwargs):
        calls.append(arguments)
        return "network-id\n" if arguments[1:3] == ["network", "ls"] else ""

    resources = delivery_cycle.preexisting_project_resources(
        command, "trackvance-delivery-e2e-123-abc"
    )

    assert resources == {
        "containers": "",
        "volumes": "",
        "networks": "network-id",
    }
    assert any(arguments[1:3] == ["network", "ls"] for arguments in calls)


def test_wait_for_evidence_waits_after_durable_commit_using_only_reads(monkeypatch):
    pending = {"status": "SUCCESS", "decision": "COMMITTED", "metrics": {}}
    published = {**pending, "metrics": {"receipt_artifact_id": "receipt"}}
    states = iter([pending, pending, published])
    requests = []

    def get(path):
        requests.append(path)
        return next(states)

    monkeypatch.setattr(delivery_cycle.smoke.time, "sleep", lambda _: None)
    assert delivery_cycle.wait_for_evidence(SimpleNamespace(get=get), "run") == published
    assert requests == ["/api/v1/runs/run"] * 3


@pytest.mark.parametrize("damage", ["UNKNOWN", "FAILED", "PENDING_REPAIR", "wrong_decision"])
def test_wait_for_evidence_fails_closed_without_retrying_delivery(damage):
    state = {"status": "SUCCESS", "decision": "COMMITTED", "metrics": {}}
    if damage == "PENDING_REPAIR":
        state["metrics"] = {"evidence_status": damage, "receipt_artifact_id": "partial"}
    elif damage == "wrong_decision":
        state["decision"] = "UNKNOWN"
    else:
        state["status"] = damage
    with pytest.raises(delivery_cycle.smoke.SmokeFailure):
        delivery_cycle.wait_for_evidence(SimpleNamespace(get=lambda _: state), "run")


def test_wait_for_evidence_is_bounded(monkeypatch):
    clock = iter([0, 0, 2])
    monkeypatch.setattr(delivery_cycle.smoke.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(delivery_cycle.smoke.time, "sleep", lambda _: None)
    pending = {"status": "SUCCESS", "decision": "COMMITTED", "metrics": {}}
    with pytest.raises(delivery_cycle.smoke.SmokeFailure, match="Tiempo agotado"):
        delivery_cycle.wait_for_evidence(SimpleNamespace(get=lambda _: pending), "run", timeout=1)
