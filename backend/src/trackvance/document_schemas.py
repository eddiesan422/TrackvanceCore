"""Versioned, label/context based extraction contracts with exact scalar formats."""
import re
from datetime import UTC, datetime
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    visual: VisualFieldRule | None = None

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

    @model_validator(mode="after")
    def row_groups(self):
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
    review_policy: Literal["ALL", "CRITICAL_OR_UNCERTAIN"] = "ALL"
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


class DocumentDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[DocumentField] = Field(min_length=1, max_length=100)
    tables: list[DocumentTable] = Field(default_factory=list, max_length=10)
    publication: DocumentPublication = Field(default_factory=DocumentPublication)
    business_period_field: str | None = None

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
        return self


def structures(definition: dict) -> dict[str, list[dict]]:
    return {"headers": definition["fields"], **{t["name"]: t["fields"] for t in definition["tables"]}}
