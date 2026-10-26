"""Document provider boundary, administrative settings and immutable job snapshots.

Only a private local inference service can be selected in 0.9.8. No provider
failure falls back to an external service. Existing documents without a snapshot
continue to use the legacy configuration when their first attempt starts.
"""
from __future__ import annotations

import copy
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from .document_models import DocumentEngineSettings, DocumentFamily
from .models import Role
from .operations_common import OperationError

Provider = Literal["PADDLE_QWEN_LOCAL", "AWS_TEXTRACT", "TESSERACT_LEGACY"]


def hash_value(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"), allow_nan=False).encode()).hexdigest()


class EngineConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    provider: Provider = "TESSERACT_LEGACY"
    paddle_model: Literal["PaddleOCR-VL-1.6"] = "PaddleOCR-VL-1.6"
    llm_model: Literal["qwen3.5:9b-q4_K_M"] = "qwen3.5:9b-q4_K_M"
    prompt_version: Literal["trackvance-idp-098-v1", "trackvance-idp-098-v2"] = "trackvance-idp-098-v2"
    timeout_seconds: int = Field(default=300, ge=30, le=1800)
    context_tokens: int = Field(default=8192, ge=1024, le=32768)
    max_retries: int = Field(default=1, ge=0, le=2)
    max_parallel_jobs: int = Field(default=1, ge=1, le=4)
    use_llm: bool = True
    extraction_strategy: Literal["LEGACY", "NATIVE_FIRST", "QWEN_VISUAL", "PADDLE_STRUCTURED", "SELECTIVE"] = "LEGACY"

    @model_serializer(mode="wrap")
    def preserve_historical_configuration(self, handler):
        value = handler(self)
        if self.extraction_strategy == "LEGACY":
            value.pop("extraction_strategy", None)
        return value


class EngineSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    configuration: EngineConfiguration
    reason: str = Field(min_length=1, max_length=2000)


def administrator(db, user):
    role = db.get(Role, user.role_id)
    return bool(role and role.organization_id == user.organization_id
        and role.active and not role.deleted and role.system_key == "ADMINISTRATOR"
        and user.active and not user.deleted)


def _family(db, organization_id, family_id):
    if family_id is None:
        return
    item = db.get(DocumentFamily, family_id)
    if item is None or item.organization_id != organization_id:
        raise OperationError(404, "DOCUMENT_NOT_FOUND", "No se encontró la familia documental.")


def settings_for(db, organization_id, family_id=None, *, exact=False):
    _family(db, organization_id, family_id)
    item = db.scalar(select(DocumentEngineSettings).where(
        DocumentEngineSettings.organization_id == organization_id,
        DocumentEngineSettings.scope_key == (family_id or "")))
    if item is None and family_id and not exact:
        item = db.scalar(select(DocumentEngineSettings).where(
            DocumentEngineSettings.organization_id == organization_id,
            DocumentEngineSettings.scope_key == ""))
    if item is not None and hash_value(item.configuration) != item.configuration_hash:
        raise OperationError(409, "DOCUMENT_ENGINE_CONFIGURATION_INTEGRITY", "La configuración del motor perdió integridad.")
    return item


def provider_catalog():
    from .document_local_inference import availability
    from .document_recognition_transport import transport_available
    local = availability()
    if not transport_available():
        local.update(status="UNAVAILABLE", available=False, execution_enabled=False,
            reason="Debe comprobarse el cierre del reconocimiento anterior antes de otra inferencia.")
    return [
        {"id": "PADDLE_QWEN_LOCAL", "name": "PaddleOCR-VL + Qwen local",
         "model": "PaddleOCR-VL-1.6 / Qwen3.5 9B Q4_K_M", **local},
        {"id": "TESSERACT_LEGACY", "name": "Tesseract histórico", "model": "Poppler / Tesseract CPU",
         "status": "AVAILABLE", "available": True, "execution_enabled": True,
         "reason": "El worker Linux verifica el sandbox al ejecutar."},
        {"id": "AWS_TEXTRACT", "name": "Amazon Textract AnalyzeExpense", "model": "AnalyzeExpense",
         "status": "PREPARED_DISABLED", "available": False, "execution_enabled": False,
         "reason": "Adaptador probado con simulaciones. AWS real está bloqueado en 0.9.8."},
    ]


def settings_dto(db, user, family_id=None):
    item = settings_for(db, user.organization_id, family_id)
    exact = item is not None and item.scope_key == (family_id or "")
    return {"configuration": copy.deepcopy(item.configuration) if item else EngineConfiguration().model_dump(),
        "version": item.version if exact else 0,
        "inherited_version": item.version if item and not exact else None,
        "scope": "FAMILY" if family_id else "ORGANIZATION", "family_id": family_id,
        "can_configure": administrator(db, user), "providers": provider_catalog()}


def save_settings(db, user, body, family_id=None):
    from .document_service import permission
    from .services import audit
    permission(db, user, "configure")
    if not administrator(db, user):
        raise OperationError(403, "DOCUMENT_ENGINE_ADMIN_REQUIRED", "Solo Administración puede cambiar el proveedor documental.")
    if not body.reason.strip():
        raise OperationError(422, "DOCUMENT_ENGINE_REASON_REQUIRED", "Registra el motivo del cambio.")
    _family(db, user.organization_id, family_id)
    config = body.configuration.model_dump()
    if config["provider"] == "AWS_TEXTRACT":
        raise OperationError(422, "DOCUMENT_EXTERNAL_PROVIDER_DISABLED", "AWS real permanece bloqueado en 0.9.8; utiliza el simulador contractual.")
    item = settings_for(db, user.organization_id, family_id, exact=True)
    previous = copy.deepcopy(item.configuration) if item else None
    if item is None:
        if body.expected_version != 0:
            raise OperationError(409, "DOCUMENT_ENGINE_CONFIGURATION_CONFLICT", "Actualiza la configuración antes de guardar.")
        item = DocumentEngineSettings(organization_id=user.organization_id, scope_key=family_id or "",
            version=1, configuration=config, configuration_hash=hash_value(config), actor_id=user.id,
            reason=body.reason.strip())
        try:
            with db.begin_nested():
                db.add(item)
                db.flush()
        except IntegrityError:
            raise OperationError(409, "DOCUMENT_ENGINE_CONFIGURATION_CONFLICT", "Otra administración guardó la configuración.") from None
    else:
        result = db.execute(update(DocumentEngineSettings).where(DocumentEngineSettings.id == item.id,
            DocumentEngineSettings.version == body.expected_version).values(version=body.expected_version + 1,
            configuration=config, configuration_hash=hash_value(config), actor_id=user.id, reason=body.reason.strip()))
        if result.rowcount != 1:
            raise OperationError(409, "DOCUMENT_ENGINE_CONFIGURATION_CONFLICT", "Actualiza la configuración antes de guardar.")
        db.refresh(item)
    audit(db, "DOCUMENT_ENGINE_CONFIGURED", "document_engine_settings", item.id,
        "Proveedor documental configurado para nuevos trabajos. Motivo: " + body.reason.strip(), user.name, user.organization_id,
        {"configuration_id": item.id, "version": item.version, "provider": config["provider"],
         "config_hash": item.configuration_hash, "previous_config_hash": hash_value(previous) if previous else None,
         "previous_provider": previous["provider"] if previous else None,
         "scope": "FAMILY" if family_id else "ORGANIZATION", "family_id": family_id,
         **{key: value for key, value in config.items() if key != "provider"}})
    return item


def freeze_configuration(db, subject):
    """Call under the successful job claim transaction; retries reuse exact bytes."""
    if subject.engine_snapshot is not None:
        if hash_value(subject.engine_snapshot) != subject.engine_snapshot_hash:
            raise OperationError(409, "DOCUMENT_ENGINE_SNAPSHOT_INTEGRITY", "La configuración congelada perdió integridad.")
        return copy.deepcopy(subject.engine_snapshot)
    item = settings_for(db, subject.organization_id, subject.family_id)
    configuration = EngineConfiguration.model_validate(item.configuration if item else {}).model_dump()
    snapshot = {**configuration, "configuration_version": item.version if item else 0,
        "scope": "FAMILY" if item and item.scope_key else "ORGANIZATION",
        "family_id": item.scope_key if item and item.scope_key else None}
    subject.engine_snapshot, subject.engine_snapshot_hash = snapshot, hash_value(snapshot)
    return copy.deepcopy(snapshot)


def execute_provider(source, definition, configuration, *, legacy_runner, check, budget=None,
                     on_event=None, attempt_context=None):
    from .document_inference_completion import LocalCandidateResult, canonical_hash
    from .document_quality import validate_candidate_contract
    provider = configuration["provider"]
    if provider == "AWS_TEXTRACT":
        raise OperationError(503, "DOCUMENT_EXTERNAL_PROVIDER_DISABLED", "No se permiten llamadas reales a AWS en 0.9.8.")
    if provider == "TESSERACT_LEGACY":
        data = legacy_runner(source, definition, check=check)
    elif provider == "PADDLE_QWEN_LOCAL":
        if configuration.get("extraction_strategy", "LEGACY") == "LEGACY":
            from .document_local_inference import run_local
            data = run_local(source, definition, configuration, legacy_runner=legacy_runner, check=check, budget=budget)
        else:
            from .document_lossless import run_lossless
            data = run_lossless(source, definition, configuration, legacy_runner=legacy_runner, check=check,
                budget=budget, on_event=on_event, attempt_context=attempt_context)
    else:
        raise OperationError(422, "DOCUMENT_PROVIDER_UNKNOWN", "El proveedor no pertenece al contrato autorizado.")
    validate_candidate_contract(data, definition)
    # A candidate cannot create a validator attestation or assert human review.
    marker = data.completion_marker if isinstance(data, LocalCandidateResult) else None
    data = copy.deepcopy(dict(data))
    data["engine"].pop("validation", None)
    data["engine"].pop("inference_completion", None)
    data["engine"].pop("inference_completion_sha256", None)
    if marker is not None:
        if (marker.get("definition_sha256") != canonical_hash(definition)
                or marker.get("configuration_sha256") != canonical_hash(configuration)
                or marker.get("checkpoint_payload_sha256") != canonical_hash(data["payload"])):
            raise OperationError(422, "DOCUMENT_PROVIDER_CONTRACT", "No se pudo verificar el resultado parcial del worker.")
        data["engine"]["inference_completion"] = copy.deepcopy(marker)
        data["engine"]["inference_completion_sha256"] = canonical_hash(marker)
    data["engine"].update(provider=provider, configuration=copy.deepcopy(configuration),
        configuration_sha256=hash_value(configuration))
    validate_candidate_contract(data, definition, allow_worker_completion=True)
    return data
