"""Shared governance contacts. Creating a contact never creates an access account."""
from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_db
from .governance_models import GovernancePerson
from .models import Dataset, User
from .operations_common import OperationError
from .services import audit

router = APIRouter(prefix="/api/v1/governance/people", tags=["Personas de gobierno"])
ASSIGNMENTS = ("business_owner", "steward", "technical_custodian")


def normalize(value: str | None) -> str | None:
    return " ".join(value.split()).casefold() if value and value.strip() else None


def current_user(request: Request) -> User:
    return request.state.user


class PersonBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str | None = Field(default=None, min_length=1, max_length=200)
    reference: str | None = Field(default=None, max_length=320)
    email: str | None = Field(default=None, max_length=200)
    user_id: str | None = Field(default=None, max_length=64)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str | None) -> str | None:
        if value and (value.count("@") != 1 or any(char.isspace() for char in value) or not all(value.split("@"))):
            raise ValueError("Indica un correo válido o deja el campo vacío.")
        return value or None


class PersonPatch(PersonBody):
    expected_version: int = Field(ge=1)
    active: bool | None = None

    @field_validator("name", "active", mode="before")
    @classmethod
    def non_null(cls, value):
        if value is None:
            raise ValueError("Omite este campo para conservar su valor.")
        return value


def person_dto(person: GovernancePerson) -> dict:
    return {"id": person.id, "name": person.name, "reference": person.reference, "email": person.email,
            "user_id": person.user_id, "active": person.active, "version": person.version,
            "identity_kind": "PERSON"}


def verified_user(db: Session, organization_id: str, identity: str, *, active: bool = True) -> User:
    user = db.get(User, identity)
    if not user or user.organization_id != organization_id or active and (not user.active or user.deleted):
        raise OperationError(422, "PERSON_USER_INVALID", "Selecciona una cuenta activa de esta organización.")
    return user


def linked_person(db: Session, user: User) -> GovernancePerson:
    """Only a verified account identity can establish this compatibility link."""
    person = db.scalar(select(GovernancePerson).where(GovernancePerson.organization_id == user.organization_id,
                                                      GovernancePerson.user_id == user.id))
    if person:
        return person
    # Email is optional here: it may already belong to an independently created
    # contact. Never merge that contact merely because its name/email matches.
    person = GovernancePerson(organization_id=user.organization_id, user_id=user.id,
                              name=user.name, normalized_name=normalize(user.name) or user.id,
                              active=user.active and not user.deleted)
    try:
        with db.begin_nested():
            db.add(person)
            db.flush()
    except IntegrityError:
        existing = db.scalar(select(GovernancePerson).where(GovernancePerson.organization_id == user.organization_id,
                                                            GovernancePerson.user_id == user.id))
        if existing is None:
            raise OperationError(409, "PERSON_CONFLICT", "La persona cambió; vuelve a consultar el catálogo.") from None
        return existing
    audit(db, "GOVERNANCE_PERSON_LINKED", "governance_person", person.id,
          "Persona vinculada a una identidad existente", "Compatibility", user.organization_id,
          {"user_id": user.id, "contract_version": 2})
    return person


def resolve_governance_assignments(db: Session, organization_id: str, changes: dict,
                                   current: Dataset | None = None) -> dict:
    """Version 2 person assignments with explicit version 1 account compatibility."""
    values = dict(changes)
    for role in ASSIGNMENTS:
        legacy, field = f"{role}_id", f"{role}_person_id"
        old_legacy = getattr(current, legacy) if current is not None else None
        old_person = getattr(current, field) if current is not None else None
        if field in changes:
            person_id = changes[field]
            person = db.get(GovernancePerson, person_id) if person_id else None
            if person_id and (not person or person.organization_id != organization_id
                              or person_id != old_person and not person.active):
                raise OperationError(422, "RESPONSIBLE_INVALID", "Selecciona una persona activa de esta organización.")
            if legacy in changes and changes[legacy] is not None and (not person or changes[legacy] != person.user_id):
                raise OperationError(422, "GOVERNANCE_IDENTITY_CONFLICT", "La persona y la cuenta declaradas no corresponden.")
            # Keep the verified legacy association only when one actually exists.
            values[legacy] = person.user_id if person else None
        elif legacy in changes:
            account_id = changes[legacy]
            if account_id:
                try:
                    account = verified_user(db, organization_id, account_id, active=account_id != old_legacy)
                except OperationError:
                    raise OperationError(422, "RESPONSIBLE_INVALID", "Selecciona una identidad activa de la organización.") from None
                person = linked_person(db, account)
                if not person.active and person.id != old_person and account_id != old_legacy:
                    raise OperationError(422, "RESPONSIBLE_INVALID", "Esta persona está inactiva para nuevas asignaciones.")
                values[field] = person.id
            else:
                values[field] = None
    return values


@router.get("")
def people(search: str = "", active: bool | None = None, offset: int = Query(0, ge=0),
           limit: int = Query(25, ge=1, le=100), db: Session = Depends(get_db), user: User = Depends(current_user)):
    statement = select(GovernancePerson).where(GovernancePerson.organization_id == user.organization_id)
    if active is not None:
        statement = statement.where(GovernancePerson.active == active)
    if search:
        value = normalize(search) or ""
        statement = statement.where(or_(GovernancePerson.normalized_name.contains(value, autoescape=True),
            GovernancePerson.normalized_reference.contains(value, autoescape=True),
            GovernancePerson.normalized_email.contains(value, autoescape=True)))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    items = db.scalars(statement.order_by(GovernancePerson.normalized_name, GovernancePerson.id).offset(offset).limit(limit))
    return {"items": [person_dto(item) for item in items], "total": total, "offset": offset, "limit": limit}


@router.get("/users")
def available_users(search: str = "", offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100),
                    db: Session = Depends(get_db), user: User = Depends(current_user)):
    statement = select(User).where(User.organization_id == user.organization_id, User.active.is_(True), User.deleted.is_(False))
    if search:
        statement = statement.where(or_(func.lower(User.name).contains(search.lower(), autoescape=True),
                                        func.lower(User.email).contains(search.lower(), autoescape=True)))
    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    users = list(db.scalars(statement.order_by(User.name, User.id).offset(offset).limit(limit)))
    links = {item.user_id: item.id for item in db.scalars(select(GovernancePerson).where(
        GovernancePerson.organization_id == user.organization_id, GovernancePerson.user_id.in_([item.id for item in users])))}
    return {"items": [{"id": item.id, "name": item.name, "email": item.email,
                       "person_id": links.get(item.id)} for item in users], "total": total, "offset": offset, "limit": limit}


@router.get("/{identity}")
def person(identity: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = db.get(GovernancePerson, identity)
    if item is None or item.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró la persona.")
    return person_dto(item)


@router.post("", status_code=201)
def create_person(body: PersonBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    account = verified_user(db, user.organization_id, body.user_id) if body.user_id else None
    if account:
        existing = db.scalar(select(GovernancePerson).where(GovernancePerson.organization_id == user.organization_id,
                                                           GovernancePerson.user_id == account.id))
        if existing:
            return person_dto(existing)
    name = body.name or (account.name if account else None)
    if not name:
        raise OperationError(422, "PERSON_NAME_REQUIRED", "Escribe un nombre o selecciona una cuenta existente.")
    item = GovernancePerson(organization_id=user.organization_id, name=name, normalized_name=normalize(name),
        reference=body.reference or None, normalized_reference=normalize(body.reference), email=body.email,
        normalized_email=normalize(body.email), user_id=body.user_id)
    db.add(item)
    try:
        db.flush()
        audit(db, "GOVERNANCE_PERSON_CREATED", "governance_person", item.id, "Persona de gobierno creada",
              user.name, user.organization_id, {"user_id": item.user_id, "version": 1})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise OperationError(409, "PERSON_DUPLICATE", "La referencia, correo o cuenta ya identifica otra persona. Selecciónala en el catálogo.") from None
    return person_dto(item)


@router.patch("/{identity}")
def update_person(identity: str, body: PersonPatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    item = db.get(GovernancePerson, identity)
    if item is None or item.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró la persona.")
    changes: dict[str, Any] = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    if changes.get("user_id"):
        verified_user(db, user.organization_id, changes["user_id"], active=changes["user_id"] != item.user_id)
    for field in ("name", "reference", "email"):
        if field in changes:
            if field != "name":
                changes[field] = changes[field] or None
            changes[f"normalized_{field}"] = normalize(changes[field])
    try:
        result = db.execute(update(GovernancePerson).where(GovernancePerson.id == identity,
            GovernancePerson.organization_id == user.organization_id, GovernancePerson.version == body.expected_version)
            .values(**changes, version=body.expected_version + 1).execution_options(synchronize_session=False))
        if cast(CursorResult, result).rowcount != 1:
            raise OperationError(409, "VERSION_CONFLICT", "La persona cambió. Actualiza antes de guardar.")
        db.refresh(item)
        audit(db, "GOVERNANCE_PERSON_UPDATED", "governance_person", identity, "Persona de gobierno actualizada",
              user.name, user.organization_id, {"version": item.version, "active": item.active})
        db.commit()
    except IntegrityError:
        db.rollback()
        raise OperationError(409, "PERSON_DUPLICATE", "Esta referencia, correo o cuenta ya pertenece a otra persona.") from None
    return person_dto(item)
