"""Bounded visual rules. No executable expressions or user supplied code."""
import re
from itertools import pairwise
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from .document_template_builder_rules import normalized


class VisualRegion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page: int = Field(default=1, ge=1, le=50)
    region: list[float] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def ordered(self):
        x1, y1, x2, y2 = self.region
        if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
            raise ValueError("Selecciona una región válida dentro de la página.")
        return self


class VisualFieldRule(VisualRegion):
    kind: Literal["LABEL_VALUE", "REGION_CONTEXT"] = "LABEL_VALUE"
    label: str = Field(min_length=1, max_length=160)
    direction: Literal["RIGHT", "BELOW"] = "RIGHT"
    sample_value: str = Field(default="", max_length=500)
    context: str = Field(default="", max_length=160)
    end_label: str = Field(default="", max_length=160)
    tolerance: float = Field(default=0.12, ge=0, le=0.25)
    label_aliases: list[str] = Field(default_factory=list, max_length=30)
    section_label: str = Field(default="", max_length=160)
    require_unique_origin: bool = False
    section_bounds: list[float] | None = Field(default=None, min_length=2, max_length=2)
    value_pattern: str = Field(default="", max_length=500)
    baseline_tolerance: float = Field(default=.004, ge=.001, le=.02)

    @model_validator(mode="after")
    def context_bounds(self):
        if self.section_bounds and not 0 <= self.section_bounds[0] < self.section_bounds[1] <= 1:
            raise ValueError("La sección requiere límites horizontales ordenados.")
        if self.value_pattern:
            try:
                if re.compile(self.value_pattern).groups != 1:
                    raise ValueError("El patrón de valor requiere un grupo de captura.")
            except re.error:
                raise ValueError("Patrón de valor inválido.") from None
        return self


class ContinuationContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    region: list[float] = Field(min_length=4, max_length=4)
    label: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def bounded_literal(self):
        left, top, right, bottom = self.region
        if not (0 <= left < right <= 1 and 0 <= top < bottom <= 1 and bottom - top <= .15):
            raise ValueError("El contexto de continuación requiere una región de hasta 15 % de la página.")
        if not any(character.isalpha() for character in self.label) or len(self.label.split()) > 24:
            raise ValueError("Indica una etiqueta literal completa de hasta 24 palabras.")
        return self


class VisualTableRule(VisualRegion):
    header_labels: list[str] = Field(min_length=2, max_length=40)
    column_bounds: list[float] = Field(min_length=3, max_length=41)
    stop_label: str = Field(default="", max_length=160)
    stop_label_aliases: list[str] = Field(default_factory=list, max_length=20)
    tolerance: float = Field(default=0.12, ge=0, le=0.25)
    flowing: bool = False
    continue_pages: bool = False
    multiline_column: int | None = Field(default=None, ge=0, le=39)
    row_tolerance: float = Field(default=.018, ge=.004, le=.03)
    continuation_context: ContinuationContext | None = None

    @model_serializer(mode="wrap")
    def serialize_continuation_context(self, handler: SerializerFunctionWrapHandler):
        result = handler(self)
        # Existing normalized definitions/snapshots have no such property. Its
        # absent default must not rehash or invalidate any historical template.
        if self.continuation_context is None:
            result.pop("continuation_context", None)
        return result

    @model_validator(mode="after")
    def columns(self):
        if any(not label.strip() or len(label) > 160 for label in self.stop_label_aliases):
            raise ValueError("Indica etiquetas de fin no vacías de hasta 160 caracteres.")
        stops = [normalized(label) for label in [self.stop_label, *self.stop_label_aliases] if label.strip()]
        if len(set(stops)) != len(stops):
            raise ValueError("Las etiquetas de fin deben ser distintas.")
        if len(self.column_bounds) != len(self.header_labels) + 1:
            raise ValueError("Cada columna requiere dos límites consecutivos.")
        if any(not s.strip() or len(s) > 160 for s in self.header_labels):
            raise ValueError("Indica las etiquetas de la cabecera.")
        if len({s.strip().casefold() for s in self.header_labels}) != len(self.header_labels):
            raise ValueError("Cada columna requiere una etiqueta distinta.")
        if not all(0 <= a < b <= 1 for a, b in pairwise(self.column_bounds)):
            raise ValueError("Ordena los límites de columna de izquierda a derecha.")
        return self


class BuilderProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selection: VisualRegion
    kind: Literal["FIELD", "TABLE"] = "FIELD"
