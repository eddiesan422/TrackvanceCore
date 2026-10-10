import hashlib
import json

import delivery_cycle
import delivery_json_oracle_probe as probe


def test_cli_metadata_preserves_shape_and_digest_without_private_rows():
    private = b"Changed database context to 'trackvance_delivery'.\n[{\"secret\":\"synthetic-private-cell\"}]\n"
    metadata = probe.channel_metadata(private)
    assert metadata["context_notice"] and metadata["json_error"] == "JSONDecodeError"
    assert metadata["json_position"] == 0 and metadata["sha256"] == hashlib.sha256(private).hexdigest()
    assert metadata["bytes"] == len(private) and "secret" not in json.dumps(metadata)
    assert "synthetic-private-cell" not in json.dumps(metadata)


def test_candidate_changes_only_database_selection_and_retains_sql_and_credentials_boundary():
    command, payload = delivery_cycle.target_command("SQLSERVER", probe.SQL, json_oracle=True)
    candidate, candidate_payload = probe.candidate_command(command, payload)
    assert " -d trackvance_delivery" in candidate[-1] and "USE trackvance_delivery;" not in candidate_payload
    assert probe.SQL in candidate_payload and "-y 4096 -w 4096" in candidate[-1]
    assert candidate[:-1] == command[:-1] and "$MSSQL_SA_PASSWORD" in candidate[-1]
    assert probe.candidate_command(candidate, candidate_payload) == (candidate, candidate_payload)
