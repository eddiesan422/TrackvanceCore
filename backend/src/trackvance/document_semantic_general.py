"""Opt-in semantic contracts, compiled into native versioned document projections.

Profiles describe meaning and scalar formats. They carry no provider layouts,
expected answers, training examples or authority to approve model observations.
"""
from __future__ import annotations

import copy
import os
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .document_normalization import DocumentNormalization, canonical_hash
from .document_semantic_business import SemanticInvoiceControls

MODE: Literal["SEMANTIC_GENERAL_V1"] = "SEMANTIC_GENERAL_V1"
AUTO_VERSION = "SEMANTIC_AUTO_V1"
AUTO_POLICY = "SEMANTIC_EVIDENCE_V1"
AUTO_VERSION_V2 = "SEMANTIC_AUTO_V2"
AUTO_POLICY_V2 = "SEMANTIC_EVIDENCE_V2"
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
    profile_revision: int | None = Field(default=None, ge=1, le=100000)
    profile: SemanticProfile | None = None
    route: Route = "QWEN_VISUAL"
    automatic_verification: Literal["OFF", "SEMANTIC_AUTO_V1", "SEMANTIC_AUTO_V2"] = "OFF"
    business_controls: SemanticInvoiceControls | None = None
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def one_profile(self):
        if ((self.profile_id is None) == (self.profile is None) or not self.reason.strip()
                or self.profile is not None and self.profile_revision is not None
                or self.business_controls is not None and self.automatic_verification == "OFF"):
            raise ValueError("Select exactly one versioned profile and give a reason.")
        return self


def enabled():
    return os.getenv("TRACKVANCE_SEMANTIC_GENERAL_ENABLED", "false").lower() == "true"


def automatic_enabled():
    return enabled() and os.getenv("TRACKVANCE_SEMANTIC_AUTO_ENABLED", "false").lower() == "true"


def automatic_contract(definition):
    return (definition.get("semantic_general") or {}).get("automatic_verification") in {AUTO_VERSION, AUTO_VERSION_V2}


def verification_contract_v2():
    """The assessment version is independent of unchanged acquisition inputs."""
    from .document_printed_pagination import VERSION as pagination_version
    from .document_semantic_vocabulary import V2_SHA256, V2_VERSION

    return {"version": AUTO_VERSION_V2, "association_contract": "SEMANTIC_SOURCE_ASSOCIATION_V2",
        "vocabulary_version": V2_VERSION, "vocabulary_sha256": V2_SHA256,
        "validator_version": "TRACKVANCE_SEMANTIC_EVIDENCE_098_V2",
        "table_inventory": "MEASURED_SOURCE_TABLE_OCCURRENCES_V2",
        "printed_pagination": pagination_version}


def require_automatic_enabled():
    from .operations_common import OperationError

    require_enabled()
    if not automatic_enabled():
        raise OperationError(409, "DOCUMENT_SEMANTIC_AUTO_DISABLED",
            "La verificación semántica automática requiere activación explícita en este entorno.")


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
    from .document_semantic_business import proposal

    return {"enabled": enabled(), "mode": MODE, "default_enabled": False,
        "automatic_verification_enabled": automatic_enabled(), "automatic_verification_default": "OFF",
        "automatic_verification_version": AUTO_VERSION_V2,
        "profiles": [{**value, "sha256": canonical_hash(value), "automatic_business_controls": proposal(value)}
            for value in profile_versions()]}


def profile_versions():
    """Keep original catalog revisions available; new policies require a new hash."""
    original = profiles()
    current = copy.deepcopy(original)
    for profile in current:
        profile["revision"] = 2
        fields = [*profile["fields"], *(field for table in profile["tables"] for field in table["fields"])]
        for field in fields:
            policy: dict[str, Any] = {}
            if field["logical_type"] == "DECIMAL":
                policy["decimal_formats"] = "UNAMBIGUOUS_LOCALE_V1"
                if field["name"] in {"subtotal", "tax", "total", "unit_price", "amount", "opening_balance", "closing_balance", "balance"}:
                    field["currency_symbols"] = ["US$", "$", "€", "£", "COP", "USD", "EUR", "GBP", "CHF"]
            elif field["logical_type"] == "DATE":
                policy["date_formats"] = ["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y",
                    "%d-%m-%Y", "%m-%d-%Y", "%d.%m.%Y", "%m.%d.%Y"]
            elif field["logical_type"] == "STRING" and not field["business_key"]:
                policy["text_linebreaks"] = "DECLARED_MULTILINE_V1"
            if field["name"] == "currency":
                policy = {"currency_equivalences": {
                    "COP": ["peso colombiano", "pesos colombianos", "COP - peso colombiano", "COP - pesos colombianos"],
                    "USD": ["US dollar", "US dollars", "dólar estadounidense", "dólares estadounidenses"],
                    "EUR": ["euro", "euros"], "GBP": ["pound sterling", "libra esterlina"],
                    "CHF": ["Swiss franc", "franco suizo"]}}
            if policy:
                field["normalization"] = policy
        if profile["document_type"] == "NARRATIVE":
            profile["tables"][0]["description"] += (
                " Revisar todas las páginas y secciones. Incluir cada compromiso explícito, también de organizaciones;"
                " no limitarse a personas ni a una sección titulada compromisos. Conservar cada ocurrencia y su cita;"
                " una fecha ausente permanece null, sin eliminar la acción afirmativa.")
        profile.update(SemanticProfile.model_validate(profile).model_dump(mode="json"))
    # A projection with an additional semantic column is a new contract. Keep
    # revision2 available, including the hashes used by saved CPU projections.
    invoice = copy.deepcopy(next(value for value in current if value["document_type"] == "INVOICE"))
    invoice["revision"] = 3
    items = invoice["tables"][0]
    description = next(field for field in items["fields"] if field["name"] == "description")
    description["description"] = (
        "Texto literal que describe el bien o servicio. Si el original distingue claramente un código"
        " de artículo de su descripción, transcribir el código por separado en item_code y sólo el texto"
        " descriptivo aquí. No recortar prefijos por su aspecto ni inferir una separación; si no es"
        " demostrable, conservar el fragmento completo y declarar la incertidumbre.")
    items["fields"].insert(0, _field("item_code", "Código o identificador del artículo", required=False,
        description="Sólo un identificador de artículo explícito y separable en la fila."
            " Preservar ceros iniciales, puntuación y caracteres; no usar referencias de factura, pago, contrato"
            " o proveedor. Si no existe o no se puede distinguir, dejar null y conservar la incertidumbre."
            " El mismo artículo puede aparecer en varias filas legítimas; el código no identifica de forma única la fila."))
    items["fields"].append(_field("source_quote", "Fragmento literal completo de la fila", required=False,
        description="Transcribir el fragmento impreso completo que contiene la fila o su bloque, incluido el"
            " identificador si existe. Conservar el texto y los saltos; no agregar etiquetas, separadores"
            " de tabla, cálculos ni texto reconstruido. Si no es legible, dejar null y declarar incertidumbre.",
        normalization={"text_linebreaks": "DECLARED_MULTILINE_V1"}))
    items["description"] += (
        " Separar identificador y texto descriptivo sólo si la evidencia impresa permite distinguirlos;"
        " conservar además el fragmento literal completo. No inventar ni eliminar códigos, ni deduplicar filas.")
    invoice = SemanticProfile.model_validate(invoice).model_dump(mode="json")
    return [*original, *current, invoice]


def resolve_profile(body):
    from .operations_common import OperationError

    if body.profile is not None:
        return body.profile.model_dump(mode="json")
    revision = body.profile_revision
    matches = [value for value in profile_versions() if value["id"] == body.profile_id
        and (revision is None or value["revision"] == revision)]
    selected = max(matches, key=lambda value: value["revision"]) if matches else None
    if selected is None:
        raise OperationError(422, "DOCUMENT_SEMANTIC_PROFILE_UNKNOWN", "Selecciona un perfil semántico disponible.")
    return selected


def compile_profile(profile, route="QWEN_VISUAL", *, automatic_verification="OFF", business_controls=None):
    from .document_schemas import DocumentDefinition

    if automatic_verification not in {"OFF", AUTO_VERSION, AUTO_VERSION_V2}:
        raise ValueError("Unknown semantic automatic verification contract.")
    if business_controls is not None and automatic_verification == "OFF":
        raise ValueError("Invoice automatic business controls require explicit semantic AUTO activation.")
    if automatic_verification in {AUTO_VERSION, AUTO_VERSION_V2}:
        require_automatic_enabled()
    profile = SemanticProfile.model_validate(profile).model_dump(mode="json")
    def field(value):
        return {key: copy.deepcopy(item) for key, item in value.items() if key != "description"}
    definition: dict[str, Any] = {"fields": [field(item) for item in profile["fields"]],
        "tables": [{"name": table["name"], "fields": [field(item) for item in table["fields"]],
            "required": table["required"], "semantic_collection": True} for table in profile["tables"]],
        "publication": {"review_policy": "ALL"}, "semantic_general": {
            "mode": MODE, "profile_id": profile["id"], "profile_name": profile["name"],
            "profile_revision": profile["revision"], "profile_sha256": canonical_hash(profile),
            "route": route, "contract": profile}}
    if automatic_verification in {AUTO_VERSION, AUTO_VERSION_V2}:
        definition["semantic_general"].update(automatic_verification=automatic_verification,
            association_contract="SEMANTIC_SOURCE_ASSOCIATION_V2" if automatic_verification == AUTO_VERSION_V2
                else "SEMANTIC_SOURCE_ASSOCIATION_V1")
        definition["publication"]["review_policy"] = "AUTO_VERIFIED"
        definition["validation_policy"] = {"version": AUTO_POLICY_V2 if automatic_verification == AUTO_VERSION_V2 else AUTO_POLICY}
        if automatic_verification == AUTO_VERSION_V2:
            contract = verification_contract_v2()
            definition["semantic_general"].update(verification_contract=contract,
                verification_contract_sha256=canonical_hash(contract))
        if business_controls is not None:
            from .document_semantic_business import projection

            value, arithmetic, totals = projection(business_controls)
            definition["semantic_general"].update(business_controls=value,
                business_controls_sha256=canonical_hash(value))
            definition["arithmetic"], definition["cross_totals"] = arithmetic, totals
    return DocumentDefinition.model_validate(definition).model_dump(mode="json")


def validate_metadata(definition):
    """Prevent a visual template, forged profile hash or AUTO policy hiding in opt-in metadata."""
    metadata = definition.semantic_general
    if metadata is None:
        if any(table.semantic_collection for table in definition.tables):
            raise ValueError("Semantic collections require the semantic general contract.")
        return
    keys = {"mode", "profile_id", "profile_name", "profile_revision", "profile_sha256", "route", "contract"}
    automatic = isinstance(metadata, dict) and metadata.get("automatic_verification") in {AUTO_VERSION, AUTO_VERSION_V2}
    version2 = automatic and metadata["automatic_verification"] == AUTO_VERSION_V2
    if automatic:
        keys |= {"automatic_verification", "association_contract"}
        if version2:
            keys |= {"verification_contract", "verification_contract_sha256"}
        if "business_controls" in metadata or "business_controls_sha256" in metadata:
            keys |= {"business_controls", "business_controls_sha256"}
    if not isinstance(metadata, dict) or set(metadata) != keys or metadata["mode"] != MODE:
        raise ValueError("Invalid semantic general metadata.")
    if automatic and (metadata.get("association_contract") != ("SEMANTIC_SOURCE_ASSOCIATION_V2" if version2 else "SEMANTIC_SOURCE_ASSOCIATION_V1")
            or definition.validation_policy is None or definition.validation_policy.version != (AUTO_POLICY_V2 if version2 else AUTO_POLICY)
            or definition.validation_policy.allow_noncritical_source_bound):
        raise ValueError("Automatic semantic verification requires its complete independent evidence policy.")
    if version2 and (metadata.get("verification_contract") != verification_contract_v2()
            or metadata.get("verification_contract_sha256") != canonical_hash(metadata["verification_contract"])):
        raise ValueError("The explicit verification contract does not match its version and hash.")
    from .document_semantic_business import validate_business_metadata

    validate_business_metadata(definition)
    profile = SemanticProfile.model_validate(metadata["contract"]).model_dump(mode="json")
    if (metadata["profile_sha256"] != canonical_hash(profile) or metadata["profile_id"] != profile["id"]
            or metadata["profile_revision"] != profile["revision"] or metadata["profile_name"] != profile["name"]
            or not isinstance(metadata["route"], str) or metadata["route"] not in {"QWEN_VISUAL", "NATIVE_FIRST", "PADDLE_STRUCTURED"}
            or definition.publication.review_policy != ("AUTO_VERIFIED" if automatic else "ALL")):
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
    automatic = automatic_contract(definition)
    if automatic:
        require_automatic_enabled()
    return {**configuration, "provider": "PADDLE_QWEN_LOCAL", "extraction_mode": MODE,
        "extraction_strategy": metadata["route"],
        "automatic_verification": "BOUNDED_LOCAL_V1" if automatic else "OFF", "max_retries": 0,
        "semantic_profile_sha256": metadata["profile_sha256"]}


def assert_configuration(definition, configuration):
    from .operations_common import OperationError

    metadata = definition.get("semantic_general")
    if not metadata:
        return
    require_enabled()
    automatic = automatic_contract(definition)
    if automatic:
        require_automatic_enabled()
    if (configuration.get("provider") != "PADDLE_QWEN_LOCAL" or configuration.get("extraction_mode") != MODE
            or configuration.get("extraction_strategy") != metadata["route"]
            or configuration.get("semantic_profile_sha256") != metadata["profile_sha256"]
            or configuration.get("automatic_verification", "OFF") != ("BOUNDED_LOCAL_V1" if automatic else "OFF")):
        raise OperationError(409, "DOCUMENT_SEMANTIC_CONFIGURATION", "El contrato semántico y su configuración congelada no coinciden.")


def _matches_verification_contract(metadata, contract):
    return (metadata.get("association_contract") == contract["association_contract"]
        and metadata.get("verification_contract") == contract
        and metadata.get("verification_contract_sha256") == canonical_hash(contract))


def assert_replay_binding(previous_definition, definition, configuration):
    from .operations_common import OperationError

    before, after = previous_definition.get("semantic_general"), definition.get("semantic_general")
    if not before and not after:
        return False
    verification_upgrade, valid_verification_target = False, True
    if before and after and after.get("automatic_verification") == AUTO_VERSION_V2:
        from .document_semantic_vocabulary import V2_ENTRY_SHA256

        current = verification_contract_v2()
        valid_verification_target = _matches_verification_contract(after, current)
        verification_upgrade = before.get("automatic_verification") == AUTO_VERSION
        if before.get("automatic_verification") == AUTO_VERSION_V2:
            entry = {**current, "vocabulary_sha256": V2_ENTRY_SHA256}
            verification_upgrade = (entry != current and _matches_verification_contract(before, entry)
                and before.get("contract") == after.get("contract")
                and before.get("business_controls") == after.get("business_controls")
                and all(previous_definition.get(key) == definition.get(key) for key in ("fields", "tables")))
            valid_verification_target &= verification_upgrade or _matches_verification_contract(before, current)
    if (not before or not after or not valid_verification_target
            or before.get("profile_sha256") != after.get("profile_sha256")
            or before.get("route") != after.get("route")
            or not verification_upgrade and before.get("automatic_verification", "OFF") != after.get("automatic_verification", "OFF")
            or not verification_upgrade and before.get("verification_contract_sha256") != after.get("verification_contract_sha256")
            or before.get("business_controls_sha256") != after.get("business_controls_sha256")):
        raise OperationError(409, "DOCUMENT_SEMANTIC_REPLAY_CONTRACT",
            "El reconocimiento conservado pertenece a otro contrato o ruta semántica. Una nueva definición requiere un nuevo documento/intento autorizado.")
    assert_configuration(previous_definition, configuration)
    assert_configuration(definition, configuration)
    return verification_upgrade


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

    versions = [item for item in profile_versions() if item["id"] == metadata["profile_id"]]
    builtin = next((item for item in versions if item["revision"] == metadata["profile_revision"]), None)
    if versions and (builtin is None or metadata["profile_sha256"] != canonical_hash(builtin)):
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
