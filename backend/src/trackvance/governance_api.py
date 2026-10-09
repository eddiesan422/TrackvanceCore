"""Catalog navigation is metadata-only, paginated and organization scoped."""

from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import String, and_, case, delete, func, literal, or_, select, update
from sqlalchemy import cast as sql_cast
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from .db import get_db, iso, utcnow
from .governance import (
    authorize_dataset,
    dataset_eligibility,
    governance_snapshot,
    update_governance,
    validate_classification,
)
from .governance_models import (
    ColumnDocumentation,
    DataDomain,
    DatasetBlock,
    DatasetSecurityDependency,
    GlossaryAssociation,
    GlossaryTerm,
    GovernanceHistory,
    MacroDomain,
    StrictApproval,
)
from .models import ArtifactLink, Configuration, Dataset, DatasetVersion, Run, User
from .operations_common import OperationError
from .permissions import effective_permissions
from .services import audit, dataset_dto, run_dto, version_dto

router = APIRouter(prefix="/api/v1", tags=["Catálogo y gobierno"])


def current_user(request: Request) -> User:
    return request.state.user


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class DomainBody(Input):
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=4000)
    macro_domain_id: str | None = Field(default=None, max_length=64)


class DomainPatch(Input):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    active: bool | None = None

    @field_validator("name", "description", "active", mode="before")
    @classmethod
    def non_null(cls, value):
        if value is None:
            raise ValueError("Este campo no admite null; omítelo para conservar su valor.")
        return value


class GovernancePatch(Input):
    expected_version: int = Field(ge=1)
    macro_domain_id: str | None = Field(default=None, max_length=64)
    domain_id: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=4000)
    business_owner_id: str | None = Field(default=None, max_length=64)
    steward_id: str | None = Field(default=None, max_length=64)
    technical_custodian_id: str | None = Field(default=None, max_length=64)
    business_owner_person_id: str | None = Field(default=None, max_length=64)
    steward_person_id: str | None = Field(default=None, max_length=64)
    technical_custodian_person_id: str | None = Field(default=None, max_length=64)
    information_classification: Literal["UNKNOWN", "PUBLIC", "INTERNAL", "CONFIDENTIAL", "RESTRICTED"] | None = None
    criticality: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"] | None = None

    @field_validator("description", "information_classification", "criticality", mode="before")
    @classmethod
    def non_null(cls, value):
        if value is None:
            raise ValueError("Este campo no admite null; omítelo para conservar su valor.")
        return value


class TermBody(Input):
    name: str = Field(min_length=1, max_length=160)
    definition: str = Field(min_length=1, max_length=8000)


class TermPatch(Input):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=1, max_length=160)
    definition: str | None = Field(default=None, min_length=1, max_length=8000)
    active: bool | None = None

    @field_validator("name", "definition", "active", mode="before")
    @classmethod
    def non_null(cls, value):
        if value is None:
            raise ValueError("Este campo no admite null; omítelo para conservar su valor.")
        return value


class ColumnBody(Input):
    version_id: str = Field(max_length=64)
    column_name: str = Field(min_length=1, max_length=240)
    description: str = Field(default="", max_length=8000)
    term_ids: list[str] = Field(default_factory=list, max_length=32)
    expected_version: int = Field(ge=0)


class AssociationBody(Input):
    term_id: str = Field(max_length=64)
    version_id: str | None = Field(default=None, max_length=64)
    column_name: str | None = Field(default=None, max_length=240)


class BlockBody(Input):
    scope: Literal["REPORT", "CONTENT"]
    reason: str = Field(min_length=1, max_length=1000)


class ReleaseBody(Input):
    expected_version: int = Field(ge=1)
    active: Literal[False] = False
    reason: str = Field(min_length=1, max_length=1000)


def normalized(label: str) -> str:
    return " ".join(label.split()).casefold()


def owned(db: Session, model, identity: str, user: User):
    item = db.get(model, identity)
    if item is None or item.organization_id != user.organization_id:
        raise OperationError(404, "NOT_FOUND", "No se encontró el recurso solicitado.")
    return item


def dto(item) -> dict:
    result = {"id": item.id, "name": item.name, "active": item.active, "version": item.version}
    if isinstance(item, GlossaryTerm):
        result["definition"] = item.definition
    else:
        result["description"] = item.description
    if isinstance(item, DataDomain):
        result["macro_domain_id"] = item.macro_domain_id
    return result


def page(db: Session, statement, offset: int, limit: int, serializer=dto) -> dict:
    total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
    return {"items": [serializer(item) for item in db.scalars(statement.offset(offset).limit(limit))], "total": total,
            "offset": offset, "limit": limit}


@router.get("/governance/macrodomains")
@router.get("/catalog/macrodomains")
def macro_domains(active: bool | None = None, search: str = "", offset: int = Query(0, ge=0),
                  limit: int = Query(100, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(current_user)):
    statement = select(MacroDomain).where(MacroDomain.organization_id == user.organization_id)
    if active is not None:
        statement = statement.where(MacroDomain.active == active)
    if search:
        statement = statement.where(MacroDomain.normalized_name.contains(normalized(search), autoescape=True))
    return page(db, statement.order_by(MacroDomain.name, MacroDomain.id), offset, limit)


@router.get("/governance/domains")
@router.get("/catalog/domains")
def domains(macro_domain_id: str | None = None, active: bool | None = None, search: str = "",
            offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200),
            db: Session = Depends(get_db), user: User = Depends(current_user)):
    statement = select(DataDomain).join(MacroDomain).where(DataDomain.organization_id == user.organization_id,
        MacroDomain.organization_id == user.organization_id)
    if macro_domain_id:
        statement = statement.where(DataDomain.macro_domain_id == macro_domain_id)
    if active is not None:
        statement = statement.where(DataDomain.active == active)
        if active:
            statement = statement.where(MacroDomain.active.is_(True))
    if search:
        statement = statement.where(DataDomain.normalized_name.contains(normalized(search), autoescape=True))
    return page(db, statement.order_by(DataDomain.name, DataDomain.id), offset, limit)


def create_domain(model, body: DomainBody, db: Session, user: User):
    if model is DataDomain:
        if not body.macro_domain_id:
            raise OperationError(422, "MACRODOMAIN_REQUIRED", "Selecciona el macrodominio del nuevo dominio.")
        validate_classification(db, user.organization_id, body.macro_domain_id, None)
    elif body.macro_domain_id:
        raise OperationError(422, "MACRODOMAIN_INVALID", "Un macrodominio no tiene macrodominio padre.")
    values: dict[str, Any] = {"name": body.name, "description": body.description, "normalized_name": normalized(body.name)}
    if model is DataDomain:
        values["macro_domain_id"] = body.macro_domain_id
    item = model(organization_id=user.organization_id, **values)
    db.add(item)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise OperationError(409, "DOMAIN_DUPLICATE", "Ya existe esta etiqueta en el ámbito seleccionado.") from None
    audit(db, "DOMAIN_CREATED", model.__tablename__, item.id, "Clasificación controlada creada", user.name, user.organization_id)
    db.commit()
    return dto(item)


@router.post("/governance/macrodomains", status_code=201)
@router.post("/catalog/macrodomains", status_code=201)
def create_macro(body: DomainBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return create_domain(MacroDomain, body, db, user)


@router.post("/governance/domains", status_code=201)
@router.post("/catalog/domains", status_code=201)
def create_data_domain(body: DomainBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return create_domain(DataDomain, body, db, user)


def optimistic_edit(db: Session, item, expected_version: int, changes: dict, user: User):
    changes = {key: value for key, value in changes.items() if value is not None}
    if "name" in changes:
        changes["normalized_name"] = normalized(changes["name"])
    result = db.execute(update(type(item)).where(type(item).id == item.id, type(item).organization_id == user.organization_id,
        type(item).version == expected_version).values(**changes, version=expected_version + 1)
        .execution_options(synchronize_session=False))
    if cast(CursorResult, result).rowcount != 1:
        raise OperationError(409, "VERSION_CONFLICT", "Otra persona modificó el recurso. Actualiza antes de guardar.")
    db.refresh(item)
    audit(db, "GOVERNANCE_RESOURCE_UPDATED", item.__tablename__, item.id, "Recurso de gobierno actualizado", user.name,
          user.organization_id, {"version": item.version})
    db.commit()
    return dto(item)


@router.patch("/governance/macrodomains/{identity}")
@router.patch("/catalog/macrodomains/{identity}")
def edit_macro(identity: str, body: DomainPatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return optimistic_edit(db, owned(db, MacroDomain, identity, user), body.expected_version,
                           body.model_dump(exclude={"expected_version"}, exclude_unset=True), user)


@router.patch("/governance/domains/{identity}")
@router.patch("/catalog/domains/{identity}")
def edit_domain(identity: str, body: DomainPatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return optimistic_edit(db, owned(db, DataDomain, identity, user), body.expected_version,
                           body.model_dump(exclude={"expected_version"}, exclude_unset=True), user)


@router.patch("/datasets/{dataset_id}/governance")
def edit_governance(dataset_id: str, body: GovernancePatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dataset = owned(db, Dataset, dataset_id, user)
    authorize_dataset(db, user, dataset.id, "METADATA")
    update_governance(db, dataset, user, body.expected_version,
                      body.model_dump(exclude={"expected_version"}, exclude_unset=True))
    db.commit()
    return {"dataset": dataset_dto(db, dataset), "governance": governance_snapshot(db, dataset)}


@router.get("/governance/glossary")
def glossary(search: str = "", active: bool | None = None, offset: int = Query(0, ge=0),
             limit: int = Query(100, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(current_user)):
    statement = select(GlossaryTerm).where(GlossaryTerm.organization_id == user.organization_id)
    if search:
        statement = statement.where(GlossaryTerm.normalized_name.contains(normalized(search), autoescape=True))
    if active is not None:
        statement = statement.where(GlossaryTerm.active == active)
    return page(db, statement.order_by(GlossaryTerm.name, GlossaryTerm.id), offset, limit)


@router.post("/governance/glossary", status_code=201)
def create_term(body: TermBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    term = GlossaryTerm(organization_id=user.organization_id, normalized_name=normalized(body.name), **body.model_dump())
    db.add(term)
    db.flush()
    audit(db, "GLOSSARY_TERM_CREATED", "glossary_term", term.id, "Término creado", user.name, user.organization_id)
    db.commit()
    return dto(term)


@router.patch("/governance/glossary/{identity}")
def edit_term(identity: str, body: TermPatch, db: Session = Depends(get_db), user: User = Depends(current_user)):
    return optimistic_edit(db, owned(db, GlossaryTerm, identity, user), body.expected_version,
                           body.model_dump(exclude={"expected_version"}, exclude_unset=True), user)


@router.patch("/catalog/datasets/{dataset_id}/columns")
def edit_column(dataset_id: str, body: ColumnBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dataset = owned(db, Dataset, dataset_id, user)
    authorize_dataset(db, user, dataset.id, "METADATA")
    version = owned(db, DatasetVersion, body.version_id, user)
    if version.dataset_id != dataset.id or body.column_name not in {item["name"] for item in version.schema_json}:
        raise OperationError(422, "COLUMN_NOT_IN_SCHEMA", "La columna no pertenece a este esquema y versión.")
    terms = [owned(db, GlossaryTerm, identity, user) for identity in set(body.term_ids)]
    associated = set(db.scalars(select(GlossaryAssociation.term_id).where(GlossaryAssociation.dataset_id == dataset.id,
        GlossaryAssociation.dataset_version_id == version.id, GlossaryAssociation.column_name == body.column_name)))
    if any(not term.active and term.id not in associated for term in terms):
        raise OperationError(422, "TERM_INACTIVE", "No se pueden asociar términos desactivados.")
    documentation = db.scalar(select(ColumnDocumentation).where(ColumnDocumentation.dataset_version_id == version.id,
        ColumnDocumentation.column_name == body.column_name))
    if documentation:
        result = db.execute(update(ColumnDocumentation).where(ColumnDocumentation.id == documentation.id,
            ColumnDocumentation.version == body.expected_version).values(description=body.description,
            version=body.expected_version + 1).execution_options(synchronize_session=False))
        if cast(CursorResult, result).rowcount != 1:
            raise OperationError(409, "VERSION_CONFLICT", "La documentación cambió. Actualiza antes de guardar.")
        db.refresh(documentation)
    elif body.expected_version != 0:
        raise OperationError(409, "VERSION_CONFLICT", "La documentación no existe en esta versión.")
    else:
        documentation = ColumnDocumentation(organization_id=user.organization_id, dataset_version_id=version.id,
            schema_hash=version.schema_hash, column_name=body.column_name, description=body.description)
        db.add(documentation)
        db.flush()
    db.execute(delete(GlossaryAssociation).where(GlossaryAssociation.dataset_id == dataset.id,
        GlossaryAssociation.dataset_version_id == version.id, GlossaryAssociation.column_name == body.column_name))
    for term in terms:
        db.add(GlossaryAssociation(organization_id=user.organization_id, term_id=term.id, dataset_id=dataset.id,
            dataset_version_id=version.id, column_name=body.column_name))
    audit(db, "COLUMN_DOCUMENTATION_UPDATED", "dataset_version", version.id, "Diccionario funcional actualizado", user.name,
          user.organization_id, {"column_name": body.column_name, "schema_hash": version.schema_hash})
    db.commit()
    return {"column_name": body.column_name, "version_id": version.id, "description": documentation.description,
            "version": documentation.version, "terms": [dto(term) for term in terms]}


@router.post("/catalog/datasets/{dataset_id}/terms", status_code=201)
def associate_term(dataset_id: str, body: AssociationBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dataset = owned(db, Dataset, dataset_id, user)
    authorize_dataset(db, user, dataset.id, "METADATA")
    term = owned(db, GlossaryTerm, body.term_id, user)
    if not term.active:
        raise OperationError(422, "TERM_INACTIVE", "El término está desactivado.")
    if body.version_id:
        version = owned(db, DatasetVersion, body.version_id, user)
        if version.dataset_id != dataset.id or body.column_name and body.column_name not in {item["name"] for item in version.schema_json}:
            raise OperationError(422, "COLUMN_NOT_IN_SCHEMA", "La asociación no corresponde al esquema seleccionado.")
    elif body.column_name:
        raise OperationError(422, "VERSION_REQUIRED", "Asocia columnas a una versión explícita.")
    existing = db.scalar(select(GlossaryAssociation).where(GlossaryAssociation.term_id == term.id,
        GlossaryAssociation.dataset_id == dataset.id, GlossaryAssociation.dataset_version_id == body.version_id,
        GlossaryAssociation.column_name == body.column_name))
    if not existing:
        db.add(GlossaryAssociation(organization_id=user.organization_id, term_id=term.id, dataset_id=dataset.id,
            dataset_version_id=body.version_id, column_name=body.column_name))
        audit(db, "GLOSSARY_ASSOCIATED", "dataset", dataset.id, "Término asociado", user.name, user.organization_id)
        db.commit()
    return {"term": dto(term), "dataset_id": dataset.id, "version_id": body.version_id, "column_name": body.column_name}


@router.delete("/catalog/glossary-associations/{association_id}")
def remove_association(association_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    association = owned(db, GlossaryAssociation, association_id, user)
    authorize_dataset(db, user, association.dataset_id, "METADATA")
    audit(db, "GLOSSARY_DISSOCIATED", "dataset", association.dataset_id, "Asociación de término retirada", user.name,
        user.organization_id, {"association_id": association.id, "term_id": association.term_id,
            "version_id": association.dataset_version_id, "column_name": association.column_name})
    db.delete(association)
    db.commit()
    return {"removed": True, "association_id": association_id}


def block_dto(block: DatasetBlock) -> dict:
    return {"id": block.id, "dataset_id": block.dataset_id, "scope": block.scope, "reason": block.reason,
            "active": block.active, "version": block.version, "created_at": iso(block.created_at),
            "released_at": iso(block.released_at), "release_reason": block.release_reason}


@router.get("/catalog/datasets/{dataset_id}/blocks")
def dataset_blocks(dataset_id: str, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dataset = owned(db, Dataset, dataset_id, user)
    authorize_dataset(db, user, dataset.id, "METADATA")
    blocks = db.scalars(select(DatasetBlock).where(DatasetBlock.dataset_id == dataset.id,
        DatasetBlock.organization_id == user.organization_id).order_by(DatasetBlock.created_at.desc())).all()
    return {"items": [block_dto(block) for block in blocks], "total": len(blocks)}


@router.post("/catalog/datasets/{dataset_id}/blocks", status_code=201)
def create_block(dataset_id: str, body: BlockBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dataset = owned(db, Dataset, dataset_id, user)
    block = DatasetBlock(organization_id=user.organization_id, dataset_id=dataset.id, created_by_id=user.id, **body.model_dump())
    db.add(block)
    db.flush()
    audit(db, "DATASET_BLOCKED", "dataset", dataset.id, "Restricción de uso creada", user.name, user.organization_id,
          {"scope": block.scope, "reason": block.reason, "block_id": block.id})
    db.commit()
    return block_dto(block)


@router.patch("/catalog/blocks/{block_id}")
def release_block(block_id: str, body: ReleaseBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    block = owned(db, DatasetBlock, block_id, user)
    result = db.execute(update(DatasetBlock).where(DatasetBlock.id == block.id, DatasetBlock.version == body.expected_version,
        DatasetBlock.active.is_(True)).values(active=False, version=body.expected_version + 1, released_by_id=user.id,
        released_at=utcnow(), release_reason=body.reason).execution_options(synchronize_session=False))
    if cast(CursorResult, result).rowcount != 1:
        raise OperationError(409, "VERSION_CONFLICT", "La restricción ya cambió. Actualiza su estado.")
    db.refresh(block)
    audit(db, "DATASET_BLOCK_RELEASED", "dataset", block.dataset_id, "Restricción liberada expresamente", user.name,
          user.organization_id, {"block_id": block.id, "reason": body.reason})
    db.commit()
    return block_dto(block)


@router.patch("/catalog/security-dependencies/{dependency_id}")
def release_dependency(dependency_id: str, body: ReleaseBody, db: Session = Depends(get_db), user: User = Depends(current_user)):
    dependency = owned(db, DatasetSecurityDependency, dependency_id, user)
    result = db.execute(update(DatasetSecurityDependency).where(DatasetSecurityDependency.id == dependency.id,
        DatasetSecurityDependency.version == body.expected_version, DatasetSecurityDependency.active.is_(True)).values(
        active=False, version=body.expected_version + 1, released_by_id=user.id, release_reason=body.reason)
        .execution_options(synchronize_session=False))
    if cast(CursorResult, result).rowcount != 1:
        raise OperationError(409, "VERSION_CONFLICT", "La dependencia ya cambió.")
    audit(db, "SECURITY_DEPENDENCY_RELEASED", "dataset", dependency.dataset_id, "Dependencia liberada expresamente", user.name,
          user.organization_id, {"dependency_id": dependency.id, "reason": body.reason})
    db.commit()
    return {"id": dependency.id, "active": False, "version": body.expected_version + 1}


def dataset_query(user: User, search: str = "", status: str | None = None,
                  domain_id: str | None = None, macro_domain_id: str | None = None,
                  responsible: str | None = None, pending: bool = False, *, namespace: str = ""):
    parent = aliased(DatasetVersion)
    totals = select(DatasetVersion.dataset_id.label("dataset_id"), func.count().label("total")).group_by(DatasetVersion.dataset_id).subquery()
    legacy = select(DatasetVersion.dataset_id.label("dataset_id"), func.min(parent.dataset_id).label("parent_id"))\
        .join(parent, DatasetVersion.parent_version_id == parent.id).join(Run, DatasetVersion.source_run_id == Run.id)\
        .join(Configuration, Run.config_id == Configuration.id).join(totals, totals.c.dataset_id == DatasetVersion.dataset_id)\
        .where(DatasetVersion.source_type == "INTAKE_OUTPUT", DatasetVersion.organization_id == user.organization_id,
            parent.organization_id == user.organization_id, Run.organization_id == user.organization_id,
            Run.module == "intake", Run.output_version_id == DatasetVersion.id, Run.dataset_version_id == parent.id,
            Configuration.organization_id == user.organization_id, Configuration.dataset_id == parent.dataset_id)\
        .group_by(DatasetVersion.dataset_id, totals.c.total).having(func.count() == totals.c.total,
            func.count(func.distinct(parent.dataset_id)) == 1).subquery()
    # PostgreSQL MIN/COALESCE removes VARCHAR typmods. Recursive columns must
    # retain the same width as the identity columns in their anchor term.
    parents = select(Dataset.id.label("dataset_id"), sql_cast(func.coalesce(Dataset.intake_input_dataset_id, legacy.c.parent_id), String(64)).label("parent_id"))\
        .outerjoin(legacy, legacy.c.dataset_id == Dataset.id).where(Dataset.organization_id == user.organization_id).cte(namespace + "governance_parents")
    ancestry = select(Dataset.id.label("dataset_id"), Dataset.id.label("current_id"), literal(0).label("depth"))\
        .where(Dataset.organization_id == user.organization_id).cte(namespace + "governance_ancestry", recursive=True)
    ancestry = ancestry.union_all(select(ancestry.c.dataset_id, parents.c.parent_id, ancestry.c.depth + 1)
        .join(parents, parents.c.dataset_id == ancestry.c.current_id).where(parents.c.parent_id.is_not(None), ancestry.c.depth < 128))
    governing = aliased(Dataset)
    statement = select(Dataset).join(ancestry, ancestry.c.dataset_id == Dataset.id)\
        .join(governing, governing.id == ancestry.c.current_id).join(parents, parents.c.dataset_id == governing.id)\
        .outerjoin(MacroDomain, MacroDomain.id == governing.macro_domain_id).outerjoin(DataDomain, DataDomain.id == governing.domain_id)\
        .where(Dataset.organization_id == user.organization_id, governing.organization_id == user.organization_id,
            parents.c.parent_id.is_(None))
    # The security closure uses UNION (distinct), making cycles and shared paths
    # finite; no recursive Python scan or metadata list is used for pagination.
    edges = select(DatasetSecurityDependency.dataset_id.label("child_id"), DatasetSecurityDependency.source_dataset_id.label("parent_id"))\
        .where(DatasetSecurityDependency.active.is_(True)).union(select(parents.c.dataset_id, parents.c.parent_id)
            .where(parents.c.parent_id.is_not(None))).cte(namespace + "security_edges")
    security = select(Dataset.id.label("root_id"), Dataset.id.label("ancestor_id"))\
        .where(Dataset.organization_id == user.organization_id).cte(namespace + "security_ancestry", recursive=True)
    security = security.union(select(security.c.root_id, edges.c.parent_id).join(edges, edges.c.child_id == security.c.ancestor_id))
    source = aliased(Dataset)
    invalid = select(security.c.root_id).outerjoin(source, source.id == security.c.ancestor_id).where(
        or_(source.id.is_(None), source.organization_id != user.organization_id)).subquery()
    excessive = select(security.c.root_id).group_by(security.c.root_id).having(func.count() > 128).subquery()
    cyclic = select(security.c.root_id).join(edges, edges.c.child_id == security.c.ancestor_id).where(
        edges.c.parent_id == security.c.root_id).subquery()
    statement = statement.where(Dataset.id.not_in(select(invalid.c.root_id)),
        Dataset.id.not_in(select(excessive.c.root_id)), Dataset.id.not_in(select(security.c.root_id).where(
            security.c.ancestor_id.in_(select(cyclic.c.root_id)))))
    if search:
        statement = statement.where(Dataset.name.ilike("%" + search.replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"))
    if status:
        statement = statement.where(Dataset.status == status)
    if domain_id:
        statement = statement.where(governing.domain_id == domain_id)
    if macro_domain_id:
        statement = statement.where(governing.macro_domain_id == macro_domain_id)
    if responsible:
        statement = statement.where(or_(governing.business_owner_id == responsible, governing.steward_id == responsible,
            governing.technical_custodian_id == responsible))
    if pending:
        statement = statement.where(or_(MacroDomain.id.is_(None), DataDomain.id.is_(None), MacroDomain.active.is_(False),
            DataDomain.active.is_(False), DataDomain.macro_domain_id != MacroDomain.id,
            MacroDomain.organization_id != user.organization_id, DataDomain.organization_id != user.organization_id))
    return statement.order_by(Dataset.name, Dataset.id)


def authorized_datasets(db: Session, user: User, search: str = "", status: str | None = None,
                        domain_id: str | None = None, macro_domain_id: str | None = None,
                        responsible: str | None = None, pending: bool = False) -> list[tuple[Dataset, dict]]:
    statement = dataset_query(user, search, status, domain_id, macro_domain_id, responsible, pending)
    result = []
    for dataset in db.scalars(statement):
        try:
            authorize_dataset(db, user, dataset.id, "METADATA")
            governance = governance_snapshot(db, dataset)
        except OperationError:
            continue
        if domain_id and governance["domain_id"] != domain_id or macro_domain_id and governance["macro_domain_id"] != macro_domain_id:
            continue
        if pending and governance["classification_complete"]:
            continue
        if responsible and responsible not in {governance["business_owner_id"], governance["steward_id"], governance["technical_custodian_id"]}:
            continue
        result.append((dataset, governance))
    return result


@router.get("/catalog/tree")
def tree(parent_type: str | None = None, parent_id: str | None = None, offset: int = Query(0, ge=0),
         limit: int = Query(100, ge=1, le=200), db: Session = Depends(get_db), user: User = Depends(current_user)):
    statement: Any
    if not parent_type:
        statement = select(MacroDomain).where(MacroDomain.organization_id == user.organization_id).order_by(MacroDomain.name, MacroDomain.id)
        total = (db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0) + 1
        nodes = []
        if offset == 0:
            pending = db.scalar(select(func.count()).select_from(dataset_query(user, pending=True).order_by(None).subquery())) or 0
            nodes.append({"id": "pending", "label": "Pendientes de clasificación", "type": "pending", "count": pending, "has_children": True})
        for macro in db.scalars(statement.offset(max(0, offset - 1)).limit(limit - len(nodes))):
            count = db.scalar(select(func.count()).select_from(dataset_query(user, macro_domain_id=macro.id).order_by(None).subquery())) or 0
            nodes.append({"id": macro.id, "label": macro.name, "type": "macro_domain", "count": count, "has_children": True})
        return {"items": nodes, "total": total, "offset": offset, "limit": limit}
    elif parent_type in {"macro_domain", "macrodomain"}:
        macro = owned(db, MacroDomain, parent_id or "", user)
        statement = select(DataDomain).where(DataDomain.organization_id == user.organization_id,
            DataDomain.macro_domain_id == macro.id).order_by(DataDomain.name, DataDomain.id)
        total = db.scalar(select(func.count()).select_from(statement.order_by(None).subquery())) or 0
        nodes = []
        for domain in db.scalars(statement.offset(offset).limit(limit)):
            count = db.scalar(select(func.count()).select_from(dataset_query(user, domain_id=domain.id).order_by(None).subquery())) or 0
            nodes.append({"id": domain.id, "label": domain.name, "type": "domain", "count": count, "has_children": True})
        return {"items": nodes, "total": total, "offset": offset, "limit": limit}
    elif parent_type in {"domain", "pending"}:
        if parent_type == "domain":
            owned(db, DataDomain, parent_id or "", user)
        nodes = []
        grants = set(effective_permissions(db, user))
        for kind, label, permission in (("DATASET", "Datasets", "datasets:read"), ("INTAKE", "Data Intake", "intake:read"),
            ("RECON", "ReconOps", "recon:read"), ("SENTINEL", "Sentinel", "sentinel:read"),
            ("DELIVERY", "Data Delivery", "delivery:read"), ("REPORT", "Reportes", "reports:read")):
            if permission in grants:
                count = resources(kind, domain_id=parent_id if parent_type == "domain" else None,
                    pending=parent_type == "pending", offset=0, limit=1, db=db, user=user)["total"]
                nodes.append({"id": kind, "label": label, "type": "resource_type", "count": count, "has_children": bool(count)})
    else:
        raise OperationError(422, "TREE_PARENT_INVALID", "El tipo de nodo no es válido.")
    return {"items": nodes[offset:offset + limit], "total": len(nodes), "offset": offset, "limit": limit}


@router.get("/catalog/resources")
@router.get("/catalog/datasets")
def resources(resource_type: str = "DATASET", search: str = "", status: str | None = None,
              domain_id: str | None = None, macro_domain_id: str | None = None, responsible: str | None = None,
              pending: bool = False, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
              db: Session = Depends(get_db), user: User = Depends(current_user)):
    selected = dataset_query(user, search if resource_type == "DATASET" else "", status if resource_type == "DATASET" else None,
        domain_id, macro_domain_id, responsible, pending)
    items: list[dict[str, Any]] = []
    if resource_type == "DATASET":
        total = db.scalar(select(func.count()).select_from(selected.order_by(None).subquery())) or 0
        for dataset in db.scalars(selected.offset(offset).limit(limit)):
            governance = governance_snapshot(db, dataset)
            items.append({"id": dataset.id, "name": dataset.name, "resource_type": "DATASET", "status": dataset.status,
                "dataset_id": dataset.id, "governance": governance, "href": "/catalog/datasets/" + dataset.id})
        return {"items": items, "total": total, "offset": offset, "limit": limit}
    elif resource_type in {"INTAKE", "RECON", "SENTINEL", "DELIVERY"}:
        module = {"INTAKE": "intake", "RECON": "recon", "SENTINEL": "sentinel", "DELIVERY": "DELIVERY"}[resource_type]
        if f"{module.lower()}:read" not in effective_permissions(db, user):
            raise OperationError(403, "PERMISSION_DENIED", "Tu rol no permite consultar este módulo.")
        from .governance import strict_approval
        dataset_ids = selected.with_only_columns(Dataset.id).order_by(None)
        statement = select(Run).join(DatasetVersion, Run.dataset_version_id == DatasetVersion.id).where(
            Run.organization_id == user.organization_id, Run.module == module, Run.status == "SUCCESS",
            DatasetVersion.dataset_id.in_(dataset_ids))
        if resource_type == "INTAKE":
            statement = statement.join(StrictApproval, StrictApproval.run_id == Run.id)
        if resource_type in {"RECON", "SENTINEL", "DELIVERY"}:
            statement = statement.where(Run.decision == {"RECON": "CONFORME", "SENTINEL": "HEALTHY", "DELIVERY": "COMMITTED"}[resource_type])
        if search:
            statement = statement.where(Run.name.ilike("%" + search.replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"))
        if status:
            statement = statement.where(Run.decision == status)
        total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
        for run in db.scalars(statement.order_by(Run.finished_at.desc(), Run.id).offset(offset).limit(limit)):
            version = db.get(DatasetVersion, run.dataset_version_id)
            if not version:
                continue
            if resource_type == "INTAKE" and not strict_approval(db, run)["approved"]:
                continue
            if resource_type == "RECON" and run.decision != "CONFORME" or resource_type == "SENTINEL" and run.decision != "HEALTHY":
                continue
            if resource_type == "DELIVERY" and run.decision != "COMMITTED":
                continue
            dataset = db.get(Dataset, version.dataset_id)
            if not dataset:
                continue
            governance = governance_snapshot(db, dataset)
            items.append({"id": run.id, "name": run.name, "resource_type": resource_type, "status": run.decision,
                "dataset_id": dataset.id, "governance": governance, "contract_id": run.config_id,
                "input_version_id": version.id, "output_version_id": run.output_version_id,
                "href": ("/delivery/runs/" if resource_type == "DELIVERY" else "/runs/") + run.id})
        return {"items": items, "total": total, "offset": offset, "limit": limit}
    elif resource_type == "REPORT":
        if "reports:read" not in effective_permissions(db, user):
            raise OperationError(403, "PERMISSION_DENIED", "Tu rol no permite consultar Reportes.")
        from .report_models import ReportDefinition, ReportRevision
        latest = select(ReportRevision.definition_id, ReportRevision.draft,
            func.row_number().over(partition_by=ReportRevision.definition_id, order_by=ReportRevision.version.desc()).label("rank"))\
            .where(ReportRevision.organization_id == user.organization_id).subquery()
        source_array = latest.c.draft["sources"]
        source_id: Any
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import JSONB
            converted = sql_cast(source_array, JSONB)
            source_array = case((func.jsonb_typeof(converted) == "array", converted), else_=literal([], type_=JSONB))
            entries = func.jsonb_array_elements(source_array).table_valued("value", joins_implicitly=True)
            source_id = entries.c.value.op("->>")("input_dataset_id")
        else:
            source_array = case((func.json_type(source_array) == "array", source_array), else_=literal("[]"))
            entries = func.json_each(source_array).table_valued("value", joins_implicitly=True)
            source_id = func.json_extract(entries.c.value, "$.input_dataset_id")
        selected_ids = selected.with_only_columns(Dataset.id).order_by(None)
        allowed_ids = dataset_query(user, namespace="report_allowed_").with_only_columns(Dataset.id).order_by(None)
        matching = select(literal(1)).select_from(entries).where(source_id.in_(selected_ids)).correlate(latest).exists()
        inaccessible = select(literal(1)).select_from(entries).where(or_(source_id.is_(None), source_id.not_in(allowed_ids))).correlate(latest).exists()
        statement = select(ReportDefinition, latest.c.draft).join(latest, latest.c.definition_id == ReportDefinition.id)\
            .where(ReportDefinition.organization_id == user.organization_id, latest.c.rank == 1, matching, ~inaccessible)
        if search:
            statement = statement.where(ReportDefinition.name.ilike("%" + search.replace("%", "\\%").replace("_", "\\_") + "%", escape="\\"))
        if status:
            statement = statement.where(case((ReportDefinition.active.is_(True), "ACTIVE"), else_="INACTIVE") == status)
        total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
        for definition, specification in db.execute(statement.order_by(ReportDefinition.name, ReportDefinition.id).offset(offset).limit(limit)):
            dataset_ids = {item["input_dataset_id"] for item in specification["sources"]}
            items.append({"id": definition.id, "name": definition.name, "resource_type": "REPORT", "status": "ACTIVE" if definition.active else "INACTIVE",
                "dataset_id": None, "source_dataset_ids": sorted(dataset_ids), "governance": None,
                "href": "/reports/definitions/" + definition.id})
        return {"items": items, "total": total, "offset": offset, "limit": limit}
    else:
        raise OperationError(422, "RESOURCE_TYPE_INVALID", "El tipo de recurso no es válido.")
    if status:
        items = [item for item in items if item["status"] == status]
    return {"items": items[offset:offset + limit], "total": len(items), "offset": offset, "limit": limit}


@router.get("/catalog/datasets/{dataset_id}")
def dataset_panel(dataset_id: str, section: Literal["summary", "columns", "versions", "quality", "lineage", "history"] = "summary",
                  version_id: str | None = None, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200),
                  db: Session = Depends(get_db), user: User = Depends(current_user)):
    dataset = owned(db, Dataset, dataset_id, user)
    authorize_dataset(db, user, dataset.id, "METADATA")
    governance = governance_snapshot(db, dataset)
    latest = db.scalar(select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id).order_by(DatasetVersion.version.desc()).limit(1))
    result = {"dataset": dataset_dto(db, dataset), "governance": governance, "items": [], "total": 0,
              "offset": offset, "limit": limit, "section": section}
    if section == "summary":
        result["eligibility"] = dataset_eligibility(db, user, latest) if latest else None
        result["blocks"] = dataset_blocks(dataset.id, db, user)["items"]
    elif section == "versions":
        def metadata_version(version):
            payload = version_dto(version, db)
            payload.pop("profile", None)
            return payload
        result.update(page(db, select(DatasetVersion).where(DatasetVersion.dataset_id == dataset.id)
            .order_by(DatasetVersion.version.desc()), offset, limit, metadata_version))
    elif section == "columns":
        version = owned(db, DatasetVersion, version_id, user) if version_id else latest
        if version and version.dataset_id != dataset.id:
            raise OperationError(422, "VERSION_INVALID", "La versión no pertenece al dataset.")
        columns = []
        if version:
            documentation = {item.column_name: item for item in db.scalars(select(ColumnDocumentation)
                .where(ColumnDocumentation.dataset_version_id == version.id, ColumnDocumentation.schema_hash == version.schema_hash))}
            for column in version.schema_json:
                record = documentation.get(column["name"])
                terms = db.scalars(select(GlossaryTerm).join(GlossaryAssociation).where(GlossaryAssociation.dataset_id == dataset.id,
                    GlossaryAssociation.dataset_version_id == version.id, GlossaryAssociation.column_name == column["name"]))
                columns.append({**column, "version_id": version.id, "schema_hash": version.schema_hash,
                    "description": record.description if record else "", "documentation_version": record.version if record else 0,
                    "terms": [dto(term) for term in terms]})
        result.update({"items": columns[offset:offset + limit], "total": len(columns), "version_id": version.id if version else None})
        associations = db.execute(select(GlossaryAssociation, GlossaryTerm).join(GlossaryTerm, GlossaryAssociation.term_id == GlossaryTerm.id)
            .where(GlossaryAssociation.dataset_id == dataset.id, GlossaryAssociation.dataset_version_id.is_(None),
                GlossaryAssociation.column_name.is_(None), GlossaryAssociation.organization_id == user.organization_id,
                GlossaryTerm.organization_id == user.organization_id).order_by(GlossaryTerm.name).limit(200))
        result["dataset_terms"] = [{"association_id": association.id, **dto(term)} for association, term in associations]
    elif section == "quality":
        if "intake:read" not in effective_permissions(db, user):
            raise OperationError(403, "PERMISSION_DENIED", "Tu rol no permite consultar contratos y calidad.")
        from .governance import strict_approval
        versions = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id)
        runs = select(Run).where(Run.organization_id == user.organization_id, Run.module == "intake",
            or_(Run.dataset_version_id.in_(versions), Run.output_version_id.in_(versions))).order_by(Run.created_at.desc())
        result.update(page(db, runs, offset, limit, lambda run: {**run_dto(db, run), "strict_approval": strict_approval(db, run)}))
    elif section == "history":
        result.update(page(db, select(GovernanceHistory).where(GovernanceHistory.dataset_id == governance["source_dataset_id"])
            .order_by(GovernanceHistory.version.desc()), offset, limit, lambda record: {"id": record.id, "version": record.version,
                "snapshot": record.snapshot, "created_at": iso(record.created_at), "actor_id": record.actor_id, "reason": record.reason}))
    elif section == "lineage":
        from .acquisition_models import AcquisitionRun
        from .models import (
            Artifact,
            DeliveryDestinationVersion,
            DeliveryTargetPolicy,
            ExternalConnectionVersion,
        )
        from .report_models import ReportDefinition, ReportExecution, ReportRevision

        identities = select(DatasetVersion.id).where(DatasetVersion.dataset_id == dataset.id)
        attached = select(ArtifactLink.source_id).where(ArtifactLink.organization_id == user.organization_id,
            ArtifactLink.source_type.in_(["RUN", "REPORT_EXECUTION"]), ArtifactLink.target_type == "DATASET_VERSION",
            ArtifactLink.target_id.in_(identities))
        grants = set(effective_permissions(db, user))
        modules = [module for module in ("intake", "recon", "sentinel", "DELIVERY", "delivery") if f"{module.lower()}:read" in grants]
        allowed_datasets = dataset_query(user).with_only_columns(Dataset.id).order_by(None)

        def visible_node(kind, identity):
            predicates = []
            models: list[tuple[str, Any, str | None]] = [("DATASET_VERSION", DatasetVersion, None), ("ARTIFACT", Artifact, None),
                ("RUN", Run, None), ("CONFIGURATION", Configuration, None), ("ACQUISITION", AcquisitionRun, None),
                ("CONNECTION_VERSION", ExternalConnectionVersion, "connections:read"),
                ("DELIVERY_DESTINATION_VERSION", DeliveryDestinationVersion, "delivery:read"),
                ("DELIVERY_TARGET_POLICY", DeliveryTargetPolicy, "delivery:read"),
                ("REPORT_DEFINITION", ReportDefinition, "reports:read"),
                ("REPORT_REVISION", ReportRevision, "reports:read"),
                ("REPORT_EXECUTION", ReportExecution, "reports:read")]
            for label, model, permission in models:
                if permission and permission not in grants:
                    continue
                predicate = select(model.id).where(model.id == identity, model.organization_id == user.organization_id)
                if model in (Run, Configuration):
                    predicate = predicate.where(model.module.in_(modules))
                elif model is DatasetVersion:
                    predicate = predicate.where(model.dataset_id.in_(allowed_datasets))
                elif model is ReportExecution:
                    predicate = predicate.where(model.user_id == user.id)
                predicates.append(and_(kind == label, predicate.exists()))
            return or_(*predicates)

        statement = select(ArtifactLink).where(ArtifactLink.organization_id == user.organization_id,
            or_(and_(ArtifactLink.source_type == "DATASET_VERSION", ArtifactLink.source_id.in_(identities)),
                and_(ArtifactLink.target_type == "DATASET_VERSION", ArtifactLink.target_id.in_(identities)),
                and_(ArtifactLink.source_type.in_(["RUN", "REPORT_EXECUTION"]), ArtifactLink.source_id.in_(attached))),
            visible_node(ArtifactLink.source_type, ArtifactLink.source_id), visible_node(ArtifactLink.target_type, ArtifactLink.target_id))
        total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
        rows = db.scalars(statement.order_by(ArtifactLink.created_at, ArtifactLink.id).offset(offset).limit(limit))
        links = []
        def reference(kind: str, identity: str) -> tuple[str | None, str | None]:
            if kind == "DATASET_VERSION":
                item = db.get(DatasetVersion, identity)
                asset = db.get(Dataset, item.dataset_id) if item else None
                if asset and item:
                    return asset.name + " · v" + str(item.version), "/datasets/" + asset.id + "?version=" + item.id
            elif kind == "RUN":
                operation = db.get(Run, identity)
                if operation:
                    return operation.name, ("/delivery/runs/" if operation.module == "DELIVERY" else "/runs/") + identity
            elif kind == "CONFIGURATION":
                contract = db.get(Configuration, identity)
                if contract and f"{contract.module.lower()}:read" in effective_permissions(db, user):
                    return contract.name + " · r" + str(contract.version), "/intake?contract_id=" + identity
            elif kind in {"REPORT_DEFINITION", "REPORT_REVISION", "REPORT_EXECUTION"} and "reports:read" in effective_permissions(db, user):
                from .report_models import ReportDefinition, ReportExecution, ReportRevision
                if kind == "REPORT_EXECUTION":
                    execution = db.get(ReportExecution, identity)
                    if execution and execution.organization_id == user.organization_id and execution.user_id == user.id:
                        return "Generación de Reportes", "/reports/executions/" + identity
                else:
                    definition_id = identity
                    if kind == "REPORT_REVISION":
                        revision = db.get(ReportRevision, identity)
                        definition_id = revision.definition_id if revision else ""
                    definition = db.get(ReportDefinition, definition_id)
                    if definition and definition.organization_id == user.organization_id:
                        if kind == "REPORT_REVISION" and revision:
                            return (definition.name + " · r" + str(revision.version),
                                "/reports/definitions/" + definition.id + "?revision_id=" + revision.id)
                        return definition.name, "/reports/definitions/" + definition.id
            return None, None
        for link in rows:
            source_name, source_href = reference(link.source_type, link.source_id)
            target_name, target_href = reference(link.target_type, link.target_id)
            links.append({"id": link.id, "relation": link.relation, "source_type": link.source_type, "source_id": link.source_id,
                "target_type": link.target_type, "target_id": link.target_id, "source_name": source_name,
                "source_href": source_href, "target_name": target_name, "target_href": target_href})
        dependencies = db.scalars(select(DatasetSecurityDependency).where(DatasetSecurityDependency.dataset_id == dataset.id))
        result.update({"items": links, "total": total, "security_dependencies": [
            {"id": item.id, "source_dataset_id": item.source_dataset_id, "active": item.active, "version": item.version} for item in dependencies]})
    return result
