"""Verify joint Report resolution against real concurrent PostgreSQL Intake commits.

DATABASE_URL must identify a disposable PostgreSQL environment. This verifier
creates one UUID-owned schema and a temporary artifact directory, then removes
both. It never reads, migrates or modifies the application's existing tables or
storage. Output is a sanitized synthetic result, without URLs, secrets or rows.
Run as a standalone process before importing the Trackvance application.
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url


def verify() -> dict:
    raw_url = os.environ.get("DATABASE_URL", "")
    url = make_url(raw_url)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("This verifier requires a disposable PostgreSQL environment.")
    if (url.username != "tv_v080_test" or url.database != "tv_v080_test"
            or not re.fullmatch(r"trackvance-v080-test-[a-z0-9-]+-[0-9a-f]{12}",
                                os.environ.get("TRACKVANCE_CERTIFICATION_PROJECT", ""))):
        raise RuntimeError("This verifier requires the guarded UUID-owned 0.8 certification context.")
    if any(name == "trackvance.db" for name in sys.modules):
        raise RuntimeError("Run this verifier in a fresh process before application imports.")
    schema = "tv_reports_snapshot_" + uuid4().hex
    if not re.fullmatch(r"tv_reports_snapshot_[0-9a-f]{32}", schema):
        raise RuntimeError("Invalid isolated schema identity.")
    administrative = create_engine(url, pool_pre_ping=True)
    created = False
    application_engine = None
    writer = None
    try:
        with administrative.begin() as connection:
            # The identifier is generated above, validated, never user supplied.
            connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
            created = True
        with TemporaryDirectory(prefix="trackvance-pg-reports-") as directory:
            os.environ["DATABASE_URL"] = url.update_query_dict({"options": "-csearch_path=" + schema}).render_as_string(hide_password=False)
            os.environ["TRACKVANCE_STORAGE_DIR"] = str(Path(directory) / "storage")
            os.environ["TRACKVANCE_STORAGE_ROOT"] = str(Path(directory) / "storage")
            os.environ["DEMO_ACCESS_ENABLED"] = "false"
            os.environ["DEMO_SEED_ENABLED"] = "false"
            from trackvance import identity_bootstrap, report_service  # noqa: F401
            from trackvance.db import Base, SessionLocal, engine
            from trackvance.governance import strict_approval
            from trackvance.governance_models import DataDomain, MacroDomain
            from trackvance.models import Configuration, Dataset, Run, User
            from trackvance.services import create_version, enqueue, execute_run

            application_engine = engine
            Base.metadata.create_all(engine)
            requests, pending_ids = [], []
            with SessionLocal() as db:
                user = User(id="snapshot-user", name="Synthetic snapshot user", email="snapshot@example.test", password_hash="unused-synthetic")
                db.add(user)
                macro = MacroDomain(name="Synthetic macro", normalized_name="synthetic macro")
                db.add(macro)
                db.flush()
                domain = DataDomain(name="Synthetic domain", normalized_name="synthetic domain", macro_domain_id=macro.id)
                db.add(domain)
                db.flush()
                for alias in ("a", "b"):
                    dataset = Dataset(name="Snapshot input " + alias, macro_domain_id=macro.id, domain_id=domain.id)
                    db.add(dataset)
                    db.flush()
                    config = Configuration(name="Snapshot strict " + alias, module="intake", dataset_id=dataset.id,
                        config={"required_columns": ["key", "value"], "max_error_rate": 0})
                    db.add(config)
                    db.flush()
                    for ordinal in (1, 2):
                        path = Path(directory) / f"{alias}-{ordinal}.csv"
                        path.write_text(f"key,value\n001,{alias}{ordinal}\n002,{alias}{ordinal}\n", encoding="utf-8")
                        version = create_version(db, dataset, path, path.name,
                            column_overrides={"key": {"logical_type": "STRING", "semantic_tag": "IDENTIFIER"}})
                        run = enqueue(db, config, version, None, user.name)
                        db.commit()
                        if ordinal == 1:
                            execute_run(db, run)
                            db.commit()
                            if not strict_approval(db, run)["approved"]:
                                raise RuntimeError("Initial synthetic Intake was not strictly approved.")
                        else:
                            pending_ids.append(run.id)
                    requests.append({"alias": alias, "input_dataset_id": dataset.id, "contract_id": config.id,
                                     "contract_revision_ids": [config.id], "policy": "LATEST_APPROVED"})
                draft = {"mode": "GUIDED", "sources": requests,
                         "joins": [{"left_alias": "a", "right_alias": "b", "type": "LEFT",
                                    "keys": [{"left_column": "key", "right_column": "key"}],
                                    "expected_cardinality": "1:1", "allow_many_to_many": False}],
                         "columns": [{"source_alias": "a", "column": "value", "alias": "a_value"},
                                     {"source_alias": "b", "column": "value", "alias": "b_value"}]}
                initial = report_service.resolve(db, user, draft)
                old_outputs = [source["output_version_id"] for source in initial.snapshot["sources"]]

            committed = threading.Event()
            writer_errors, new_outputs, observed_isolation = [], [], []

            def publish_new_approvals():
                try:
                    with SessionLocal() as concurrent:
                        for identity in pending_ids:
                            new_run = concurrent.get(Run, identity)
                            execute_run(concurrent, new_run)
                            concurrent.commit()
                            if not strict_approval(concurrent, new_run)["approved"]:
                                raise RuntimeError("Concurrent synthetic Intake was not strictly approved.")
                            new_outputs.append(new_run.output_version_id)
                except Exception as exc:  # noqa: BLE001 - report only a sanitized error classification.
                    writer_errors.append(type(exc).__name__)
                finally:
                    committed.set()

            writer = threading.Thread(target=publish_new_approvals, daemon=True)
            original = report_service._source
            invoked = False

            def concurrent_boundary(joint, request, actor):
                nonlocal invoked
                selected = original(joint, request, actor)
                observed_isolation.append(joint.connection().get_isolation_level())
                if not invoked:
                    invoked = True
                    # Both new Intake approvals really publish after the first
                    # source read and before the second source read.
                    writer.start()
                    if not committed.wait(timeout=45) or writer_errors:
                        raise RuntimeError("Concurrent synthetic publication did not complete.")
                return selected

            report_service._source = concurrent_boundary
            try:
                with SessionLocal() as db:
                    frozen = report_service.resolve(db, db.get(User, "snapshot-user"), draft)
                    frozen_outputs = [source["output_version_id"] for source in frozen.snapshot["sources"]]
            finally:
                report_service._source = original
                writer.join(timeout=45)
            if writer.is_alive() or writer_errors or not committed.is_set():
                raise RuntimeError("Concurrent verification worker did not finish.")
            with SessionLocal() as db:
                following = report_service.resolve(db, db.get(User, "snapshot-user"), draft)
                following_outputs = [source["output_version_id"] for source in following.snapshot["sources"]]
            if (frozen_outputs != old_outputs or following_outputs != new_outputs
                    or set(old_outputs) & set(new_outputs) or observed_isolation != ["REPEATABLE READ"] * 2):
                raise RuntimeError("Joint metadata resolution mixed source snapshots.")
            result = {"version": "0.8.0", "status": "PASS", "database": "POSTGRESQL",
                      "read_isolation": "REPEATABLE READ", "source_count": 2,
                      "old_input_versions": [source["input_version"] for source in frozen.snapshot["sources"]],
                      "next_input_versions": [source["input_version"] for source in following.snapshot["sources"]],
                      "real_new_intake_approvals": len(new_outputs),
                      "new_approvals_committed_before_second_source_read": True,
                      "frozen_context_has_no_mixed_snapshot": True,
                      "next_context_selects_both_new_outputs": True,
                      "existing_application_tables_and_artifacts_touched": False}
    finally:
        if writer and writer.is_alive():
            writer.join(timeout=45)
        if application_engine is not None:
            application_engine.dispose()
        if created:
            with administrative.begin() as connection:
                # Drop only this invocation's exact validated UUID-owned schema.
                connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        administrative.dispose()
    return {**result, "isolated_schema_and_storage_cleaned": True}


def main():
    try:
        result = verify()
    except Exception as exc:  # noqa: BLE001 - no credentials, URLs, SQL or source data in evidence.
        print(json.dumps({"status": "FAIL", "check": "REPORT_POSTGRES_JOINT_SNAPSHOT", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
