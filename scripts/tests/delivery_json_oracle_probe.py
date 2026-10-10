"""Bounded, UUID-owned native SQL Server CLI diagnostic; no backend certification."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

import delivery_cycle as runner
import delivery_typed_chain as chain
from delivery_native_query_probe import bounded_process

ROOT = Path(__file__).resolve().parents[2]
GIB = 1024**3
TABLE = "typed_085_polars"
SQL = ("SELECT record_id,customer_name,CONVERT(varchar(50),amount) AS amount,quantity,"
       "CONVERT(varchar(10),happened_on,23) AS happened_on,"
       "CONVERT(varchar(40),SWITCHOFFSET(updated_at,'+00:00'),127) AS updated_at,"
       "is_active,optional_note,all_null_int FROM [existing_delivery].[typed_085_polars] "
       "ORDER BY record_id FOR JSON PATH,INCLUDE_NULL_VALUES;")


def channel_metadata(value: bytes) -> dict:
    """Never persist CLI rows, SQL, private text or credentials."""
    text = value.decode("utf-8", errors="replace")
    notice = text.startswith("Changed database context to 'trackvance_delivery'.")
    try:
        parsed = json.loads("".join(line.strip() for line in text.splitlines() if line.strip()))
        shape = {"json_type": type(parsed).__name__, "json_items": len(parsed) if isinstance(parsed, list) else None}
    except (UnicodeError, ValueError) as error:
        shape = {"json_error": type(error).__name__, "json_position": error.pos if isinstance(error, json.JSONDecodeError) else None}
    return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest(),
            "newlines": value.count(b"\n"), "context_notice": notice,
            "sqlcmd_diagnostic": text.lstrip().startswith("Sqlcmd:"), **shape}


def candidate_command(command: list[str], payload: str) -> tuple[list[str], str]:
    """Private command-only experiment before changing the committed harness."""
    if "USE trackvance_delivery;\n" not in payload:
        return command, payload
    if payload.count("USE trackvance_delivery;\n") != 1 or " -d " in command[-1]:
        raise ValueError("AMBIGUOUS_DATABASE_SELECTOR")
    return [*command[:-1], command[-1] + " -d trackvance_delivery"], payload.replace("USE trackvance_delivery;\n", "", 1)


def run(args) -> int:
    sys.path.insert(0, str(ROOT / "scripts"))
    from ci import owned_cleanup
    from ci.run_local import inventory_differences, protected_inventory

    os.environ.update(DOCKER_CONFIG=str(args.docker_config.resolve()), DOCKER_HOST=args.docker_host)
    os.environ.pop("DOCKER_AUTH_CONFIG", None)
    os.environ.pop("DOCKER_CONTEXT", None)
    config = json.loads((args.docker_config / "config.json").read_text(encoding="utf-8"))
    if config.get("auths") != {"https://index.docker.io/v1/": {}} or config.get("credsStore") or config.get("credHelpers"):
        raise ValueError("EXPLICIT_ANONYMOUS_CONTEXT_REQUIRED")
    if not re.fullmatch(r"sha256:[a-f0-9]{64}", args.image_id):
        raise ValueError("IMMUTABLE_EXISTING_SQLSERVER_IMAGE_REQUIRED")
    inspected = json.loads(owned_cleanup.docker("image", "inspect", "--format",
        '{"id":{{json .Id}},"os":{{json .Os}},"arch":{{json .Architecture}}}', args.image_id))
    if inspected != {"id": args.image_id, "os": "linux", "arch": "amd64"}:
        raise ValueError("SQLSERVER_IMAGE_DRIFT")
    git = lambda *values: subprocess.check_output(["git", *values], cwd=ROOT, text=True).strip()
    scoped = ["scripts/tests/delivery_cycle.py", "scripts/tests/delivery_typed_chain.py",
              "scripts/tests/delivery_json_oracle_probe.py"]
    if git("diff", "HEAD", "--name-only", "--", *scoped):
        raise ValueError("COMMIT_DIAGNOSTIC_SOURCE_BEFORE_DOCKER")
    project = "trackvance-v070-test-delivery-json-" + uuid.uuid4().hex[:12]
    before = owned_cleanup.snapshot()
    protected_before = protected_inventory()
    receipt = {"schema_version": 1, "kind": "OWNED_SQLSERVER_JSON_ORACLE_DIAGNOSTIC", "status": "REGISTERED",
               "project": project, "harness_source_sha": git("rev-parse", "HEAD"), "image_id": args.image_id,
               "limits": {"cpus": 1, "memory_bytes": 3 * GIB, "pids": 256, "sql_pool_mib": 2048},
               "before": {key: sorted(values) for key, values in before.items()},
               "query_sha256": hashlib.sha256(SQL.encode()).hexdigest(), "rows": 3, "columns": 9}
    args.output.parent.mkdir(parents=True, exist_ok=True)

    def save():
        partial = args.output.with_suffix(".partial")
        with partial.open("w", encoding="utf-8") as output:
            json.dump(receipt, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        partial.replace(args.output)

    save()  # Durable UUID, image, limits and before IDs precede creation.
    started = time.monotonic()
    try:
        label = "com.docker.compose.project=" + project
        volume = owned_cleanup.docker("volume", "create", "--label", label, project + "_data").strip()
        network = owned_cleanup.docker("network", "create", "--internal", "--label", label, project + "_network").strip()
        receipt.update(volume=volume, network_id=network)
        save()
        os.environ["MSSQL_SA_PASSWORD"] = "TVp8!" + secrets.token_hex(24)
        try:
            container = owned_cleanup.docker("create", "--name", project, "--label", label,
                "--label", "com.docker.compose.service=destination-sqlserver", "--network", network,
                "--memory", str(3 * GIB), "--memory-swap", str(3 * GIB), "--cpus", "1", "--pids-limit", "256",
                "--env", "ACCEPT_EULA=Y", "--env", "MSSQL_PID=Developer", "--env", "MSSQL_MEMORY_LIMIT_MB=2048",
                "--env", "MSSQL_SA_PASSWORD", "--mount", f"type=volume,src={volume},dst=/var/opt/mssql", args.image_id).strip()
        finally:
            os.environ.pop("MSSQL_SA_PASSWORD", None)
        receipt["container_id"] = container
        save()
        owned_cleanup.docker("start", container)
        actual = json.loads(owned_cleanup.docker("inspect", "--format",
            '{"id":{{json .Id}},"image":{{json .Image}},"memory":{{json .HostConfig.Memory}},'
            '"nano_cpus":{{json .HostConfig.NanoCpus}},"pids":{{json .HostConfig.PidsLimit}}}', container))
        assert actual == {"id": container, "image": args.image_id, "memory": 3 * GIB,
                          "nano_cpus": 10**9, "pids": 256}
        receipt["effective_limits"] = actual

        def query(command, payload, timeout=30):
            assert command[:3] == ["exec", "-T", "destination-sqlserver"]
            status, stdout, stderr = bounded_process(["docker", "exec", "-i", container, *command[3:]],
                content=payload.encode("utf-8"), timeout=timeout)
            return status, stdout, stderr

        ready_command, ready_payload = runner.target_command("SQLSERVER", "SELECT 1;", use_delivery_database=False)
        deadline = time.monotonic() + 120
        while True:
            status, _, _ = query(ready_command, ready_payload)
            if status == 0:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("SQLSERVER_READY_TIMEOUT")
            time.sleep(2)
        setup = ("EXEC sp_configure 'show advanced options',1; RECONFIGURE; "
                 "EXEC sp_configure 'max server memory (MB)',2048; RECONFIGURE;\n"
                 "CREATE DATABASE trackvance_delivery;\nGO\nUSE trackvance_delivery;\nGO\n"
                 "CREATE SCHEMA existing_delivery;\nGO\n"
                 "CREATE TABLE existing_delivery.typed_085_polars (record_id nvarchar(128) NOT NULL PRIMARY KEY,"
                 "customer_name nvarchar(128) NULL,amount decimal(18,2) NULL,quantity bigint NULL,"
                 "happened_on date NULL,updated_at datetimeoffset(6) NULL,is_active bit NULL,"
                 "optional_note nvarchar(128) NULL,all_null_int bigint NULL);\n"
                 "INSERT INTO existing_delivery.typed_085_polars VALUES "
                 "(N'A-001',N'José Álvarez',10.50,1,'2026-09-20','2026-09-20T10:00:00+00:00',1,N'000001',NULL),"
                 "(N'B-002',N'Miyuki 東京',20.25,2,'2026-09-21','2026-09-21T11:30:00.123456-05:00',0,NULL,NULL),"
                 "(N'C-003',N'Zoë',30.00,3,'2026-09-22','2026-09-22T12:45:00+00:00',1,N'000003',NULL);\n")
        command, payload = runner.target_command("SQLSERVER", setup, use_delivery_database=False)
        status, stdout, stderr = query(command, payload)
        receipt["setup"] = {"exit_code": status, "stdout": channel_metadata(stdout), "stderr": channel_metadata(stderr)}
        if status:
            raise RuntimeError("SQLSERVER_SYNTHETIC_SETUP_FAILED")
        command, payload = runner.target_command("SQLSERVER", SQL, json_oracle=True)
        candidate, candidate_payload = candidate_command(command, payload)
        for name, cmd, text in (("current", command, payload), ("candidate", candidate, candidate_payload)):
            status, stdout, stderr = query(cmd, text)
            receipt[name] = {"exit_code": status, "stdout": channel_metadata(stdout), "stderr": channel_metadata(stderr)}
            if name == "candidate" and status == 0:
                raw = json.loads("".join(line.strip() for line in stdout.decode("utf-8").splitlines() if line.strip()))
                rows = [[row[key] for key in chain.column_names(runner)] for row in raw]
                actual = chain.normalize_rows(rows)
                expected = chain.normalize_rows(chain.fixture_rows(runner))
                assert actual == expected and len(actual) == 3
                receipt["candidate"].update(values_compared=27, logical_values_sha256=chain.content_hash(actual), oracle="PASS")
            save()
        sql = ("SELECT (SELECT COUNT(*) FROM sys.indexes WHERE object_id=OBJECT_ID(N'existing_delivery.typed_085_polars') AND index_id>0),"
               "(SELECT COUNT(*) FROM sys.key_constraints WHERE parent_object_id=OBJECT_ID(N'existing_delivery.typed_085_polars') AND type='PK'),"
               "(SELECT COUNT(*) FROM existing_delivery.typed_085_polars WHERE all_null_int IS NULL),"
               "(SELECT CAST(value_in_use AS int) FROM sys.configurations WHERE name='max server memory (MB)');")
        cmd, text = candidate_command(*runner.target_command("SQLSERVER", sql))
        status, stdout, _ = query(cmd, text)
        assert status == 0 and stdout.decode().split() == ["1", "1", "3", "2048"]
        receipt["native_checks"] = {"indexes": 1, "primary_keys": 1, "all_null_rows": 3, "sql_pool_mib": 2048, "status": "PASS"}
        receipt["status"] = "DIAGNOSED"
    except (OSError, ValueError, RuntimeError, AssertionError, subprocess.SubprocessError) as error:
        receipt.update(status="FAILED", error_type=type(error).__name__)
    finally:
        receipt["elapsed_seconds"] = round(time.monotonic() - started, 3)
        receipt["cleanup"] = owned_cleanup.cleanup(before, projects={project})
        receipt["after"] = {key: sorted(values) for key, values in owned_cleanup.snapshot().items()}
        receipt["protected_inventory_unchanged"] = not inventory_differences(protected_before, protected_inventory())
        save()
    print(json.dumps({key: receipt[key] for key in ("project", "status", "elapsed_seconds", "cleanup", "protected_inventory_unchanged")}))
    return 0 if receipt["status"] == "DIAGNOSED" and receipt["protected_inventory_unchanged"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-id", required=True)
    parser.add_argument("--docker-config", type=Path, required=True)
    parser.add_argument("--docker-host", required=True)
    parser.add_argument("--output", type=Path, required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
