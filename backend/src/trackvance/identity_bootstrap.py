"""Provision persisted base roles and adapt trusted pre-0.6 ORM callers.

This adapter only translates explicit legacy role assignments into role_id writes.
HTTP inputs accept role_id exclusively; authorization never reads users.role.
"""
import re
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import event, insert, inspect, select
from sqlalchemy.orm import Session

from .config import ORG_ID
from .db import utcnow
from .models import Role, RolePermission, User
from .permissions import DEFAULT_ROLE_GRANTS


def role_identifier(organization_id: str, name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"trackvance:role:{organization_id}:{name.casefold()}"))


def username_base(email: str) -> str:
    value = re.sub(r"[^a-z0-9._-]", "-", email.split("@", 1)[0].casefold()).strip(".-_")
    return (value if len(value) >= 3 else "user-" + value)[:80]


def ensure_roles(db: Session, organization_id: str) -> dict[str, str]:
    records = {role.name: role.id for role in db.scalars(select(Role).where(Role.organization_id == organization_id))}
    for name, permissions in DEFAULT_ROLE_GRANTS.items():
        if name in records:
            continue
        identity = role_identifier(organization_id, name)
        now = utcnow()
        db.execute(insert(Role).values(id=identity, organization_id=organization_id,
                   created_at=now, updated_at=now, name=name, normalized_name=name.casefold(),
                   description="Rol inicial migrado de Trackvance 0.5.1", active=True, deleted=False,
                   system_key="ADMINISTRATOR" if name == "Administrator" else None, version=1))
        if permissions:
            db.execute(insert(RolePermission), [{"role_id": identity, "permission_code": code} for code in sorted(permissions)])
        records[name] = identity
    return records


@event.listens_for(Session, "before_flush")
def adapt_legacy_user_writes(db: Session, _context, _instances) -> None:
    for user in list(db.new) + list(db.dirty):
        if not isinstance(user, User):
            continue
        state = inspect(user)
        org = user.organization_id or ORG_ID
        # New trusted ORM fixtures/bootstrap can still use the old role argument.
        # A changed legacy label is translated once to a persisted role reference.
        changed_label = state.attrs.role.history.has_changes()
        changed_id = state.attrs.role_id.history.has_changes()
        if not user.role_id or (changed_label and not changed_id):
            roles = ensure_roles(db, org)
            name = user.role or "Administrator"
            name = "Data Owner / Lead" if name == "Data Owner" else name
            if name not in roles:
                identity = role_identifier(org, name)
                now = utcnow()
                db.execute(insert(Role).values(id=identity, organization_id=org,
                           created_at=now, updated_at=now, name=name, normalized_name=name.casefold(),
                           description="Identidad histórica sin privilegios", active=False, deleted=False,
                           version=1))
                roles[name] = identity
            user.role_id = roles[name]
        if not user.username:
            base = username_base(user.email)
            occupied = set(db.scalars(select(User.username))) | {item.username for item in db.new if isinstance(item, User) and item is not user}
            value, suffix = base, 1
            while value in occupied:
                suffix += 1
                tail = f"-{suffix}"
                value = base[:80-len(tail)] + tail
            user.username = value
