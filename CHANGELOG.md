# Changelog

## 0.2.0 — 2026-09-14

- Corrige profiling observado, fechas ISO e identificadores con ceros iniciales.
- Publica outputs Intake en Parquet con artifacts y linaje inmutable.
- Actualiza manifests a schema 2 y auditoría con actores estructurados.
- Añade normalización explícita y reglas ampliadas de Intake, ReconOps y Sentinel.
- Sustituye la exportación principal CSV por informes Excel estructurados y seguros.
- Añade Alembic `0002_evidence_v2`, PostgreSQL/Docker, respaldo y pruebas integrales.
- Conserva lectura de manifests v1, configuraciones legacy y export CSV deprecated.

Los límites pendientes de PySpark, gran volumen, privacidad avanzada y backup
automatizado de PostgreSQL se documentan en `docs/development/validation.md`.
