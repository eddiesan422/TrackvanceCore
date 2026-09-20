"""Organization-scoped administration of local identities; no external identity provider."""

import hashlib
from datetime import datetime
from typing import Literal, cast

from argon2 import PasswordHasher
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator
from sqlalchemy import delete, select, text, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from .audit_context import Actor
from .db import get_db, iso, utcnow
from .models import AuthSession, User
from .operations_common import OperationError
from .permissions import ROLE_PERMISSIONS, permissions_for
from .services import audit

router = APIRouter(prefix="/api/v1/users", tags=["Usuarios locales"])
Role = Literal["Administrator", "Data Owner", "Data Owner / Lead", "Data Analyst", "Operations", "Auditor"]


def current_user(request: Request) -> User:
    return request.state.user


def user_dto(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role,
            "active": user.active, "permissions": permissions_for(user.role), "version": user.version,
            "created_at": iso(user.created_at), "updated_at": iso(user.updated_at),
            "password_changed_at": iso(user.password_changed_at)}


class UserResponse(BaseModel):
    id: str
    name: str
    email: str
    role: str
    active: bool
    permissions: list[str]
    version: int
    created_at: datetime
    updated_at: datetime
    password_changed_at: datetime | None


class UserListResponse(BaseModel):
    items: list[UserResponse]
    total: int


class UserInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    @field_validator("name", check_fields=False)
    @classmethod
    def clean_name(cls, value):
        if value is not None and not value.strip():
            raise ValueError("El nombre es obligatorio.")
        return value.strip() if isinstance(value, str) else value

    @field_validator("email", check_fields=False)
    @classmethod
    def valid_email(cls, value):
        if value is None:
            return value
        value = value.strip().lower()
        if value.count("@") != 1 or any(c.isspace() for c in value):
            raise ValueError("El correo no tiene un formato válido.")
        local, domain = value.split("@")
        if not local or not domain or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise ValueError("El correo no tiene un formato válido.")
        return value


class UserCreate(UserInput):
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=200)
    role: Role = "Data Analyst"
    password: SecretStr = Field(min_length=12, max_length=1024)
    active: bool = True


class UserPatch(UserInput):
    version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    email: str | None = Field(default=None, min_length=3, max_length=200)
    role: Role | None = None
    active: bool | None = None


class PasswordReset(UserInput):
    version: int = Field(ge=1)
    password: SecretStr = Field(min_length=12, max_length=1024)


def owned_user(db: Session, identity: str, actor: User) -> User:
    record = db.get(User, identity)
    if not record or record.organization_id != actor.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró el usuario.")
    return record


def lock_organization_identities(db: Session, organization_id: str) -> None:
    """Serialize last-administrator decisions, including concurrent edits of different users."""
    if db.get_bind().dialect.name == "postgresql":
        key = int.from_bytes(hashlib.sha256(f"identity:{organization_id}".encode()).digest()[:8], "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    else:
        # SQLite has one writer; take its transaction lock before checking the invariant.
        db.execute(update(User).where(User.organization_id == organization_id).values(version=User.version)
                   .execution_options(synchronize_session=False))


@router.get("", response_model=UserListResponse)
def users(active: bool | None = None, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    query = select(User).where(User.organization_id == actor.organization_id).order_by(User.name, User.id)
    if active is not None:
        query = query.where(User.active == active)
    rows = [user_dto(record) for record in db.scalars(query)]
    return {"items": rows, "total": len(rows)}


@router.get("/roles")
def roles():
    return {"items": [{"name": role, "permissions": sorted(perms)} for role, perms in ROLE_PERMISSIONS.items()
                      if role != "Data Owner"], "total": len(ROLE_PERMISSIONS) - 1}


@router.post("", status_code=201, response_model=UserResponse)
def create_user(body: UserCreate, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    if db.scalar(select(User.id).where(User.email == body.email)):
        raise OperationError(409, "USER_EMAIL_UNAVAILABLE", "El correo no está disponible.")
    now = utcnow()
    user = User(organization_id=actor.organization_id, name=body.name, email=body.email,
                role=body.role, active=body.active, password_hash=PasswordHasher().hash(body.password.get_secret_value()),
                password_changed_at=now)
    db.add(user)
    db.flush()
    audit(db, "USER_CREATED", "user", user.id, "Usuario local creado", Actor("USER", actor.id, actor.name),
          actor.organization_id, {"role": user.role, "active": user.active})
    db.commit()
    return user_dto(user)


@router.get("/{user_id}", response_model=UserResponse)
def user_detail(user_id: str, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    return user_dto(owned_user(db, user_id, actor))


@router.patch("/{user_id}", response_model=UserResponse)
def edit_user(user_id: str, body: UserPatch, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    lock_organization_identities(db, actor.organization_id)
    user = owned_user(db, user_id, actor)
    db.refresh(user)
    if user.version != body.version:
        raise OperationError(409, "VERSION_CONFLICT", "El usuario cambió. Actualiza antes de editar.")
    changes = body.model_dump(exclude={"version"}, exclude_unset=True, exclude_none=True)
    if "email" in changes and db.scalar(select(User.id).where(User.email == changes["email"], User.id != user.id)):
        raise OperationError(409, "USER_EMAIL_UNAVAILABLE", "El correo no está disponible.")
    if user.active and user.role == "Administrator" and (
        changes.get("active") is False or changes.get("role", user.role) != "Administrator"
    ):
        other = db.scalar(select(User.id).where(User.organization_id == actor.organization_id,
                                               User.role == "Administrator", User.active.is_(True), User.id != user.id))
        if not other:
            raise OperationError(422, "LAST_ADMINISTRATOR_REQUIRED", "Debe quedar al menos un administrador activo.")
    revoke = any(key in changes and changes[key] != getattr(user, key) for key in ("email", "role", "active"))
    if not changes:
        return user_dto(user)
    result = db.execute(update(User).where(User.id == user.id, User.organization_id == actor.organization_id,
                                          User.version == body.version)
                        .values(**changes, version=body.version + 1, updated_at=utcnow()).execution_options(synchronize_session=False))
    if cast(CursorResult, result).rowcount != 1:
        raise OperationError(409, "VERSION_CONFLICT", "El usuario cambió. Actualiza antes de editar.")
    if revoke:
        db.execute(delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.organization_id == actor.organization_id))
    audit(db, "USER_UPDATED", "user", user.id, "Usuario local actualizado", Actor("USER", actor.id, actor.name),
          actor.organization_id, {"changed_fields": sorted(changes), "sessions_revoked": revoke,
                                  "role": changes.get("role", user.role), "active": changes.get("active", user.active)})
    db.commit()
    db.refresh(user)
    return user_dto(user)


@router.post("/{user_id}/reset-password", response_model=UserResponse)
def reset_password(user_id: str, body: PasswordReset, db: Session = Depends(get_db), actor: User = Depends(current_user)):
    user = owned_user(db, user_id, actor)
    now = utcnow()
    digest = PasswordHasher().hash(body.password.get_secret_value())
    result = db.execute(update(User).where(User.id == user.id, User.organization_id == actor.organization_id,
                                          User.version == body.version)
                        .values(password_hash=digest, password_changed_at=now, updated_at=now,
                                version=body.version + 1).execution_options(synchronize_session=False))
    if cast(CursorResult, result).rowcount != 1:
        raise OperationError(409, "VERSION_CONFLICT", "El usuario cambió. Actualiza antes de restablecer.")
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.organization_id == actor.organization_id))
    audit(db, "USER_PASSWORD_RESET", "user", user.id, "Contraseña local restablecida y sesiones revocadas",
          Actor("USER", actor.id, actor.name), actor.organization_id, {"sessions_revoked": True})
    db.commit()
    db.refresh(user)
    return user_dto(user)
