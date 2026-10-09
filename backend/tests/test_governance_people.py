"""R085-06: contacts, verified account reuse and historical governance compatibility."""
from sqlalchemy import func, select

from trackvance.governance_models import GovernanceHistory, GovernancePerson
from trackvance.models import AuditEvent, Dataset, User


def create(client, **values):
    response = client.post("/api/v1/governance/people", json=values)
    assert response.status_code == 201, response.text
    return response.json()


def test_shared_contacts_allow_homonyms_but_reject_unambiguous_duplicates(authenticated, database):
    with database() as db:
        accounts = db.scalar(select(func.count()).select_from(User))
    first = create(authenticated, name="Ana", reference="EMP-9", email="ana@example.test")
    second = create(authenticated, name="Ana")
    assert first["id"] != second["id"] and first["user_id"] is None
    for values in ({"name": "Other", "reference": " emp-9 "}, {"name": "Other", "email": "ANA@example.test"}):
        response = authenticated.post("/api/v1/governance/people", json=values)
        assert response.status_code == 409 and response.json()["error"]["code"] == "PERSON_DUPLICATE"
    listed = authenticated.get("/api/v1/governance/people?search=ana&offset=1&limit=1").json()
    assert listed["total"] == 2 and len(listed["items"]) == 1
    assert authenticated.get("/api/v1/governance/people?search=%25").json()["total"] == 0
    with database() as db:
        assert db.scalar(select(func.count()).select_from(User)) == accounts
        assert db.scalar(select(func.count()).select_from(GovernancePerson)) == 2


def test_verified_account_selection_reuses_identity_without_uuid_copy_or_access_changes(authenticated, database):
    users = authenticated.get("/api/v1/governance/people/users?search=Test&limit=1").json()
    account = users["items"][0]
    with database() as db:
        user = db.get(User, account["id"])
        before = (user.password_hash, user.role_id, user.username, user.active)
    first = create(authenticated, user_id=account["id"])
    again = create(authenticated, user_id=account["id"])
    assert first["id"] == again["id"] and first["id"] != account["id"] and first["name"] == account["name"]
    with database() as db:
        user = db.get(User, account["id"])
        assert (user.password_hash, user.role_id, user.username, user.active) == before
        assert db.scalar(select(func.count()).select_from(GovernancePerson)) == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.event_type == "GOVERNANCE_PERSON_CREATED")) == 1
    assert authenticated.post("/api/v1/governance/people", json={"user_id": "foreign-or-missing"}).status_code == 422


def test_one_person_can_hold_three_roles_inactive_history_remains_visible(authenticated, database):
    person = create(authenticated, name="Data lead")
    dataset = authenticated.post("/api/v1/datasets", json={"name": "People roles"}).json()
    path = f"/api/v1/datasets/{dataset['id']}/governance"
    assignments = {f"{role}_person_id": person["id"] for role in ("business_owner", "steward", "technical_custodian")}
    assigned = authenticated.patch(path, json={"expected_version": 1, **assignments})
    assert assigned.status_code == 200, assigned.text
    governance = assigned.json()["governance"]
    assert governance["schema_version"] == 2
    assert all(governance[role]["id"] == person["id"] for role in ("business_owner", "steward", "technical_custodian"))
    assert governance["business_owner_id"] is None
    assert authenticated.patch(f"/api/v1/governance/people/{person['id']}", json={"expected_version": 1, "active": False}).status_code == 200
    retained = authenticated.patch(path, json={"expected_version": 2, **assignments, "description": "Retained historical assignments"})
    assert retained.status_code == 200 and not retained.json()["governance"]["business_owner"]["active"]
    assert authenticated.get("/api/v1/governance/people?active=true").json()["total"] == 0
    other = authenticated.post("/api/v1/datasets", json={"name": "New roles"}).json()
    assert authenticated.patch(f"/api/v1/datasets/{other['id']}/governance", json={"expected_version": 1, **assignments}).status_code == 422
    with database() as db:
        history = list(db.scalars(select(GovernanceHistory).where(GovernanceHistory.dataset_id == dataset["id"]).order_by(GovernanceHistory.version)))
        assert history[0].snapshot["business_owner"]["active"] and history[0].snapshot["business_owner"]["name"] == "Data lead"
        assert not history[-1].snapshot["business_owner"]["active"]


def test_legacy_account_assignment_is_explicitly_mapped_and_conflicts_fail(authenticated, database):
    dataset = authenticated.post("/api/v1/datasets", json={"name": "Legacy assignment"}).json()
    path = f"/api/v1/datasets/{dataset['id']}/governance"
    response = authenticated.patch(path, json={"expected_version": 1, "business_owner_id": "test-user"})
    assert response.status_code == 200
    governance = response.json()["governance"]
    person = governance["business_owner_person_id"]
    assert governance["business_owner_id"] == "test-user" and governance["business_owner"]["id"] == person
    other = create(authenticated, name="Independent")
    refused = authenticated.patch(path, json={"expected_version": 2, "business_owner_person_id": other["id"], "business_owner_id": "test-user"})
    assert refused.status_code == 422 and refused.json()["error"]["code"] == "GOVERNANCE_IDENTITY_CONFLICT"
    with database() as db:
        assert db.get(Dataset, dataset["id"]).governance_version == 2


def test_person_edits_are_optimistic_and_management_permission_is_separate(authenticated, database):
    person = create(authenticated, name="Original")
    path = f"/api/v1/governance/people/{person['id']}"
    assert authenticated.patch(path, json={"expected_version": 1, "name": "Renamed"}).status_code == 200
    assert authenticated.patch(path, json={"expected_version": 1, "name": "Stale"}).status_code == 409
    with database() as db:
        db.get(User, "test-user").role = "Data Analyst"
        db.commit()
    assert authenticated.get("/api/v1/governance/people").status_code == 200
    assert authenticated.get("/api/v1/governance/people/users").status_code == 403
    assert authenticated.post("/api/v1/governance/people", json={"name": "Denied"}).status_code == 403
    assert authenticated.patch(path, json={"expected_version": 2, "name": "Denied"}).status_code == 403
