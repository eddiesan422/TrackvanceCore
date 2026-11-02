"""Opt-in semantic contracts, compiled into native versioned document projections.

Profiles describe meaning and scalar formats. They carry no provider layouts,
expected answers, training examples or authority to approve model observations.
"""
from __future__ import annotations

import copy
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .document_normalization import DocumentNormalization, canonical_hash

MODE: Literal["SEMANTIC_GENERAL_V1"] = "SEMANTIC_GENERAL_V1"
Route = Literal["QWEN_VISUAL", "NATIVE_FIRST", "PADDLE_STRUCTURED"]


class SemanticField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
    label: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=1000)
    logical_type: Literal["STRING", "DECIMAL", "INT64", "DATE", "BOOLEAN"] = "STRING"
    required: bool = True
    critical: bool = False
    business_key: bool = False
    date_format: str = Field(default="%Y-%m-%d", max_length=40)
    decimal_separator: Literal[".", ","] = "."
    thousands_separator: Literal[".", ",", " "] | None = None
    currency_symbols: list[str] = Field(default_factory=list, max_length=10)
    section: Literal["DOCUMENT", "ISSUER", "BUYER", "TOTALS"] = "DOCUMENT"
    normalization: DocumentNormalization | None = None


class SemanticCollection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
    description: str = Field(min_length=1, max_length=1000)
    kind: Literal["TABLE", "COLLECTION"] = "TABLE"
    required: bool = True
    fields: list[SemanticField] = Field(min_length=1, max_length=40)


class SemanticProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["SEMANTIC_GENERAL_V1"] = MODE
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")
    name: str = Field(min_length=1, max_length=140)
    revision: int = Field(ge=1, le=100000)
    document_type: Literal["INVOICE", "STATEMENT", "NARRATIVE", "CERTIFICATE", "CUSTOM"]
    description: str = Field(min_length=1, max_length=2000)
    fields: list[SemanticField] = Field(min_length=1, max_length=100)
    tables: list[SemanticCollection] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def names(self):
        reserved = {"document_id", "extraction_id", "batch_id", "row_number", "__tv_record_number"}
        for items in [self.fields, *(table.fields for table in self.tables)]:
            names = [item.name.casefold() for item in items]
            if len(set(names)) != len(names) or set(names) & reserved:
                raise ValueError("Semantic field names must be unique and nonreserved.")
        names = [table.name.casefold() for table in self.tables]
        if len(set(names)) != len(names) or "headers" in names:
            raise ValueError("Semantic collection names must be unique and nonreserved.")
        return self


class ActivateSemanticProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_id: str | None = Field(default=None, max_length=63)
    profile: SemanticProfile | None = None
    route: Route = "QWEN_VISUAL"
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def one_profile(self):
        if (self.profile_id is None) == (self.profile is None) or not self.reason.strip():
            raise ValueError("Select exactly one versioned profile and give a reason.")
        return self


def enabled():
    return os.getenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "false").lower() == "true"


def require_enabled():
    from .operations_common import OperationError

    if not enabled():
        raise OperationError(409, "DOCUMENT_SEMANTIC_GENERAL_DISABLED",
            "La extracción semántica general permanece deshabilitada; habilita el candidato explícitamente en el entorno de pruebas.")


def _field(name, label, logical_type="STRING", *, required=True, **kwargs):
    return {"name": name, "label": label, "logical_type": logical_type, "required": required, **kwargs}


def profiles():
    """Generic starter contracts; callers may submit their own declared schema."""
    values = [
        {"id": "invoice-v1", "name": "Factura general", "revision": 1, "document_type": "INVOICE",
         "description": "Cabecera de factura y líneas impresas. No calcular importes ausentes.",
         "fields": [_field("invoice_number", "Número de factura", critical=True, business_key=True),
             _field("issuer", "Emisor", critical=True, section="ISSUER"),
             _field("buyer", "Comprador", required=False, section="BUYER"),
             _field("issue_date", "Fecha de emisión", "DATE", required=False),
             _field("currency", "Moneda", required=False),
             _field("subtotal", "Subtotal", "DECIMAL", required=False, critical=True, section="TOTALS"),
             _field("tax", "Impuestos", "DECIMAL", required=False, section="TOTALS"),
             _field("total", "Total a pagar", "DECIMAL", critical=True, section="TOTALS")],
         "tables": [{"name": "items", "description": "Una fila por cada concepto de factura impreso, conservando repeticiones y continuidad entre páginas.",
             "fields": [_field("description", "Descripción"), _field("quantity", "Cantidad", "DECIMAL", required=False),
                 _field("unit_price", "Precio unitario", "DECIMAL", required=False),
                 _field("amount", "Importe de línea", "DECIMAL", required=False, critical=True)]}]},
        {"id": "statement-v1", "name": "Extracto general", "revision": 1, "document_type": "STATEMENT",
         "description": "Identificación de extracto y movimientos impresos, preservando signos y saldos.",
         "fields": [_field("account", "Cuenta", critical=True, business_key=True),
             _field("holder", "Titular", required=False), _field("period", "Período", required=False),
             _field("opening_balance", "Saldo inicial", "DECIMAL", required=False, critical=True),
             _field("closing_balance", "Saldo final", "DECIMAL", required=False, critical=True)],
         "tables": [{"name": "movements", "description": "Cada movimiento impreso, sin deduplicar operaciones de igual importe.",
             "fields": [_field("date", "Fecha", "DATE", required=False), _field("description", "Concepto"),
                 _field("amount", "Importe con signo", "DECIMAL", critical=True),
                 _field("balance", "Saldo", "DECIMAL", required=False)]}]},
        {"id": "narrative-v1", "name": "Narrativo: acciones solicitadas", "revision": 1, "document_type": "NARRATIVE",
         "description": "Conservar contexto narrativo; proyectar sólo acciones afirmativas explícitamente comprometidas, sin convertir negaciones o hipótesis en compromisos.",
         "fields": [_field("title", "Título o asunto", required=False), _field("summary", "Resumen literal breve", required=False)],
         "tables": [{"name": "actions", "kind": "COLLECTION", "required": False,
             "description": "Sólo compromisos afirmativos explícitos. No incluir acciones negadas, rechazadas, propuestas hipotéticas o meras menciones.",
             "fields": [_field("action", "Acción comprometida"), _field("responsible", "Responsable", required=False),
                 _field("due_date", "Fecha comprometida", "DATE", required=False),
                 _field("source_quote", "Cita literal completa que expresa el compromiso", critical=True)]}]},
        {"id": "certificate-v1", "name": "Certificado o formulario general", "revision": 1, "document_type": "CERTIFICATE",
         "description": "Identificación de certificado y afirmación literal; conservar negaciones y limitaciones.",
         "fields": [_field("certificate_id", "Identificador del certificado", required=False, business_key=True),
             _field("issuer", "Emisor", critical=True, section="ISSUER"), _field("subject", "Persona o entidad certificada", critical=True),
             _field("statement", "Afirmación literal certificada, incluidas negaciones y limitaciones", critical=True),
             _field("issue_date", "Fecha de emisión", "DATE", required=False)]},
    ]
    return [SemanticProfile.model_validate(value).model_dump(mode="json") for value in values]


def catalog():
    return {"enabled": enabled(), "mode": MODE, "default_enabled": False,
        "profiles": [{**value, "sha256": canonical_hash(value)} for value in profiles()]}


def resolve_profile(body):
    from .operations_common import OperationError

    if body.profile is not None:
        return body.profile.model_dump(mode="json")
    selected = next((value for value in profiles() if value["id"] == body.profile_id), None)
    if selected is None:
        raise OperationError(422, "DOCUMENT_SEMANTIC_PROFILE_UNKNOWN", "Selecciona un perfil semántico disponible.")
    return selected


def compile_profile(profile, route="QWEN_VISUAL"):
    from .document_schemas import DocumentDefinition

    profile = SemanticProfile.model_validate(profile).model_dump(mode="json")
    def field(value):
        return {key: copy.deepcopy(item) for key, item in value.items() if key != "description"}
    definition = {"fields": [field(item) for item in profile["fields"]],
        "tables": [{"name": table["name"], "fields": [field(item) for item in table["fields"]],
            "required": table["required"], "semantic_collection": True} for table in profile["tables"]],
        "publication": {"review_policy": "ALL"}, "semantic_general": {
            "mode": MODE, "profile_id": profile["id"], "profile_name": profile["name"],
            "profile_revision": profile["revision"], "profile_sha256": canonical_hash(profile),
            "route": route, "contract": profile}}
    return DocumentDefinition.model_validate(definition).model_dump(mode="json")


def validate_metadata(definition):
    """Prevent a visual template, forged profile hash or AUTO policy hiding in opt-in metadata."""
    metadata = definition.semantic_general
    if metadata is None:
        if any(table.semantic_collection for table in definition.tables):
            raise ValueError("Semantic collections require the semantic general contract.")
        return
    keys = {"mode", "profile_id", "profile_name", "profile_revision", "profile_sha256", "route", "contract"}
    if not isinstance(metadata, dict) or set(metadata) != keys or metadata["mode"] != MODE:
        raise ValueError("Invalid semantic general metadata.")
    profile = SemanticProfile.model_validate(metadata["contract"]).model_dump(mode="json")
    if (metadata["profile_sha256"] != canonical_hash(profile) or metadata["profile_id"] != profile["id"]
            or metadata["profile_revision"] != profile["revision"] or metadata["profile_name"] != profile["name"]
            or not isinstance(metadata["route"], str) or metadata["route"] not in {"QWEN_VISUAL", "NATIVE_FIRST", "PADDLE_STRUCTURED"}
            or definition.publication.review_policy != "ALL"):
        raise ValueError("Semantic profile integrity or mandatory review policy failed.")
    from .document_schemas import DocumentField

    def field_projection(value):
        return DocumentField.model_validate({key: item for key, item in value.items()
            if key != "description"}).model_dump(mode="json")
    actual = [field.model_dump(mode="json") for field in definition.fields]
    expected = [field_projection(field) for field in profile["fields"]]
    tables = [(table.name, table.required, [field.model_dump(mode="json") for field in table.fields]) for table in definition.tables]
    expected_tables = [(table["name"], table["required"], [field_projection(field) for field in table["fields"]]) for table in profile["tables"]]
    if actual != expected or tables != expected_tables:
        raise ValueError("Semantic projection does not match the declared contract.")
    for field in [*definition.fields, *(field for table in definition.tables for field in table.fields)]:
        if field.visual or field.visual_alternatives or field.source_association or field.pattern != r"(.+)":
            raise ValueError("Semantic profiles cannot carry layout or provider-specific parser rules.")
    if any(table.visual or table.row_pattern or table.structured_header_labels or not table.semantic_collection for table in definition.tables):
        raise ValueError("Semantic collections cannot carry visual or regular-expression row rules.")


def configuration_for(definition, configuration):
    metadata = definition.get("semantic_general")
    if not metadata:
        return configuration
    require_enabled()
    return {**configuration, "provider": "PADDLE_QWEN_LOCAL", "extraction_mode": MODE,
        "extraction_strategy": metadata["route"], "automatic_verification": "OFF", "max_retries": 0,
        "semantic_profile_sha256": metadata["profile_sha256"]}


def assert_configuration(definition, configuration):
    from .operations_common import OperationError

    metadata = definition.get("semantic_general")
    if not metadata:
        return
    require_enabled()
    if (configuration.get("provider") != "PADDLE_QWEN_LOCAL" or configuration.get("extraction_mode") != MODE
            or configuration.get("extraction_strategy") != metadata["route"]
            or configuration.get("semantic_profile_sha256") != metadata["profile_sha256"]
            or configuration.get("automatic_verification", "OFF") != "OFF"):
        raise OperationError(409, "DOCUMENT_SEMANTIC_CONFIGURATION", "El contrato semántico y su configuración congelada no coinciden.")


def assert_replay_binding(previous_definition, definition, configuration):
    from .operations_common import OperationError

    before, after = previous_definition.get("semantic_general"), definition.get("semantic_general")
    if not before and not after:
        return
    if (not before or not after or before.get("profile_sha256") != after.get("profile_sha256")
            or before.get("route") != after.get("route")):
        raise OperationError(409, "DOCUMENT_SEMANTIC_REPLAY_CONTRACT",
            "El reconocimiento conservado pertenece a otro contrato o ruta semántica. Una nueva definición requiere un nuevo documento/intento autorizado.")
    assert_configuration(previous_definition, configuration)
    assert_configuration(definition, configuration)


def guard_profile_revision(db, organization_id, definition):
    """Reserve catalog IDs and keep a profile revision immutable across families.

    The caller already physically locked its family (SQLite serializes writers).
    PostgreSQL additionally takes a transaction-scoped organization advisory
    lock because no profile registry or organization row exists in this schema.
    No old template is rewritten or retroactively registered.
    """
    metadata = definition.get("semantic_general")
    if not metadata:
        return
    from sqlalchemy import select, text

    from .document_models import DocumentTemplate
    from .operations_common import OperationError

    builtin = next((item for item in profiles() if item["id"] == metadata["profile_id"]), None)
    if builtin is not None and metadata["profile_sha256"] != canonical_hash(builtin):
        raise OperationError(409, "DOCUMENT_SEMANTIC_PROFILE_REVISION_CONFLICT",
            "Los identificadores del catálogo están reservados; crea un identificador de perfil propio.")
    if db.bind.dialect.name == "postgresql":
        import hashlib

        key = int.from_bytes(hashlib.sha256(("document-semantic-profile:" + organization_id).encode()).digest()[:8], "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
    for item in db.scalars(select(DocumentTemplate).where(DocumentTemplate.organization_id == organization_id)):
        existing = item.definition.get("semantic_general")
        if (not existing or existing.get("profile_id") != metadata["profile_id"]
                or existing.get("profile_revision") != metadata["profile_revision"]):
            continue
        if canonical_hash(item.definition) != item.definition_hash:
            raise OperationError(409, "DOCUMENT_TEMPLATE_INTEGRITY", "Un contrato conservado perdió su integridad.")
        if existing.get("profile_sha256") != metadata["profile_sha256"]:
            raise OperationError(409, "DOCUMENT_SEMANTIC_PROFILE_REVISION_CONFLICT",
                "Ese identificador y revisión ya conservan otro contrato; incrementa la revisión o usa otro identificador.")


def native_semantic_sufficient(native, definition):
    """A native-only shortcut needs exact, unique, self-contained label:value lines.

    General layout-free table/narrative association is delegated to the selected
    semantic reader. It cannot be manufactured by the legacy regex defaults.
    """
    import re

    if definition.get("tables") or native["payload"].get("native_incomplete_pages"):
        return False
    lines = native["payload"].get("lines", [])
    if not lines:
        return False
    for field in definition["fields"]:
        cell = native["payload"].get("fields", {}).get(field["name"], {})
        observed = cell.get("observed")
        if observed is None and not field.get("required", True):
            continue
        if (not observed or cell.get("validity") != "VALID" or field.get("section", "DOCUMENT") != "DOCUMENT"):
            return False
        pattern = re.compile(r"^\s*" + re.escape(field["label"]) + r"\s*[:=]\s*" + re.escape(observed) + r"\s*$", re.IGNORECASE)
        matches = [line for line in lines if pattern.fullmatch(line["text"])]
        if len(matches) != 1 or (cell.get("origin") or {}).get("page") != matches[0]["page"]:
            return False
    return True
