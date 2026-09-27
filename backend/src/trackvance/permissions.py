"""Product catalog and exhaustive HTTP matrix; persisted roles are the sole authority."""
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Role, RolePermission, User

GROUPS = {"datasets": "Datasets", "connections": "Conexiones", "intake": "Data Intake", "recon": "ReconOps", "sentinel": "Sentinel", "delivery": "Data Delivery", "destinations": "Destinos", "exceptions": "Excepciones", "rules": "Reglas", "exports": "Evidencia / exports", "artifacts": "Evidencia / exports", "audit": "Auditoría", "users": "Usuarios", "roles": "Roles", "notifications": "Notificaciones", "system": "Sistema", "runs": "Ejecuciones"}
ACTIONS = {"read": "Consultar", "write": "Crear y editar", "use": "Utilizar", "manage": "Administrar", "configure": "Configurar", "execute": "Ejecutar", "schedule": "Programar", "overwrite": "Reemplazar contenido", "alter_target": "Crear o modificar target", "review_unknown": "Revisar UNKNOWN", "repair_evidence": "Reparar evidencia", "close": "Cerrar", "download": "Descargar"}
_ACTIONS_BY_GROUP = {"datasets": "read write", "connections": "read use manage", "intake": "read configure execute", "recon": "read configure execute", "sentinel": "read configure execute schedule", "delivery": "read configure execute overwrite alter_target review_unknown repair_evidence", "destinations": "read use manage", "exceptions": "read write close", "rules": "read", "exports": "download", "artifacts": "download", "audit": "read", "users": "read manage", "roles": "read manage", "notifications": "read manage", "system": "read", "runs": "read execute"}
CATALOG = frozenset(f"{group}:{action}" for group, actions in _ACTIONS_BY_GROUP.items() for action in actions.split())
NON_DELEGABLE = frozenset({"users:manage", "roles:manage"})
DEPENDENCIES: dict[str, frozenset[str]] = {}
for _code in CATALOG:
    _group, _action = _code.split(":")
    if _action != "read" and f"{_group}:read" in CATALOG:
        DEPENDENCIES[_code] = frozenset({f"{_group}:read"})
for _code in ("delivery:overwrite", "delivery:alter_target", "delivery:repair_evidence"):
    DEPENDENCIES[_code] |= {"delivery:execute"}
DEPENDENCIES["sentinel:schedule"] |= {"sentinel:execute"}
DEPENDENCIES["exceptions:close"] |= {"exceptions:write"}
for _module in ("intake", "recon", "sentinel", "delivery"):
    DEPENDENCIES[f"{_module}:configure"] |= {"datasets:read"}
    DEPENDENCIES[f"{_module}:execute"] |= {"datasets:read"}
DEPENDENCIES["delivery:configure"] |= {"destinations:read", "destinations:use"}


def catalog_dto() -> list[dict]:
    return [{"code": code, "group": GROUPS[code.split(":")[0]], "label": ACTIONS[code.split(":")[1]], "dependencies": sorted(DEPENDENCIES.get(code, ())), "delegable": code not in NON_DELEGABLE} for code in sorted(CATALOG)]


def permissions_for_role(db: Session, role: Role | None) -> list[str]:
    if role is None or not role.active or role.deleted:
        return []
    if role.system_key == "ADMINISTRATOR":
        return sorted(CATALOG)
    grants = set(db.scalars(select(RolePermission.permission_code).where(RolePermission.role_id == role.id)))
    return sorted((grants & CATALOG) - NON_DELEGABLE)


def effective_permissions(db: Session, user: User) -> list[str]:
    if not user.active or user.deleted:
        return []
    role = db.get(Role, user.role_id)
    if role is None or role.organization_id != user.organization_id:
        return []
    return permissions_for_role(db, role)


def readable_modules(db: Session, user: User) -> set[str]:
    grants = effective_permissions(db, user)
    return {module for module in ("intake", "recon", "sentinel", "DELIVERY") if f"{module.lower()}:read" in grants}


def dependency_closure(grants: set[str]) -> set[str]:
    result = set(grants)
    while True:
        expanded = result | {dep for code in result for dep in DEPENDENCIES.get(code, ())}
        if expanded == result:
            return result
        result = expanded


# Defaults seed new organizations only; editing a role never reapplies these grants.
_READ = {f"{group}:read" for group in ("datasets", "connections", "intake", "recon", "sentinel", "delivery", "destinations", "exceptions", "rules", "runs")}
_EXPORT = {"exports:download", "artifacts:download"}
_AUTHOR = {"datasets:write", "runs:execute", "exceptions:write", "connections:use", "destinations:use"} | {f"{group}:{action}" for group in ("intake", "recon", "sentinel", "delivery") for action in ("configure", "execute")} | {"sentinel:schedule", "delivery:overwrite", "delivery:alter_target", "delivery:review_unknown", "delivery:repair_evidence"}
DEFAULT_ROLE_GRANTS = {
    "Administrator": set(),
    "Data Owner / Lead": dependency_closure(_READ | _EXPORT | _AUTHOR | {"audit:read", "exceptions:close", "connections:manage", "destinations:manage"}),
    "Data Analyst": dependency_closure(_READ | _EXPORT | _AUTHOR | {"audit:read"}),
    "Operations": dependency_closure(_READ | _EXPORT | {"exceptions:write"}),
    "Auditor": _READ | _EXPORT | {"audit:read", "users:read", "roles:read", "system:read"},
}
PUBLIC_ENDPOINTS = {("GET", "/health"), ("GET", "/health/ready"), ("POST", "/auth/demo"), ("POST", "/auth/login"), ("GET", "/auth/providers"), *(("GET", f"/auth/sso/{provider}/{action}") for provider in ("microsoft", "google") for action in ("start", "callback"))}
SESSION_ENDPOINTS = {("GET", "/me"), ("POST", "/auth/logout"), ("POST", "/auth/first-login/change-password")}
UNMAPPED = "__unmapped_endpoint__"
_MATRIX: list[tuple[str, str, str]] = []


def _routes(permission: str, method: str, *paths: str) -> None:
    _MATRIX.extend((method, path, permission) for path in paths)


_routes("datasets:read", "GET", "/datasets", "/datasets/{id}", "/datasets/{id}/schema", "/dataset-versions/{id}/profile")
_routes("datasets:write", "POST", "/datasets", "/datasets/uploads/inspect", "/datasets/{id}/versions/upload")
_routes("connections:use", "POST", "/datasets/{id}/refresh-source", "/connections/{id}/test", "/connections/{id}/datasets")
_routes("connections:use", "GET", "/connections/{id}/schemas", "/connections/{id}/objects", "/connections/{id}/preview")
_routes("connections:read", "GET", "/connections", "/connections/{id}")
_routes("connections:manage", "POST", "/connections", "/connections/test")
_routes("connections:manage", "PATCH", "/connections/{id}")
_routes("connections:manage", "DELETE", "/connections/{id}")
for _module, _path in (("intake", "/intake/contracts"), ("recon", "/recon/controls"), ("sentinel", "/monitors")):
    _routes(f"{_module}:read", "GET", _path)
    _routes(f"{_module}:configure", "POST", _path, _path + "/{id}/versions")
_routes("intake:execute", "POST", "/intake/runs")
_routes("recon:execute", "POST", "/recon/runs")
_routes("sentinel:execute", "POST", "/monitors/{id}/runs")
_routes("intake:read", "GET", "/intake/runs/{id}/errors")
_routes("recon:read", "GET", "/recon/runs/{id}/results")
_routes("sentinel:read", "GET", *(f"/monitors/{{id}}/{action}" for action in ("metrics", "schedule", "occurrences", "series", "alerts")))
_routes("sentinel:schedule", "POST", "/monitors/{id}/schedule")
_routes("runs:read", "GET", "/runs", "/runs/{id}", "/runs/{id}/results", "/runs/{id}/execution-plan", "/runs/{id}/diagnostics", "/findings", "/dashboard")
_routes("runs:execute", "POST", "/runs/{id}/cancel", "/execution-plans/preview")
_routes("exports:download", "GET", "/runs/{id}/export.xlsx", "/runs/{id}/export.csv")
_routes("artifacts:download", "GET", "/runs/{id}/evidence", "/artifacts/{id}/download", "/exceptions/{id}/attachments/{attachment_id}/download", "/delivery/runs/{id}/receipt")
_routes("exceptions:read", "GET", "/exceptions", "/exceptions/assignees", "/exceptions/{id}")
_routes("exceptions:write", "POST", "/findings/{id}/exceptions", "/exceptions/{id}/validate", "/exceptions/{id}/comments", "/exceptions/{id}/attachments")
_routes("exceptions:write", "PATCH", "/exceptions/{id}")
_routes("audit:read", "GET", "/audit-events")
_routes("rules:read", "GET", "/rules")
_routes("system:read", "GET", "/system/engines")
_routes("users:read", "GET", "/users", "/users/{id}", "/users/roles")
_routes("users:manage", "POST", "/users", "/users/{id}/regenerate-credentials", "/users/{id}/resend-credentials", "/users/{id}/reset-password")
_routes("users:manage", "PATCH", "/users/{id}")
_routes("users:manage", "DELETE", "/users/{id}", "/users/{id}/external-identities/{identity_id}")
_routes("roles:read", "GET", "/roles", "/roles/permissions", "/roles/{id}")
_routes("roles:manage", "POST", "/roles")
_routes("roles:manage", "PATCH", "/roles/{id}")
_routes("roles:manage", "DELETE", "/roles/{id}")
_routes("notifications:read", "GET", "/notifications/status", "/notifications/deliveries")
_routes("destinations:read", "GET", "/delivery/destinations", "/delivery/destinations/{id}")
_routes("destinations:manage", "POST", "/delivery/destinations", "/delivery/destinations/test")
_routes("destinations:manage", "PATCH", "/delivery/destinations/{id}")
_routes("destinations:manage", "DELETE", "/delivery/destinations/{id}")
_routes("destinations:use", "POST", "/delivery/destinations/{id}/test")
_routes("destinations:use", "GET", "/delivery/destinations/{id}/schemas", "/delivery/destinations/{id}/tables", "/delivery/destinations/{id}/table-metadata")
_routes("delivery:read", "GET", "/delivery/configurations", "/delivery/runs", "/delivery/runs/{id}/attempts", "/delivery/runs/{id}/reviews", "/delivery/destinations/{id}/target-policy")
_routes("delivery:configure", "POST", "/delivery/preview", "/delivery/preflight", "/delivery/configurations", "/delivery/configurations/{id}/versions")
_routes("delivery:execute", "POST", "/delivery/runs")
_routes("delivery:repair_evidence", "POST", "/delivery/runs/{id}/repair-evidence")
_routes("delivery:review_unknown", "POST", "/delivery/runs/{id}/reviews")
ENDPOINT_MATRIX = tuple(_MATRIX)
_COMPILED = [(method, re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", path.replace(".", r"\.")) + "$"), permission) for method, path, permission in ENDPOINT_MATRIX]


def required_permission(path: str, method: str) -> str | None:
    if method == "GET" and path in {"/auth/sso/{provider}/start", "/auth/sso/{provider}/callback"}:
        return None  # OpenAPI route templates; concrete anonymous paths remain explicitly enumerated.
    if (method, path) in PUBLIC_ENDPOINTS | SESSION_ENDPOINTS:
        return None
    for verb, pattern, permission in _COMPILED:
        if method == verb and pattern.fullmatch(path):
            return permission
    return UNMAPPED
