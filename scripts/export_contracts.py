"""Export runtime contracts to explicit docs or ignored CI scratch, without DB I/O."""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NEW_TABLES = {
    "document_engine_settings",
}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def docs_destination(value: Path | None) -> Path:
    root = ROOT.resolve()
    target = (value if value is not None else root / ".codex-local/ci/generated-contracts/docs").resolve()
    if target.is_relative_to(root) and not target.is_relative_to(root / ".codex-local"):
        raise ValueError("Documentation must use its official checkout or ignored .codex-local scratch, not product paths")
    return target


def check_baselines(directory: Path, version: str, contracts: dict) -> None:
    for kind, value in contracts.items():
        baseline = directory / f"{kind}_{version}.json"
        if json.loads(baseline.read_text(encoding="utf-8-sig")) != value:
            raise ValueError(f"Runtime {kind} differs from its reviewed machine contract fixture")


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docs-root", type=Path, help="Official docs checkout/docs, or temporary generation directory")
    parser.add_argument("--check-baselines", type=Path, help="Compare generated JSON with reviewed machine fixtures without updating them")
    args = parser.parse_args(argv)
    try:
        docs = docs_destination(args.docs_root)
    except ValueError as error:
        parser.error(str(error))
    # Reject an invalid destination before importing the application or writing.
    from trackvance import __version__
    from trackvance.api import app
    from trackvance.db import Base
    from trackvance.permissions import ENDPOINT_MATRIX, catalog_dto

    specification = docs / "specification"
    permissions = {"version": __version__, "catalog": catalog_dto(),
                   "routes": [{"method": method, "path": path, "permission": permission}
                              for method, path, permission in sorted(ENDPOINT_MATRIX)],
                   "shared_read_routes": [{"method": "GET", "path": "/operations",
                       "resource_permissions": ["datasets:read", "intake:read", "recon:read", "sentinel:read", "delivery:read", "documents:read"],
                       "authorization": "RESOURCE_AND_CONTENT_BEFORE_FILTER_COUNT_AND_PAGINATION",
                       "cases_require": "exceptions:read", "personal_news_require": "notifications:read"}]}
    inventory = {"version": __version__, "tables": sorted(Base.metadata.tables), "entities": []}
    for name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[name]
        inventory["entities"].append({
            "table": name,
            "evolution": ("NEW" if name in NEW_TABLES else "MODIFIED" if name in
                          {"document_originals", "document_references"} else "PRESERVED"),
            "columns": [{"name": c.name, "type": str(c.type), "nullable": c.nullable,
                         "primary_key": c.primary_key,
                         "references": sorted(str(f.target_fullname) for f in c.foreign_keys)}
                        for c in table.columns],
            "constraints": sorted(str(constraint.sqltext) if hasattr(constraint, "sqltext") else
                                  type(constraint).__name__ + "(" + ",".join(c.name for c in constraint.columns) + ")"
                                  for constraint in table.constraints),
            "indexes": sorted([{"name": index.name, "unique": index.unique,
                              "columns": [c.name for c in index.columns]}
                             for index in table.indexes], key=lambda x: x["name"]),
        })
    if args.check_baselines is not None:
        check_baselines(args.check_baselines, __version__,
                        {"model_contract": inventory, "permission_contract": permissions})
    save(ROOT / "backend" / "openapi.json", app.openapi())
    save(specification / f"permission_contract_{__version__}.json", permissions)
    save(specification / f"model_contract_{__version__}.json", inventory)
    lines = [f"# Matriz de permisos HTTP y catálogo {__version__}", "",
             ("Generada por `scripts/export_contracts.py` desde la autoridad del runtime. "
             "Base `/api/v1`; organización, propietario, CSRF, primer acceso, estado y "
             "autorización de estrategia se aplican además del permiso de ruta."), "",
             (f"El catálogo contiene {len(permissions['catalog'])} códigos; "
             f"la matriz tiene {len(permissions['routes'])} entradas protegidas."), "",
             ("Administrator resuelve el catálogo completo, conserva protección y no puede "
             "consultar bandejas/preflights personales ajenos. Los defaults ampliados de "
             "organizaciones nuevas no reescriben roles personalizados existentes."), "",
             "## Catálogo y dependencias", "", "| Código | Grupo | Dependencias directas | Delegable |",
             "|---|---|---|---|"]
    for entry in permissions["catalog"]:
        deps = ", ".join(entry["dependencies"]) or "—"
        lines.append(f"| `{entry['code']}` | {entry['group']} | {deps} | {'Sí' if entry['delegable'] else 'No'} |")
    lines.extend(["", "## Rutas protegidas", "", "| Método | Ruta | Permiso |", "|---|---|---|"])
    for entry in permissions["routes"]:
        lines.append(f"| {entry['method']} | `{entry['path']}` | `{entry['permission']}` |")
    lines.extend(["", ("La inbox aplica usuario destinatario y permisos actuales del módulo "
                  "a lista, contador y lectura. El responsable de programación se revalida "
                  "al despachar y antes de STARTED; SYSTEM no es una cuenta ejecutora."), ""])
    lines.extend(["## Consulta operativa compartida", "",
        ("GET /operations permite consulta por los permisos existentes de Datasets, Intake, ReconOps, "
        "Sentinel, Delivery o Documentos, autorizando cada recurso/contenido antes de filtrar, contar y paginar. "
        "La alternativa de consulta no otorga Excepciones: sus casos, enlaces y conteos requieren exceptions:read. "
        "Las novedades personales conservan notifications:read y la gestión de casos sus permisos históricos. "
        "Los filtros dataset_id/version_id se autorizan antes de consultar usos directos de la versión."), ""])
    development = docs / "development"
    development.mkdir(parents=True, exist_ok=True)
    (development / "permission-matrix.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"version": __version__, "paths": len(app.openapi()["paths"]),
                      "tables": len(inventory["tables"]), "permissions": len(permissions["catalog"]),
                      "docs_root": str(docs), "machine_baselines_checked": args.check_baselines is not None}))


if __name__ == "__main__":
    main()
