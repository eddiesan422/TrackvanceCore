"""Central permission policy for local roles; unknown roles fail closed."""

READ = frozenset({"datasets:read", "runs:read", "exceptions:read", "rules:read", "connections:read"})
EXPORT = frozenset({"exports:download", "artifacts:download"})
AUTHOR = frozenset({"datasets:write", "configurations:write", "runs:execute", "exceptions:write"})
ROLE_PERMISSIONS = {
    "Administrator": READ | EXPORT | AUTHOR | {"audit:read", "users:read", "users:write", "system:read", "exceptions:close", "connections:manage", "connections:use"},
    "Data Owner": READ | EXPORT | AUTHOR | {"audit:read", "exceptions:close", "connections:manage", "connections:use"},
    "Data Owner / Lead": READ | EXPORT | AUTHOR | {"audit:read", "exceptions:close", "connections:manage", "connections:use"},
    "Data Analyst": READ | EXPORT | AUTHOR | {"audit:read", "connections:use"},
    "Operations": READ | EXPORT | {"exceptions:write"},
    "Auditor": READ | EXPORT | {"audit:read", "users:read", "system:read"},
}


def permissions_for(role: str) -> list[str]:
    return sorted(ROLE_PERMISSIONS.get(role, frozenset()))


def required_permission(path: str, method: str) -> str | None:
    if path.startswith(("/auth/", "/me", "/health")):
        return None
    if path.endswith("/refresh-source"):
        return "connections:use"
    if path.startswith("/connections"):
        if path == "/connections/test":
            return "connections:manage"
        if path.endswith(("/schemas", "/objects", "/preview", "/datasets", "/test")):
            return "connections:use"
        return "connections:read" if method == "GET" else "connections:manage"
    if path.endswith(("/export.xlsx", "/export.csv")):
        return "exports:download"
    if path.startswith("/artifacts/") or path.endswith("/evidence"):
        return "artifacts:download"
    if path.startswith("/audit-events"):
        return "audit:read"
    if path.startswith("/users"):
        return "users:read" if method == "GET" else "users:write"
    if path.startswith(("/system/", "/jobs/")):
        return "system:read"
    if path.startswith("/exceptions") or "/exceptions" in path:
        return "exceptions:read" if method == "GET" else "exceptions:write"
    if path.startswith(("/datasets", "/dataset-versions")):
        return "datasets:read" if method == "GET" else "datasets:write"
    if method != "GET" and (path.endswith(("/runs", "/cancel")) or path.startswith("/execution-plans")):
        return "runs:execute"
    if method != "GET" and path.startswith(("/intake/contracts", "/recon/controls", "/monitors")):
        return "configurations:write"
    return "runs:read"
