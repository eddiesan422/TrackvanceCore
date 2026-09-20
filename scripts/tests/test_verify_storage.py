import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from trackvance.db import Base
from trackvance.models import Artifact, ArtifactLink, ExceptionCase

SCRIPT = Path(__file__).resolve().parents[1] / "verify_storage.py"
spec = importlib.util.spec_from_file_location("verify_storage", SCRIPT)
verify_storage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(verify_storage)


def minimal_rows():
    return {
        "artifacts": [{"id": "artifact", "organization_id": "org"}],
        "datasets": [{"id": "dataset", "organization_id": "org"}],
        "dataset_versions": [
            {
                "id": "version",
                "organization_id": "org",
                "dataset_id": "dataset",
                "parent_version_id": None,
                "source_run_id": None,
                "ingestion_metadata": {},
            }
        ],
        "runs": [],
        "external_connections": [],
        "external_connection_versions": [],
        "artifact_links": [
            {
                "id": "link",
                "organization_id": "org",
                "source_type": "DATASET_VERSION",
                "source_id": "version",
                "target_type": "ARTIFACT",
                "target_id": "artifact",
            }
        ],
    }


def exception_evidence_rows(
    *, exception_id: str = "exception", exception_org: str = "org"
):
    """Round-trip the production ORM mappings used by snapshot()."""

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all(
            [
                ExceptionCase(
                    id="exception",
                    organization_id=exception_org,
                    display_id="EXC-ORM-1",
                    run_id="run",
                    configuration_id="configuration",
                    title="ORM evidence",
                    module="SENTINEL",
                    severity="HIGH",
                ),
                Artifact(
                    id="artifact",
                    organization_id="org",
                    kind="EXCEPTION_ATTACHMENT",
                    name="evidence.txt",
                    path="artifacts/evidence.txt",
                    sha256="0" * 64,
                    size_bytes=1,
                    media_type="text/plain",
                ),
                ArtifactLink(
                    id="link",
                    organization_id="org",
                    relation="EXCEPTION_EVIDENCE",
                    source_type="EXCEPTION",
                    source_id=exception_id,
                    target_type="ARTIFACT",
                    target_id="artifact",
                ),
            ]
        )
        session.commit()
        return {
            table_name: list(
                session.execute(select(Base.metadata.tables[table_name])).mappings()
            )
            for table_name in ("exceptions", "artifacts", "artifact_links")
        }


def test_relationship_validation_covers_declared_and_polymorphic_links():
    rows = minimal_rows()
    checks = verify_storage.validate_relationships(
        rows, [("dataset_versions", "dataset_id", "datasets", "id")]
    )
    assert checks == 3


def test_relationship_validation_rejects_cross_organization_link():
    rows = minimal_rows()
    rows["artifacts"][0]["organization_id"] = "other-org"
    with pytest.raises(ValueError, match="organización"):
        verify_storage.validate_relationships(rows, [])


def test_exception_evidence_uses_real_orm_table_mapping():
    assert verify_storage.validate_relationships(exception_evidence_rows(), []) == 2


def test_exception_evidence_rejects_missing_exception_from_real_orm_rows():
    rows = exception_evidence_rows(exception_id="missing")
    with pytest.raises(ValueError, match="source EXCEPTION"):
        verify_storage.validate_relationships(rows, [])


def test_exception_evidence_rejects_cross_organization_from_real_orm_rows():
    rows = exception_evidence_rows(exception_org="other-org")
    with pytest.raises(ValueError, match="organización"):
        verify_storage.validate_relationships(rows, [])


def test_relationship_validation_rejects_stale_source_identity():
    rows = minimal_rows()
    rows["external_connections"] = [{"id": "connection", "organization_id": "org"}]
    rows["external_connection_versions"] = [
        {
            "id": "connection-version",
            "organization_id": "org",
            "connection_id": "connection",
            "config_hash": "current",
        }
    ]
    rows["dataset_versions"][0]["ingestion_metadata"] = {
        "source": {
            "connection_id": "connection",
            "connection_version_id": "connection-version",
            "config_hash": "stale",
        }
    }
    with pytest.raises(ValueError, match="config_hash"):
        verify_storage.validate_relationships(rows, [])


def test_compare_reports_new_scheduler_table_change():
    before = {
        "schema_version": 2,
        "tables": {"monitor_occurrences": {"one": "before"}},
    }
    after = {
        "schema_version": 2,
        "tables": {"monitor_occurrences": {"one": "after"}},
    }
    with pytest.raises(ValueError, match="monitor_occurrences"):
        verify_storage.compare(before, after)
