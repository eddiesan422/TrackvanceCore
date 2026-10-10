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
    assert draft["schema_version"] == 1
    assert "primary_key_mode" not in draft and "primary_key_columns" not in draft


@pytest.mark.parametrize("keys,mode", [(["record_id", "quantity"], "DEFINE"), ([], "NONE")])
def test_delivery_draft_pk_is_explicit_versioned_and_ordered(keys, mode):
    draft = delivery_cycle.delivery_draft("dataset-version", {"id": "destination", "destination_version_id": "version"},
                                         mode="CREATE_TABLE", schema_name="public", table_name="pk",
                                         strategy="CREATE_AND_LOAD", primary_key_columns=keys)
    assert draft["schema_version"] == 2 and draft["primary_key_mode"] == mode
    assert draft["primary_key_columns"] == keys and draft["upsert_keys"] == []


def test_target_commands_never_embed_passwords():
    postgres, postgres_input = delivery_cycle.target_command("POSTGRESQL", "SELECT 1")
    sqlserver, sqlserver_input = delivery_cycle.target_command("SQLSERVER", "SELECT 1")
    assert "password" not in " ".join(postgres).casefold()
    assert "$MSSQL_SA_PASSWORD" in " ".join(sqlserver)
    assert "SELECT 1" in postgres_input and "SELECT 1" in sqlserver_input


@pytest.mark.parametrize("delivery_database", [False, True])
@pytest.mark.parametrize("json_oracle", [False, True])
def test_sqlserver_database_selected_before_query_keeps_json_stdout_pure(delivery_database, json_oracle):
    query = "SELECT N'Unicode 東京' AS preserved_text FOR JSON PATH,INCLUDE_NULL_VALUES;"
    command, payload = delivery_cycle.target_command(
        "SQLSERVER", query, use_delivery_database=delivery_database, json_oracle=json_oracle
    )
    assert (" -d trackvance_delivery" in command[-1]) is delivery_database
    assert "USE trackvance_delivery;" not in payload
    assert payload == "SET NOCOUNT ON;\n" + query + "\nGO\n"
    assert "-C -b -h -1 -f 65001" in command[-1]
    assert ("-y 4096 -w 4096" in command[-1]) is json_oracle
    assert ("-W" in command[-1]) is not json_oracle
    assert "$MSSQL_SA_PASSWORD" in command[-1]


@pytest.mark.parametrize("damage", [None, "source_names", "order", "rows", "preview_columns", "mapping_columns"])
def test_publication_preview_checks_renamed_final_mapping_and_exact_population(damage):
    """Exercise only the harness assertion; this is not remote SQL evidence."""
    draft = delivery_cycle.delivery_draft("dataset-version", {"id": "destination", "destination_version_id": "version"},
                                         mode="CREATE_TABLE", schema_name="existing_delivery", table_name="pk_composite_085",
                                         strategy="CREATE_AND_LOAD", primary_key_columns=["transaction_code", "quantity"])
    draft["columns"] = [dict(column) for column in draft["columns"]]
    draft["columns"][0]["target_name"] = "transaction_code"
    # Storage order need not equal the explicit ordinal order.
    draft["columns"].reverse()
    names = ["transaction_code", *delivery_cycle.DATASET_COLUMNS[1:]]
    rows = len(delivery_cycle.DATASET_ROWS)
    if damage == "source_names":
        names = list(delivery_cycle.DATASET_COLUMNS)
    elif damage == "order":
        names.reverse()
    elif damage == "rows":
        rows -= 1
    elif damage == "preview_columns":
        names.pop()
    elif damage == "mapping_columns":
        draft["columns"].pop()

    class ReachedPreflight(Exception):
        pass

    def post(path, _body, **_kwargs):
        if path == "/api/v1/delivery/preflight":
            raise ReachedPreflight
        assert path == "/api/v1/delivery/preview"
        return {"sampled_rows": rows, "columns": [{"target_name": name} for name in names]}

    expected = delivery_cycle.smoke.SmokeFailure if damage else ReachedPreflight
    with pytest.raises(expected):
        delivery_cycle.publish_and_run(SimpleNamespace(post=post), delivery_cycle.smoke.Checks(), draft, "renamed mapping", [])


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
