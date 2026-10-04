"""The shared area catalogue uses the entire organization dataset collection."""

from trackvance.models import Dataset, Role, RolePermission, User


def test_dataset_collection_keeps_all_areas_beyond_100_and_org_scope(database, authenticated):
    with database() as db:
        user = db.get(User, "test-user")
        role = Role(name="Dataset catalogue reader", normalized_name="dataset catalogue reader", active=True)
        db.add(role)
        db.flush()
        db.add(RolePermission(role_id=role.id, permission_code="datasets:read"))
        user.role_id = role.id
        for number in range(151):
            db.add(Dataset(organization_id=user.organization_id, name=f"Dataset {number:03}",
                           domain=f"Área {number:03}", status="INACTIVE" if number == 150 else "ACTIVE"))
        db.add(Dataset(organization_id="foreign-organization", name="Foreign dataset", domain="Private foreign area"))
        db.commit()
    response = authenticated.get("/api/v1/datasets")
    assert response.status_code == 200
    collection = response.json()
    assert collection["total"] == 151 and len(collection["items"]) == 151
    assert {item["domain"] for item in collection["items"]} == {f"Área {number:03}" for number in range(151)}
    assert "Private foreign area" not in response.text
