"""Typed HTTP contracts for Data Delivery; secret references never leave storage."""

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, SecretStr, model_validator

from .data_sinks import DeliveryError, validate_identifier


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, str_strip_whitespace=False
    )


class DestinationConnectionBody(StrictModel):
    name: str = Field(min_length=1, max_length=160)
    sink_type: Literal["POSTGRESQL", "SQLSERVER"]
    host: str = Field(min_length=1, max_length=253)
    port: int = Field(ge=1, le=65535)
    database: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    options: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def normalize_display_name(self):
        self.name = self.name.strip()
        if not self.name:
            raise ValueError("Indica un nombre para el destino.")
        return self


class DestinationBody(DestinationConnectionBody):
    password: SecretStr = Field(min_length=1, max_length=1024)


class DestinationTestBody(DestinationConnectionBody):
    password: SecretStr | None = Field(default=None, min_length=1, max_length=1024)
    destination_id: str | None = None


class DestinationPatch(StrictModel):
    version: int = Field(ge=1, validation_alias=AliasChoices("version", "expected_version"))
    name: str | None = Field(default=None, min_length=1, max_length=160)
    host: str | None = Field(default=None, min_length=1, max_length=253)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str | None = Field(default=None, min_length=1, max_length=128)
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password: SecretStr | None = Field(default=None, max_length=1024)
    options: dict[str, Any] | None = None
    enabled: bool | None = None


class DestinationResponse(BaseModel):
    id: str
    name: str
    sink_type: Literal["POSTGRESQL", "SQLSERVER"]
    enabled: bool
    version: int
    host: str
    port: int
    database: str
    username: str
    options: dict[str, Any]
    destination_version_id: str
    config_hash: str
    last_test_status: str
    last_test_at: str | None
    last_test_message: str
    created_at: str
    updated_at: str


class DestinationListResponse(BaseModel):
    items: list[DestinationResponse]
    total: int


class DestinationTestResponse(BaseModel):
    status: Literal["SUCCESS"]
    message: str
    tested_at: str


class TargetSpec(StrictModel):
    mode: Literal["EXISTING_TABLE", "CREATE_TABLE"]
    schema_name: str = Field(min_length=1, max_length=128)
    table_name: str = Field(min_length=1, max_length=128)
    create_schema: bool = False

    @model_validator(mode="after")
    def validate_names(self):
        try:
            validate_identifier(self.schema_name)
            validate_identifier(self.table_name)
        except DeliveryError as error:
            raise ValueError(error.message) from None
        if self.mode == "EXISTING_TABLE" and self.create_schema:
            raise ValueError("Una tabla existente no puede crear su schema.")
        return self


class ColumnMapping(StrictModel):
    source_name: str = Field(min_length=1, max_length=256)
    target_name: str = Field(min_length=1, max_length=128)
    target_type: Literal["STRING", "INT64", "DECIMAL", "DATE", "TIMESTAMP", "BOOLEAN"]
    ordinal: int = Field(ge=0, le=99)
    nullable: bool
    precision: int | None = Field(default=None, ge=1, le=38)
    scale: int | None = Field(default=None, ge=0, le=38)
    length: int | None = Field(default=None, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def validate_parameters(self):
        try:
            validate_identifier(self.target_name)
        except DeliveryError as error:
            raise ValueError(error.message) from None
        if self.target_type == "DECIMAL":
            precision = self.precision or 38
            scale = self.scale if self.scale is not None else 10
            if scale > precision:
                raise ValueError("La escala DECIMAL no puede superar la precisión.")
        elif self.precision is not None or self.scale is not None:
            raise ValueError("Precisión y escala solo aplican a columnas DECIMAL.")
        if self.target_type != "STRING" and self.length is not None:
            raise ValueError("La longitud solo aplica a columnas STRING.")
        return self


class DeliveryDraft(StrictModel):
    schema_version: Literal[1] = 1
    dataset_version_id: str = Field(min_length=1, max_length=64)
    destination_id: str = Field(min_length=1, max_length=64)
    destination_version_id: str = Field(min_length=1, max_length=64)
    target: TargetSpec
    columns: list[ColumnMapping] = Field(min_length=1, max_length=100)
    write_strategy: Literal["CREATE_AND_LOAD", "APPEND", "OVERWRITE", "UPSERT"]
    upsert_keys: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_plan(self):
        sources = [column.source_name for column in self.columns]
        targets = [column.target_name for column in self.columns]
        ordinals = [column.ordinal for column in self.columns]
        if len(set(sources)) != len(sources):
            raise ValueError("Una columna de origen no puede mapearse más de una vez.")
        if len(set(targets)) != len(targets):
            raise ValueError("Los nombres de destino no pueden repetirse.")
        if len(set(ordinals)) != len(ordinals):
            raise ValueError("El orden de columnas no puede repetirse.")
        self.columns.sort(key=lambda column: column.ordinal)
        if self.target.mode == "CREATE_TABLE" and self.write_strategy != "CREATE_AND_LOAD":
            raise ValueError("Una tabla nueva requiere CREATE_AND_LOAD.")
        if self.target.mode == "EXISTING_TABLE" and self.write_strategy == "CREATE_AND_LOAD":
            raise ValueError("CREATE_AND_LOAD solo aplica a una tabla nueva.")
        if self.write_strategy == "UPSERT":
            if not self.upsert_keys or len(set(self.upsert_keys)) != len(self.upsert_keys):
                raise ValueError("UPSERT requiere claves únicas explícitas.")
            if set(self.upsert_keys) - set(targets):
                raise ValueError("Las claves UPSERT deben pertenecer al mapping de destino.")
        elif self.upsert_keys:
            raise ValueError("Las claves UPSERT solo aplican a esa estrategia.")
        return self

    def snapshot(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class DeliveryPreviewResponse(BaseModel):
    dataset_version_id: str
    columns: list[dict[str, Any]]
    source_rows: list[dict[str, Any]]
    destination_rows: list[dict[str, Any]]
    sampled_rows: int


class DeliveryPreflightResponse(BaseModel):
    status: Literal["PASS"]
    checks: list[dict[str, Any]]
    source: dict[str, Any]
    destination: dict[str, Any]
    target: dict[str, Any]


class DeliveryConfigurationBody(DeliveryDraft):
    name: str = Field(min_length=1, max_length=160)
    owner: str = Field(default="Equipo de datos", min_length=1, max_length=120)
    description: str = Field(default="", max_length=4000)


class DeliveryConfigurationVersionBody(DeliveryDraft):
    description: str | None = Field(default=None, max_length=4000)


class DeliveryRunBody(StrictModel):
    configuration_id: str = Field(min_length=1, max_length=64)
    dataset_version_id: str = Field(min_length=1, max_length=64)


class DeliveryAttemptsResponse(BaseModel):
    items: list[dict[str, Any]]
    total: int
