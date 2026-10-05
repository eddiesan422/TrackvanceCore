"""Regenerate HTTP, permissions and model documentation without connecting to DB."""

import json
from pathlib import Path

from trackvance import __version__
from trackvance.api import app
from trackvance.db import Base
from trackvance.permissions import ENDPOINT_MATRIX, catalog_dto

ROOT = Path(__file__).resolve().parents[1]
HERE = ROOT / "docs" / "specification"
NEW_TABLES = {
    "macro_domains", "data_domains", "governance_history", "glossary_terms",
    "column_documentation", "glossary_associations", "dataset_blocks",
    "dataset_security_dependencies", "strict_approvals", "report_definitions",
    "report_revisions", "report_contexts", "report_executions",
}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    save(ROOT / "backend" / "openapi.json", app.openapi())
    permissions = {"version": __version__, "catalog": catalog_dto(),
                   "routes": [{"method": method, "path": path, "permission": permission}
                              for method, path, permission in sorted(ENDPOINT_MATRIX)]}
    save(HERE / f"permission_contract_{__version__}.json", permissions)
    inventory = {"version": __version__, "tables": sorted(Base.metadata.tables), "entities": []}
    for name in sorted(Base.metadata.tables):
        table = Base.metadata.tables[name]
        inventory["entities"].append({
            "table": name,
            "evolution": ("NEW" if name in NEW_TABLES else "MODIFIED" if name in
                          {"datasets", "jobs"} else "PRESERVED"),
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
    save(HERE / f"model_contract_{__version__}.json", inventory)
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
    (ROOT / "docs" / "development" / "permission-matrix.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"version": __version__, "paths": len(app.openapi()["paths"]),
                      "tables": len(inventory["tables"]), "permissions": len(permissions["catalog"])}))


if __name__ == "__main__":
    main()
