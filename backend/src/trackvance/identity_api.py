"""Organization-scoped roles, one-time credential issuance and historical metadata."""
import hashlib
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import datetime, timedelta
from typing import Literal

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .audit_context import Actor
from .db import SessionLocal, get_db, iso, utcnow
from .models import (
    AuthSession,
    ExternalIdentity,
    NotificationDeliveryRecord,
    Role,
    RolePermission,
    User,
)
from .notifications import delivery_dto, historical_notification_status
from .operations_common import OperationError
from .permissions import (
    CATALOG,
    NON_DELEGABLE,
    catalog_dto,
    dependency_closure,
    effective_permissions,
    permissions_for_role,
)
from .services import audit

router = APIRouter(prefix="/api/v1", tags=["Identidad y RBAC"])


def current_user(request: Request) -> User:
    return request.state.user


def actor_of(user: User) -> Actor:
    return Actor("USER", user.id, user.name)


def user_dto(user: User, db: Session | None = None) -> dict:
    if db is None:
        with SessionLocal() as local:
            return user_dto(user, local)
    role = db.get(Role, user.role_id)
    identities = db.scalars(select(ExternalIdentity).where(ExternalIdentity.user_id == user.id,
        ExternalIdentity.organization_id == user.organization_id).order_by(ExternalIdentity.provider)).all()
    return {"id": user.id, "name": user.name, "first_name": user.first_name, "last_name": user.last_name,
        "username": user.username, "email": user.email, "role": role.name if role else "",
        "role_id": user.role_id, "role_version": role.version if role else 0,
        "active": user.active, "deleted": user.deleted, "permissions": effective_permissions(db, user),
        "version": user.version, "created_at": iso(user.created_at), "updated_at": iso(user.updated_at),
        "password_changed_at": iso(user.password_changed_at), "deleted_at": iso(user.deleted_at),
        "last_login_at": iso(user.last_login_at),
        "must_change_password": user.must_change_password,
        "temporary_password_expires_at": iso(user.temporary_password_expires_at),
        "external_identities": [{"id": row.id, "provider": row.provider, "issuer": row.issuer,
            "subject": row.subject, "email_at_link": row.email_at_link,
            "linked_at": iso(row.linked_at), "last_login_at": iso(row.last_login_at)} for row in identities]}


class ExternalIdentityResponse(BaseModel):
    id: str
    provider: str
    issuer: str
    subject: str
    email_at_link: str
    linked_at: datetime
    last_login_at: datetime | None


class NotificationResponse(BaseModel):
    id: str
    organization_id: str
    event_type: str
    template_key: str
    channel: str
    recipient_type: str
    recipient_user_id: str
    recipient_email_snapshot: str
    provider_key: str
    attempt_number: int
    status: str
    error_code: str | None
    created_at: datetime
    sent_at: datetime | None
    failed_at: datetime | None


class UserResponse(BaseModel):
    id: str
    name: str
    first_name: str | None
    last_name: str | None
    username: str
    email: str
    role: str
    role_id: str
    role_version: int
    active: bool
    deleted: bool
    permissions: list[str]
    version: int
    created_at: datetime
    updated_at: datetime
    password_changed_at: datetime | None
    deleted_at: datetime | None
    last_login_at: datetime | None
    must_change_password: bool
    temporary_password_expires_at: datetime | None
    external_identities: list[ExternalIdentityResponse]


class TemporaryCredentialsResponse(BaseModel):
    """Ephemeral disclosure; never attach this model to persisted user metadata."""

    username: str
    temporary_password: str = Field(repr=False)
    expires_at: datetime
    must_change_password: Literal[True] = True


class UserCredentialIssueResponse(BaseModel):
    user: UserResponse
    temporary_credentials: TemporaryCredentialsResponse


@contextmanager
def credential_write_boundary(db: Session, *, code: str = "CREDENTIAL_ISSUE_FAILED",
                              message: str = "No se pudieron emitir las credenciales. Actualiza el usuario y vuelve a intentar.") -> Iterator[None]:
    """Do not pass secret-bearing hash/DB exceptions to the general error logger."""
    try:
        yield
    except (OperationError, IntegrityError):
        raise  # Both have sanitized HTTP handlers without exception logging.
    except Exception:  # noqa: BLE001 - secret issuance is a deliberately opaque boundary.
        with suppress(Exception):
            db.rollback()
        raise OperationError(500, code, message) from None


def credential_issue_response(db: Session, user: User, password: str, response: Response) -> UserCredentialIssueResponse:
    assert user.temporary_password_expires_at is not None
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return UserCredentialIssueResponse(user=UserResponse.model_validate(user_dto(user, db)),
        temporary_credentials=TemporaryCredentialsResponse(username=user.username,
            temporary_password=password, expires_at=user.temporary_password_expires_at))


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int


class RoleResponse(BaseModel):
    id: str
    name: str
    description: str
    active: bool
    deleted: bool
    system_key: str | None
    protected: bool
    version: int
    user_count: int
    permissions: list[str]


class RoleListResponse(BaseModel):
    items: list[RoleResponse]
    total: int


class PermissionResponse(BaseModel):
    code: str
    group: str
    label: str
    dependencies: list[str]
    delegable: bool


class PermissionListResponse(BaseModel):
    items: list[PermissionResponse]
    total: int


class NotificationStatusResponse(BaseModel):
    enabled: bool
    configured: bool
    provider: str
    security: str
    from_address: str
    from_name: str
    availability: Literal["HISTORICAL_ONLY"]


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    total: int


class IdentityInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("email", check_fields=False)
    @classmethod
    def email_valid(cls, value):
        if value is None:
            return value
        value = value.strip().casefold()
        if not re.fullmatch(r"[^\s@]+@[^\s@.]+(?:\.[^\s@.]+)+", value):
            raise ValueError("El correo no tiene un formato válido.")
        return value

    @field_validator("username", check_fields=False)
    @classmethod
    def username_valid(cls, value):
        if value is None:
            return value
        value = value.strip().lower()
        if not re.fullmatch(r"[a-z0-9._-]{3,80}", value):
            raise ValueError("Usa entre 3 y 80 letras, números, puntos, guiones o guiones bajos.")
        return value


class UserCreate(IdentityInput):
    first_name: str = Field(min_length=1, max_length=100)
    last_name: str = Field(min_length=1, max_length=100)
    username: str = Field(min_length=3, max_length=80)
    email: str = Field(min_length=3, max_length=200)
    role_id: str = Field(min_length=1, max_length=64)
    active: bool = True


class VersionInput(IdentityInput):
    version: int = Field(ge=1)


class UserPatch(VersionInput):
    first_name: str | None = Field(default=None, min_length=1, max_length=100)
    last_name: str | None = Field(default=None, min_length=1, max_length=100)
    username: str | None = Field(default=None, min_length=3, max_length=80)
    email: str | None = Field(default=None, min_length=3, max_length=200)
    role_id: str | None = Field(default=None, min_length=1, max_length=64)
    active: bool | None = None


class RoleCreate(IdentityInput):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    permissions: list[str] = Field(default_factory=list, max_length=100)
    active: bool = True


class RolePatch(VersionInput):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    permissions: list[str] | None = Field(default=None, max_length=100)
    active: bool | None = None


def lock_organization_identities(db: Session, organization_id: str) -> None:
    if db.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(f"identity:{organization_id}".encode()).digest()[:8], "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    else:
        db.execute(update(User).where(User.organization_id == organization_id).values(version=User.version).execution_options(synchronize_session=False))


def owned_user(db: Session, identity: str, actor: User, *, deleted: bool = False) -> User:
    user = db.get(User, identity)
    if not user or user.organization_id != actor.organization_id or (user.deleted and not deleted):
        raise OperationError(404, "NOT_FOUND", "No se encontró el usuario.")
    return user


def owned_role(db: Session, identity: str, actor: User, *, assign: bool = False) -> Role:
    role = db.get(Role, identity)
    if not role or role.organization_id != actor.organization_id or role.deleted or (assign and not role.active):
        raise OperationError(422 if assign else 404, "ROLE_UNAVAILABLE", "El rol no está disponible.")
    return role


def assert_version(record, version: int) -> None:
    if record.version != version:
        raise OperationError(409, "VERSION_CONFLICT", "El registro cambió. Actualiza antes de editar.")


def role_dto(db: Session, role: Role) -> dict:
    count = db.scalar(select(func.count()).select_from(User).where(User.role_id == role.id, User.deleted.is_(False))) or 0
    return {"id": role.id, "name": role.name, "description": role.description, "active": role.active,
            "deleted": role.deleted, "system_key": role.system_key, "protected": role.system_key == "ADMINISTRATOR",
            "version": role.version, "user_count": count, "permissions": permissions_for_role(db, role)}


def valid_grants(grants: list[str]) -> set[str]:
    values = set(grants)
    if values - CATALOG:
        raise OperationError(422, "UNKNOWN_PERMISSION", "El permiso no pertenece al catálogo del producto.")
    if values & NON_DELEGABLE:
        raise OperationError(422, "NON_DELEGABLE_PERMISSION", "La administración de usuarios y roles es exclusiva de Administrator.")
    missing = dependency_closure(values) - values
    if missing:
        raise OperationError(422, "PERMISSION_DEPENDENCIES_REQUIRED", "Incluye los permisos requeridos.", {"missing": sorted(missing)})
    return values


def assert_role_can_retire(db: Session, role: Role) -> None:
    if role.system_key == "ADMINISTRATOR":
        raise OperationError(422, "PROTECTED_ROLE", "Administrator es un rol protegido del sistema.")
    if db.scalar(select(User.id).where(User.role_id == role.id, User.deleted.is_(False))):
        raise OperationError(422, "ROLE_HAS_USERS", "El rol tiene usuarios asociados, incluidos usuarios desactivados.")


@router.get("/roles/permissions", response_model=PermissionListResponse)
def permission_catalog():
    items = catalog_dto()
    return {"items": items, "total": len(items)}


@router.get("/roles", response_model=RoleListResponse)
@router.get("/users/roles", response_model=RoleListResponse)
def roles(search: str = "", db: Session = Depends(get_db), actor: User = Depends(current_user)):
    rows = db.scalars(select(Role).where(Role.organization_id == actor.organization_id, Role.deleted.is_(False)).order_by(Role.name))
    items = [role_dto(db, row) for row in rows if search.casefold() in row.name.casefold()]
    return {"items": items, "total": len(items)}


@router.get("/roles/{role_id}", response_model=RoleResponse)
def role_detail(role_id: str, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    return role_dto(db, owned_role(db, role_id, actor))


@router.post("/roles", status_code=201, response_model=RoleResponse)
def create_role(body: RoleCreate, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    grants = valid_grants(body.permissions)
    lock_organization_identities(db, actor.organization_id)
    role = Role(organization_id=actor.organization_id, name=body.name, normalized_name=body.name.casefold(),
                description=body.description, active=body.active)
    db.add(role)
    db.flush()
    db.add_all([RolePermission(role_id=role.id, permission_code=code) for code in sorted(grants)])
    audit(db, "ROLE_CREATED", "role", role.id, "Rol creado", actor_of(actor), actor.organization_id, {"permissions": sorted(grants)})
    db.commit()
    return role_dto(db, role)


@router.patch("/roles/{role_id}", response_model=RoleResponse)
def edit_role(role_id: str, body: RolePatch, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    lock_organization_identities(db, actor.organization_id)
    role = owned_role(db, role_id, actor)
    db.refresh(role)
    assert_version(role, body.version)
    changes = body.model_dump(exclude={"version"}, exclude_none=True, exclude_unset=True)
    if role.system_key == "ADMINISTRATOR" and any(key in changes for key in ("name", "active", "permissions")):
        raise OperationError(422, "PROTECTED_ROLE", "Administrator conserva su identidad, actividad y todos los permisos.")
    if changes.get("active") is False:
        assert_role_can_retire(db, role)
    if "permissions" in changes:
        grants = valid_grants(changes.pop("permissions"))
        db.execute(delete(RolePermission).where(RolePermission.role_id == role.id))
        db.add_all([RolePermission(role_id=role.id, permission_code=code) for code in sorted(grants)])
        audit(db, "ROLE_PERMISSIONS_CHANGED", "role", role.id, "Permisos del rol actualizados", actor_of(actor), actor.organization_id, {"permissions": sorted(grants)})
    for key, value in changes.items():
        setattr(role, key, value)
    role.normalized_name, role.version, role.updated_at = role.name.casefold(), role.version + 1, utcnow()
    event = "ROLE_DISABLED" if changes.get("active") is False else "ROLE_UPDATED"
    audit(db, event, "role", role.id, "Rol actualizado", actor_of(actor), actor.organization_id, {"version": role.version})
    db.commit()
    return role_dto(db, role)


@router.delete("/roles/{role_id}", response_model=RoleResponse)
def delete_role(role_id: str, body: VersionInput, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    lock_organization_identities(db, actor.organization_id)
    role = owned_role(db, role_id, actor)
    assert_version(role, body.version)
    assert_role_can_retire(db, role)
    role.deleted, role.active, role.deleted_at, role.updated_at = True, False, utcnow(), utcnow()
    role.version += 1
    audit(db, "ROLE_DELETED", "role", role.id, "Baja lógica del rol", actor_of(actor), actor.organization_id)
    db.commit()
    return role_dto(db, role)


@router.get("/users", response_model=UserListResponse)
def users(active: bool | None = None, search: str = "", include_deleted: bool = False,
          db: Session = Depends(get_db), actor: User = Depends(current_user)):
    query = select(User).where(User.organization_id == actor.organization_id).order_by(User.name, User.id)
    if not include_deleted:
        query = query.where(User.deleted.is_(False))
    if active is not None:
        query = query.where(User.active == active)
    items = [user_dto(row, db) for row in db.scalars(query) if search.casefold() in f"{row.name} {row.username} {row.email}".casefold()]
    return {"items": items, "total": len(items)}


def assert_unique(db: Session, username: str, email: str, user_id: str = "") -> None:
    if db.scalar(select(User.id).where(func.lower(User.email) == email, User.id != user_id)):
        raise OperationError(409, "USER_EMAIL_UNAVAILABLE", "El correo no está disponible.")
    if db.scalar(select(User.id).where(func.lower(User.username) == username, User.id != user_id)):
        raise OperationError(409, "USER_USERNAME_UNAVAILABLE", "El username no está disponible.")


@router.post("/users", status_code=201, response_model=UserCredentialIssueResponse)
def create_user(body: UserCreate, response: Response, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    lock_organization_identities(db, actor.organization_id)
    role = owned_role(db, body.role_id, actor, assign=True)
    assert_unique(db, body.username, body.email)
    with credential_write_boundary(db):
        password = secrets.token_urlsafe(24)
        user = User(organization_id=actor.organization_id, name=f"{body.first_name} {body.last_name}",
            first_name=body.first_name, last_name=body.last_name, username=body.username, email=body.email,
            role_id=role.id, role=role.name[:40], active=body.active, password_hash=PasswordHasher().hash(password),
            must_change_password=True, temporary_password_expires_at=utcnow() + timedelta(hours=24))
        db.add(user)
        db.flush()
        audit(db, "USER_CREATED", "user", user.id, "Usuario pre-provisionado", actor_of(actor), actor.organization_id,
              {"role_id": role.id, "active": user.active})
        db.commit()
        return credential_issue_response(db, user, password, response)


@router.get("/users/{user_id}", response_model=UserResponse)
def user_detail(user_id: str, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    return user_dto(owned_user(db, user_id, actor, deleted=True), db)


def preserve_administrator(db: Session, user: User, *, role_id: str, active: bool) -> None:
    role = db.get(Role, user.role_id)
    if not user.active or user.deleted or role is None or role.system_key != "ADMINISTRATOR":
        return
    target = db.get(Role, role_id)
    if active and target is not None and target.system_key == "ADMINISTRATOR":
        return
    another = db.scalar(select(User.id).join(Role, Role.id == User.role_id).where(
        User.organization_id == user.organization_id, User.id != user.id,
        User.active.is_(True), User.deleted.is_(False), Role.system_key == "ADMINISTRATOR"))
    if not another:
        raise OperationError(422, "LAST_ADMINISTRATOR_REQUIRED", "Debe quedar al menos un administrador activo.")


def revoke_sessions(db: Session, user: User) -> None:
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id))


@router.patch("/users/{user_id}", response_model=UserResponse)
def edit_user(user_id: str, body: UserPatch, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    lock_organization_identities(db, actor.organization_id)
    user = owned_user(db, user_id, actor)
    db.refresh(user)
    assert_version(user, body.version)
    changes = body.model_dump(exclude={"version"}, exclude_none=True, exclude_unset=True)
    assert_unique(db, changes.get("username", user.username), changes.get("email", user.email), user.id)
    new_role = owned_role(db, changes["role_id"], actor, assign=True) if "role_id" in changes else None
    preserve_administrator(db, user, role_id=changes.get("role_id", user.role_id), active=changes.get("active", user.active))
    if "first_name" in changes or "last_name" in changes:
        first, last = changes.get("first_name", user.first_name), changes.get("last_name", user.last_name)
        if not first or not last:
            raise OperationError(422, "FULL_NAME_REQUIRED", "Indica nombres y apellidos para actualizar la cuenta histórica.")
        changes["name"] = f"{first} {last}"
    role_changed = "role_id" in changes and user.role_id != changes["role_id"]
    revoke = any(key in changes and changes[key] != getattr(user, key) for key in ("role_id", "active", "username", "email"))
    if new_role and role_changed:
        changes["role"] = new_role.name[:40]
    for key, value in changes.items():
        setattr(user, key, value)
    user.version, user.updated_at = user.version + 1, utcnow()
    if revoke:
        revoke_sessions(db, user)
    event = "USER_ROLE_CHANGED" if role_changed else "USER_DISABLED" if changes.get("active") is False else "USER_UPDATED"
    audit(db, event, "user", user.id, "Usuario actualizado", actor_of(actor), actor.organization_id,
          {"changed_fields": sorted(changes), "sessions_revoked": revoke, "role_id": user.role_id})
    db.commit()
    return user_dto(user, db)


@router.delete("/users/{user_id}", response_model=UserResponse)
def delete_user(user_id: str, body: VersionInput, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    lock_organization_identities(db, actor.organization_id)
    user = owned_user(db, user_id, actor)
    assert_version(user, body.version)
    preserve_administrator(db, user, role_id=user.role_id, active=False)
    if user.id == actor.id:
        raise OperationError(422, "SELF_DELETE_FORBIDDEN", "Otro administrador debe gestionar la baja de tu cuenta.")
    user.active, user.deleted, user.deleted_at, user.updated_at = False, True, utcnow(), utcnow()
    user.version += 1
    revoke_sessions(db, user)
    audit(db, "USER_DELETED", "user", user.id, "Baja lógica del usuario", actor_of(actor), actor.organization_id, {"sessions_revoked": True})
    db.commit()
    return user_dto(user, db)


@router.post("/users/{user_id}/regenerate-credentials", response_model=UserCredentialIssueResponse)
@router.post("/users/{user_id}/resend-credentials", response_model=UserCredentialIssueResponse, deprecated=True)
@router.post("/users/{user_id}/reset-password", response_model=UserCredentialIssueResponse, deprecated=True)
def regenerate_credentials(user_id: str, body: VersionInput, response: Response,
                           db: Session = Depends(get_db), actor: User = Depends(current_user)):
    lock_organization_identities(db, actor.organization_id)
    user = owned_user(db, user_id, actor)
    assert_version(user, body.version)
    with credential_write_boundary(db):
        password = secrets.token_urlsafe(24)
        user.password_hash = PasswordHasher().hash(password)
        user.must_change_password, user.temporary_password_expires_at = True, utcnow() + timedelta(hours=24)
        user.version, user.updated_at = user.version + 1, utcnow()
        revoke_sessions(db, user)
        audit(db, "USER_CREDENTIALS_REGENERATED", "user", user.id, "Credenciales regeneradas", actor_of(actor), actor.organization_id, {"sessions_revoked": True})
        db.commit()
        return credential_issue_response(db, user, password, response)


@router.delete("/users/{user_id}/external-identities/{identity_id}", response_model=UserResponse)
def unlink_identity(user_id: str, identity_id: str, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    lock_organization_identities(db, actor.organization_id)
    user = owned_user(db, user_id, actor)
    link = db.get(ExternalIdentity, identity_id)
    if not link or link.user_id != user.id or link.organization_id != actor.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró el vínculo.")
    provider = link.provider
    db.delete(link)
    revoke_sessions(db, user)
    audit(db, "SSO_UNLINKED", "user", user.id, "Identidad externa desvinculada", actor_of(actor), actor.organization_id,
          {"provider": provider, "sessions_revoked": True})
    db.commit()
    return user_dto(user, db)


@router.get("/notifications/status", response_model=NotificationStatusResponse, deprecated=True)
def notification_status():
    return historical_notification_status()


@router.get("/notifications/deliveries", response_model=NotificationListResponse, deprecated=True)
def notification_deliveries(db: Session = Depends(get_db), actor: User = Depends(current_user)):
    rows = db.scalars(select(NotificationDeliveryRecord).where(NotificationDeliveryRecord.organization_id == actor.organization_id)
                      .order_by(NotificationDeliveryRecord.created_at.desc()).limit(200))
    items = [delivery_dto(row) for row in rows]
    return {"items": items, "total": len(items)}
