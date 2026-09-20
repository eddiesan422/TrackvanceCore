"""Public connector responses. Stored secret references never cross this boundary."""

from typing import Any, Literal

from pydantic import BaseModel, Field


class ConnectionResponse(BaseModel):
    id: str
    name: str
    source_type: Literal["POSTGRESQL", "SQLSERVER"]
    enabled: bool
    version: int
    host: str
    port: int
    database: str
    username: str
    options: dict[str, Any]
    connection_version_id: str
    config_hash: str
    last_test_status: str
    last_test_at: str | None
    last_test_message: str
    created_at: str
    updated_at: str


class ConnectionListResponse(BaseModel):
    items: list[ConnectionResponse]
    total: int


class ConnectionTestResponse(BaseModel):
    status: Literal["SUCCESS"]
    message: str
    tested_at: str


class ConnectionDeleteResponse(BaseModel):
    ok: bool


class SourceSchemasResponse(BaseModel):
    items: list[str]
    total: int


class SourceObjectResponse(BaseModel):
    name: str
    kind: Literal["TABLE", "VIEW"]


class SourceObjectsResponse(BaseModel):
    items: list[SourceObjectResponse]
    total: int


class SourceColumnResponse(BaseModel):
    name: str
    native_type: str
    logical_type: str
    nullable: bool
    numeric: bool


class SourcePreviewResponse(BaseModel):
    columns: list[SourceColumnResponse]
    rows: list[dict[str, str | None]] = Field(description="Valores normalizados de la muestra; máximo 100 registros.")
    sampled_rows: int


class SourceDatasetResponse(BaseModel):
    dataset: dict[str, Any] = Field(description="Dataset DTO compartido con GET /datasets/{id}.")
    version: dict[str, Any] = Field(description="DatasetVersion inmutable, con Parquet canónico, artifacts y linaje.")
