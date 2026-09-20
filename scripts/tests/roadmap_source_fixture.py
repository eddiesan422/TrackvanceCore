"""Administrative fixture writes, restricted to disposable connector certification projects.

This helper is test infrastructure; DatasetSource remains read-only. No credential
is supplied on command lines or returned in errors. Fixed SQL never accepts identifiers.
"""

import argparse
import json
import re
import subprocess


def fixture(project: str, engine: str, action: str) -> None:
    if not re.fullmatch(r"trackvance-connections-e2e-[a-z0-9-]+", project):
        raise ValueError("Las escrituras de fixtures requieren un proyecto efímero de conexiones.")
    if engine not in {"POSTGRESQL", "SQLSERVER"} or action not in {"setup", "correct"}:
        raise ValueError("Operación de fixture no admitida.")
    service = "source-postgres" if engine == "POSTGRESQL" else "source-sqlserver"
    containers = subprocess.run([
        "docker", "ps", "-q", "--filter", f"label=com.docker.compose.project={project}",
        "--filter", f"label=com.docker.compose.service={service}",
    ], capture_output=True, text=True, check=False)
    ids = containers.stdout.split()
    if containers.returncode or len(ids) != 1:
        raise ValueError("No se encontró un único motor de pruebas en el proyecto solicitado.")
    if action == "correct":
        sql = "UPDATE source_data.roadmap_transactions SET department = 'Bogota' WHERE record_id = '002';"
    else:
        sql = """
CREATE TABLE source_data.roadmap_transactions (
  record_id varchar(12) NOT NULL, country varchar(2) NOT NULL,
  department varchar(60), amount decimal(12,2) NOT NULL
);
INSERT INTO source_data.roadmap_transactions VALUES
('001', 'CO', 'Antioquia', 10.50), ('002', 'CO', NULL, 20.00), ('003', 'US', NULL, 30.25);
GRANT SELECT ON source_data.roadmap_transactions TO tv_reader;
"""
    if engine == "POSTGRESQL":
        arguments = ["psql", "-U", "source_admin", "-d", "trackvance_source", "-v", "ON_ERROR_STOP=1"]
    else:
        arguments = ["sh", "-c", 'SQLCMDPASSWORD="$MSSQL_SA_PASSWORD" /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -d trackvance_source -C -b -f 65001']
    result = subprocess.run(["docker", "exec", "-i", ids[0], *arguments], input=sql,
                            capture_output=True, text=True, encoding="utf-8", check=False)
    if result.returncode:
        raise RuntimeError("No se pudo preparar/corregir la fuente efímera de certificación.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--engine", required=True, choices=["POSTGRESQL", "SQLSERVER"])
    parser.add_argument("--action", required=True, choices=["setup", "correct"])
    args = parser.parse_args()
    fixture(args.project, args.engine, args.action)
    print(json.dumps({"status": "PASS", "engine": args.engine, "action": args.action}))
