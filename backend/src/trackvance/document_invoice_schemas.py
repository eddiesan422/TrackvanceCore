"""API declaration for optional invoice controls; never loaded by OCR children."""
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")]


class InvoiceTaxTotal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    rate: str = Field(min_length=1, max_length=96, pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")
    field: Identifier

    @field_validator("rate")
    @classmethod
    def bounded_rate(cls, value):
        if not Decimal(0) <= Decimal(value) <= Decimal(100):
            raise ValueError("Invoice tax rates must be percentage points between zero and 100.")
        return value


class InvoiceHeaders(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    gross_subtotal: Identifier
    discount_total: Identifier
    net_base: Identifier
    exempt_base: Identifier
    tax_totals: list[InvoiceTaxTotal] = Field(default_factory=list, max_length=30)
    invoice_total: Identifier
    advance: Identifier
    balance: Identifier

    def bound_names(self):
        return [self.gross_subtotal, self.discount_total, self.net_base, self.exempt_base,
            self.invoice_total, self.advance, self.balance, *(item.field for item in self.tax_totals)]

    @model_validator(mode="after")
    def distinct_roles(self):
        names = [name.casefold() for name in self.bound_names()]
        rates = [Decimal(item.rate) for item in self.tax_totals]
        if len(set(names)) != len(names) or len(set(rates)) != len(rates):
            raise ValueError("Invoice header roles and numerical tax rates must be distinct.")
        return self


class InvoiceControls(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=100)
    table: Identifier
    quantity: Identifier
    unit_price: Identifier
    discount_percent: Identifier
    tax_percent: Identifier
    line_net: Identifier
    headers: InvoiceHeaders
    rounding_scope: Literal["ROW", "AGGREGATE"] = "ROW"
    rounding_mode: Literal["ROUND_HALF_UP"] = "ROUND_HALF_UP"
    scale: int = Field(default=2, ge=0, le=12)
    tolerance: str = Field(default="0.01", min_length=1, max_length=96,
        pattern=r"^(?:0|[1-9][0-9]*)(?:\.[0-9]{1,12})?$")
    percentage_decimal_separator: Literal[".", ","] = "."
    string_percentage_suffix: Literal["REQUIRED", "OPTIONAL"] = "REQUIRED"
    discount_header_sign: Literal["POSITIVE_DEDUCTION", "NEGATIVE_DEDUCTION"] = "POSITIVE_DEDUCTION"
    advance_header_sign: Literal["POSITIVE_DEDUCTION", "NEGATIVE_DEDUCTION"] = "POSITIVE_DEDUCTION"

    @model_validator(mode="after")
    def distinct_and_bounded(self):
        names = [getattr(self, role).casefold() for role in
                 ("quantity", "unit_price", "discount_percent", "tax_percent", "line_net")]
        if len(set(names)) != len(names) or self.table.casefold() == "headers":
            raise ValueError("Invoice row roles must be distinct within a declared detail table.")
        if Decimal(self.tolerance) > Decimal(1).scaleb(-self.scale):
            raise ValueError("Invoice tolerance cannot exceed one unit of its declared scale.")
        return self
