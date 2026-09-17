import pytest

from trackvance.models import Dataset, DatasetVersion
from trackvance.services import dataset_origin


@pytest.mark.parametrize(
    ("source_type", "expected"),
    [
        ("UPLOAD", ("MANUAL", "Manual")),
        ("INTAKE_OUTPUT", ("DATA_INTAKE", "Data Intake")),
        ("DEMO", ("DEMO", "Demo")),
        ("GENERATED_DEMO", ("DEMO", "Demo")),
        (None, ("UNKNOWN", "Sin versiones")),
        ("postgresql_snapshot", ("POSTGRESQL_SNAPSHOT", "Postgresql Snapshot")),
    ],
)
def test_dataset_origin_maps_known_sources_and_keeps_future_connectors_visible(
    source_type,
    expected,
):
    assert dataset_origin(source_type) == expected


def add_version(db, dataset: Dataset, version: int, source_type: str) -> DatasetVersion:
    item = DatasetVersion(
        dataset_id=dataset.id,
        organization_id=dataset.organization_id,
        version=version,
        filename=f"version-{version}.parquet",
        source_type=source_type,
        sha256=str(version) * 64,
        schema_hash="a" * 64,
        size_bytes=10,
        row_count=version * 10,
        column_count=2,
        original_path="",
        canonical_path=f"/tmp/version-{version}.parquet",
        schema_json=[],
        profile={},
    )
    db.add(item)
    return item


def test_dataset_list_and_detail_expose_origin_from_latest_version(
    authenticated,
    database,
):
    with database() as db:
        manual = Dataset(name="Manual source")
        intake = Dataset(name="Intake output")
        demo = Dataset(name="Demo source")
        future = Dataset(name="Future connector")
        empty = Dataset(name="Without versions")
        db.add_all([manual, intake, demo, future, empty])
        db.flush()

        add_version(db, manual, 1, "UPLOAD")
        add_version(db, intake, 1, "UPLOAD")
        latest_intake = add_version(db, intake, 2, "INTAKE_OUTPUT")
        add_version(db, demo, 1, "GENERATED_DEMO")
        add_version(db, future, 1, "POSTGRESQL_SNAPSHOT")
        db.commit()
        ids = {
            "manual": manual.id,
            "intake": intake.id,
            "demo": demo.id,
            "future": future.id,
            "empty": empty.id,
        }

    response = authenticated.get("/api/v1/datasets")
    assert response.status_code == 200
    items = {item["id"]: item for item in response.json()["items"]}

    assert (items[ids["manual"]]["origin"], items[ids["manual"]]["origin_label"]) == (
        "MANUAL",
        "Manual",
    )
    assert items[ids["manual"]]["origin_source_type"] == "UPLOAD"
    assert items[ids["intake"]]["origin"] == "DATA_INTAKE"
    assert items[ids["intake"]]["origin_label"] == "Data Intake"
    assert items[ids["intake"]]["origin_source_type"] == "INTAKE_OUTPUT"
    assert items[ids["intake"]]["latest_version_id"] == latest_intake.id
    assert items[ids["demo"]]["origin_label"] == "Demo"
    assert items[ids["future"]]["origin"] == "POSTGRESQL_SNAPSHOT"
    assert items[ids["future"]]["origin_label"] == "Postgresql Snapshot"
    assert items[ids["empty"]]["origin"] == "UNKNOWN"
    assert items[ids["empty"]]["origin_label"] == "Sin versiones"
    assert items[ids["empty"]]["origin_source_type"] is None

    detail = authenticated.get(f"/api/v1/datasets/{ids['intake']}")
    assert detail.status_code == 200
    assert detail.json()["origin"] == "DATA_INTAKE"
    assert detail.json()["origin_label"] == "Data Intake"
