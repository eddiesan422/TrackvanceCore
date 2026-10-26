"""Versioned, label/context based extraction contracts with exact scalar formats."""
import re
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_serializer,
    model_validator,
)

from .document_invoice_controls import validated_invoice_controls
from .document_invoice_schemas import InvoiceControls
from .document_normalization import DocumentNormalization
from .document_template_builder_schemas import VisualFieldRule, VisualTableRule


class DocumentField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
    label: str = Field(default="", max_length=160)
    pattern: str = Field(default=r"(.+)", min_length=1, max_length=500)
    logical_type: Literal["STRING", "DECIMAL", "INT64", "DATE", "BOOLEAN"] = "STRING"
    required: bool = True
    critical: bool = False
    business_key: bool = False
    date_format: str = Field(default="%Y-%m-%d", max_length=40)
    decimal_separator: Literal[".", ","] = "."
    thousands_separator: Literal[".", ",", " "] | None = None
    currency_symbols: list[str] = Field(default_factory=list, max_length=10)
    decimal_format_explicit: bool = True
    numeric_fragments: bool = False
    section: Literal["DOCUMENT", "ISSUER", "BUYER", "TOTALS"] = "DOCUMENT"
    visual: VisualFieldRule | None = None
    visual_alternatives: list[VisualFieldRule] = Field(default_factory=list, max_length=20)
    normalization: DocumentNormalization | None = None

    @model_serializer(mode="wrap")
    def preserve_legacy_normalization(self, handler):
        result = handler(self)
        if self.normalization is None:
            result.pop("normalization", None)
        return result

    @model_validator(mode="after")
    def numeric_format(self):
        if self.thousands_separator == self.decimal_separator:
            raise ValueError("Los separadores decimal y de millares deben ser distintos.")
        if self.normalization:
            if self.normalization.numeric_semantics == "DEDUCTION_MAGNITUDE" and self.logical_type != "DECIMAL":
                raise ValueError("La magnitud de deducción requiere un campo decimal explícito.")
            if self.normalization.decimal_scale is not None and self.logical_type != "DECIMAL":
                raise ValueError("La escala declarada requiere un campo decimal.")
            if self.normalization.currency_equivalences and self.logical_type != "STRING":
                raise ValueError("La normalización de moneda requiere un campo de texto.")
            if self.normalization.text_linebreaks and (self.logical_type != "STRING"
                    or self.business_key or self.normalization.currency_equivalences):
                raise ValueError("Los saltos de línea declarados requieren texto documental, no claves ni moneda.")
        return self

    @field_validator("pattern")
    @classmethod
    def regex(cls, value):
        try:
            if re.compile(value).groups != 1:
                raise ValueError("Cada campo requiere un grupo de captura.")
        except re.error:
            raise ValueError("Expresión regular inválida.") from None
        return value


class DocumentTable(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
    row_pattern: str = Field(default="", max_length=1000)
    fields: list[DocumentField] = Field(min_length=1, max_length=40)
    required: bool = True
    row_label: str = Field(default="", max_length=160)
    visual: VisualTableRule | None = None
    structured_header_labels: list[str] | None = Field(default=None, min_length=1, max_length=40)

    @model_serializer(mode="wrap")
    def preserve_legacy_table(self, handler):
        result = handler(self)
        if self.structured_header_labels is None:
            result.pop("structured_header_labels", None)
        return result

    @model_validator(mode="after")
    def row_groups(self):
        if self.structured_header_labels is not None:
            if (self.visual is not None or self.row_pattern or len(self.structured_header_labels) != len(self.fields)
                    or any(not label.strip() or len(label) > 160 for label in self.structured_header_labels)
                    or len({label.casefold().strip() for label in self.structured_header_labels}) != len(self.fields)
                    or len({field.name.casefold() for field in self.fields}) != len(self.fields)):
                raise ValueError("La tabla estructurada requiere etiquetas únicas por columna y conserva geometría desconocida.")
            return self
        if self.visual:
            if len(self.fields) != len(self.visual.header_labels):
                raise ValueError("La tabla visual requiere una definición por columna.")
            if len({f.name.casefold() for f in self.fields}) != len(self.fields):
                raise ValueError("Los nombres finales de columnas deben ser únicos.")
            return self
        if not self.row_pattern:
            raise ValueError("Selecciona una tabla visual o configura el patrón avanzado.")
        try:
            groups = re.compile(self.row_pattern).groupindex
        except re.error:
            raise ValueError("Expresión de fila inválida.") from None
        if set(groups) != {f.name for f in self.fields}:
            raise ValueError("La fila requiere grupos con los nombres de todas sus columnas.")
        if len({f.name.casefold() for f in self.fields}) != len(self.fields):
            raise ValueError("Los nombres finales de columnas deben ser únicos.")
        return self


class DocumentPublication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["INDIVIDUAL", "SCHEDULED"] = "INDIVIDUAL"
    timezone: str = "America/Bogota"
    starts_at: datetime | None = None
    interval_seconds: int = Field(default=86400, ge=60, le=2678400)
    close_policy: Literal["STRICT", "READY_DEFER_PENDING"] = "STRICT"
    wait_seconds: int = Field(default=300, ge=0, le=86400)
    review_policy: Literal["ALL", "CRITICAL_OR_UNCERTAIN", "AUTO_VERIFIED"] = "ALL"
    minimum_confidence: float = Field(default=90, ge=0, le=100)
    stop_after: Literal["PUBLICATION", "VALIDATION", "DELIVERY"] = "PUBLICATION"
    intake_configuration_ids: dict[str, str] = Field(default_factory=dict)
    delivery_automation_ids: dict[str, str] = Field(default_factory=dict)
    automatic_delivery: bool = False

    @model_validator(mode="after")
    def schedule(self):
        try:
            ZoneInfo(self.timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Indica una zona horaria IANA disponible.") from None
        if self.starts_at and self.starts_at.tzinfo is None:
            raise ValueError("El inicio requiere zona horaria explícita.")
        if self.starts_at:
            self.starts_at = self.starts_at.astimezone(UTC)
        if self.mode == "SCHEDULED" and not self.starts_at:
            raise ValueError("La modalidad calendarizada requiere inicio explícito.")
        if self.stop_after != "PUBLICATION" and not self.intake_configuration_ids:
            raise ValueError("Vincula un contrato Intake para cada estructura.")
        if self.automatic_delivery and (self.stop_after != "DELIVERY" or not self.delivery_automation_ids):
            raise ValueError("La entrega automática requiere activación y automatizaciones nativas.")
        return self


class DocumentValidationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal["EVIDENCE_V2"] = "EVIDENCE_V2"
    allow_noncritical_source_bound: bool = False
    critical_corroboration: Literal["INDEPENDENT_SOURCE"] = "INDEPENDENT_SOURCE"
    require_table_structure: Literal[True] = True


class DocumentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[DocumentField] = Field(min_length=1, max_length=100)
    tables: list[DocumentTable] = Field(default_factory=list, max_length=10)
    publication: DocumentPublication = Field(default_factory=DocumentPublication)
    business_period_field: str | None = None
    arithmetic: list["DocumentArithmetic"] = Field(default_factory=list, max_length=50)
    cross_totals: list["DocumentCrossTotal"] = Field(default_factory=list, max_length=30)
    invoice_controls: InvoiceControls | None = None
    validation_policy: DocumentValidationPolicy | None = None

    @model_serializer(mode="wrap")
    def preserve_historical_definition(self, handler):
        result = handler(self)
        if not self.cross_totals:
            result.pop("cross_totals", None)
        if self.invoice_controls is None:
            result.pop("invoice_controls", None)
        if self.validation_policy is None:
            result.pop("validation_policy", None)
        return result

    @model_validator(mode="after")
    def names(self):
        names = [f.name.casefold() for f in self.fields]
        tables = [t.name.casefold() for t in self.tables]
        reserved = {"document_id", "extraction_id", "batch_id", "row_number", "__tv_record_number"}
        if len(set(names)) != len(names) or set(names) & reserved:
            raise ValueError("Campos duplicados o reservados.")
        if len(set(tables)) != len(tables) or "headers" in tables:
            raise ValueError("Tablas duplicadas o reservadas.")
        for table in self.tables:
            if {f.name.casefold() for f in table.fields} & reserved:
                raise ValueError("Columnas de tabla reservadas.")
        if self.business_period_field and self.business_period_field not in {f.name for f in self.fields}:
            raise ValueError("El período de negocio debe referenciar un campo de cabecera declarado.")
        for check in self.arithmetic:
            scope = next((t.fields for t in self.tables if t.name == check.table), None) if check.table else self.fields
            if scope is None or not {check.result, *check.terms, *check.subtract}.issubset({f.name for f in scope}):
                raise ValueError("La comprobación aritmética requiere campos declarados en su estructura.")
        for total_check in self.cross_totals:
            total_table = next((item for item in self.tables if item.name == total_check.table), None)
            column = next((item for item in total_table.fields if item.name == total_check.column), None) if total_table else None
            header = next((item for item in self.fields if item.name == total_check.header), None)
            if not column or not header or column.logical_type not in {"DECIMAL", "INT64"} or header.logical_type not in {"DECIMAL", "INT64"}:
                raise ValueError("El total cruzado requiere una columna de detalle y una cabecera numéricas declaradas.")
        if self.invoice_controls is not None:
            validated_invoice_controls({"invoice_controls": self.invoice_controls.model_dump(mode="json"),
                "fields": [field.model_dump(mode="json") for field in self.fields],
                "tables": [table.model_dump(mode="json") for table in self.tables]})
        return self


class DocumentArithmetic(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    table: str | None = None
    operation: Literal["SUM", "PRODUCT"] = "SUM"
    terms: list[str] = Field(min_length=1, max_length=30)
    subtract: list[str] = Field(default_factory=list, max_length=30)
    result: str
    scale: int = Field(default=2, ge=0, le=12)
    tolerance: str = Field(default="0.01", pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")


class DocumentCrossTotal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    table: str
    column: str
    header: str
    scale: int = Field(default=2, ge=0, le=12)
    tolerance: str = Field(default="0.01", pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")


class DocumentDraftDefinition(DocumentDefinition):
    """A genuine empty draft is valid storage, never an executable template."""
    fields: list[DocumentField] = Field(default_factory=list, max_length=100)


class IncompleteSourceConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_extraction_id: str = Field(min_length=1, max_length=100)
    expected_payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    expected_marker_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    pages: list[int] = Field(min_length=1, max_length=50)
    all_pages_confirmed: bool = Field(strict=True)
    all_rows_confirmed: bool = Field(strict=True)

    @model_validator(mode="before")
    @classmethod
    def exact_pages_and_confirmation(cls, value):
        if isinstance(value, dict):
            pages = value.get("pages")
            if (not isinstance(pages, list) or any(type(page) is not int or not 1 <= page <= 50 for page in pages)
                    or pages != sorted(set(pages)) or value.get("all_pages_confirmed") is not True
                    or value.get("all_rows_confirmed") is not True):
                raise ValueError("Confirma todas las páginas y filas con sus índices exactos.")
        return value


def structures(definition: dict) -> dict[str, list[dict]]:
    return {"headers": definition["fields"], **{t["name"]: t["fields"] for t in definition["tables"]}}
