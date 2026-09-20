# Entrega de Conexiones — 19 de septiembre de 2026

La aplicación queda disponible en http://localhost:3100 (proyecto `trackvance-certification`), con cuatro servicios healthy y `restart=no`. El acceso demo funciona sin seed. La base principal conserva cero datasets, versiones, runs y conexiones de prueba. Los proyectos temporales de certificación y sus volúmenes fueron retirados.

## Arquitectura y comportamiento

`DatasetSource` separa adquisición y reglas. PostgreSQLDatasetSource y SQLServerDatasetSource entregan la representación normalizada existente; Intake, ReconOps y Sentinel procesan snapshots Parquet mediante StorageProvider. La BD interna solo almacena metadata. Cada adquisición conserva conexión/configuración versionada, schema, tabla/vista, hashes, captura y linaje; refresh crea otra DatasetVersion.

Conexiones permite crear, probar, editar, deshabilitar, retirar, explorar schemas/objetos, revisar columnas/preview y registrar datasets. No hay escritura externa ni SQL arbitrario. Los lectores CSV/XLSX/JSON/Parquet/TXT y módulos existentes permanecen compatibles.

SecretStore cifra credenciales localmente y deja preparado un proveedor gestionado. Solo la API monta los volúmenes de credenciales y clave; el worker consume snapshots. RBAC y organización se verifican en backend, los errores son sanitizados y pruebas/preview/adquisiciones quedan auditadas. Cambiar destino/usuario/TLS exige contraseña explícita. DELETE requiere versión vigente; los snapshots siguen disponibles al retirar la conexión.

## Migración y API

- Migración aditiva: `backend/migrations/versions/0005_external_connections.py`; no se modificaron migraciones aplicadas.
- Tablas: `external_connections`, `external_connection_versions`, `dataset_source_bindings`.
- `/api/v1/connections`: listado/creación; detalle/edición/baja con `?version=`; prueba borrador y guardada; schemas, objetos, preview y registro de datasets.
- `/api/v1/datasets/{id}/refresh-source`: snapshot nuevo de la fuente.
- `backend/openapi.json`: 56 paths, respuestas públicas tipadas de Conexiones, sesión/CSRF y permisos requeridos. Contrato detallado en `backend/API_CONTRACT.md`.
- Corrección de permisos: cerrar una excepción técnica o administrativamente exige `exceptions:close`; UI y backend coinciden.
- Backup SQLite v2 copia/verifica/restaura metadata, artifacts, credenciales cifradas y clave; schema v1 sigue legible cuando contiene todo lo requerido.

## Validación ejecutada

| Comprobación | Resultado |
| --- | --- |
| pytest backend + operaciones | 363 aprobadas |
| Ruff | Aprobado |
| Mypy | Aprobado, 27 módulos |
| ESLint / TypeScript | Aprobados |
| Vitest | 89 aprobadas, 9 archivos |
| Build frontend | Aprobado |
| PostgreSQL real | Conexión, permisos, descubrimiento, preview, snapshot, Intake, linaje, reconexión y reinicio: PASS |
| SQL Server real | Los mismos escenarios con SQL Server 2022: PASS |
| Conectores reales | 62 comprobaciones aprobadas |
| Smoke API | 84 comprobaciones aprobadas, incluye los cinco formatos existentes |
| Playwright | 15 flujos aprobados en stack aislado; acceso limpio opt-in aprobado por separado. Total 16 |
| Migraciones PostgreSQL | Upgrade/downgrade, historia, paridad ORM y Alembic check aprobados |
| Doctor local | 7/7; cuatro servicios healthy |
| Limpieza | Sin contenedores/volúmenes E2E y sin datos sintéticos en la instancia principal |

Evidencia real: `.codex-local/connections-e2e/final-20260919/result.json`. Las capturas y Excel de cada motor están en `outputs/connections/`. Se reconstruyó la web final después del ajuste del enlace histórico a conexiones retiradas; su regresión está cubierta por Vitest. La prueba de acceso limpio pasó sobre esa imagen final.

No hay pruebas fallidas pendientes. Avisos no bloqueantes: dos deprecaciones Starlette/AnyIO, bundle Vite de 500,53 kB (151,44 kB gzip) y variables de color de terminal concurrentes en Playwright. El workflow incluye `connections-e2e`; esta entrega certifica la ejecución local y no afirma una nueva ejecución remota de GitHub Actions. Los cambios permanecen locales, sin commit/push en este ciclo.

## Límites y evolución posterior

- Snapshots locales completos acotados: 100.000 filas, 100 columnas, 64 MiB y 64 KiB por celda; preview hasta 100 filas. Sin truncamiento silencioso. La adquisición masiva mediante jobs queda para otra evolución.
- Adaptadores futuros: S3, Azure Blob, APIs, Oracle, MySQL, Snowflake y Databricks.
- Proveedores externos de SecretStore, rotación automática de clave y backup PostgreSQL integral automatizado quedan preparados/documentados, no implementados.
- Destinos/Data Delivery será un módulo independiente de DatasetSource; esta entrega es solo lectura.

## Archivos nuevos o modificados

Inventario del árbol de trabajo frente al commit actual. Incluye los ajustes previos de acceso demo sin seed que ya estaban pendientes al continuar.

- `.dockerignore`
- `.env.example`
- `.github/workflows/ci.yml`
- `.gitignore`
- `CHANGELOG.md`
- `README.md`
- `backend/API_CONTRACT.md`
- `backend/Dockerfile`
- `backend/migrations/versions/0005_external_connections.py`
- `backend/openapi.json`
- `backend/pyproject.toml`
- `backend/src/trackvance/api.py`
- `backend/src/trackvance/audit_context.py`
- `backend/src/trackvance/config.py`
- `backend/src/trackvance/connections_api.py`
- `backend/src/trackvance/connections_schemas.py`
- `backend/src/trackvance/connections_service.py`
- `backend/src/trackvance/credential_store.py`
- `backend/src/trackvance/dataset_sources.py`
- `backend/src/trackvance/models.py`
- `backend/src/trackvance/permissions.py`
- `backend/src/trackvance/seed.py`
- `backend/src/trackvance/services.py`
- `backend/tests/conftest.py`
- `backend/tests/test_connection_test_audit.py`
- `backend/tests/test_connections_api.py`
- `backend/tests/test_connections_contract.py`
- `backend/tests/test_credential_store.py`
- `backend/tests/test_dataset_sources.py`
- `backend/tests/test_demo_modes.py`
- `backend/tests/test_exception_validation.py`
- `backend/uv.lock`
- `compose.yml`
- `deploy/docker/compose.connections-test.yml`
- `docs/adr/0001-local-prototype.md`
- `docs/adr/0006-architecture-ports.md`
- `docs/adr/0007-external-connections.md`
- `docs/architecture.md`
- `docs/development/architecture-changes.md`
- `docs/development/changed-files.md`
- `docs/development/connections-delivery.md`
- `docs/development/operations.md`
- `docs/development/prototype-scope.md`
- `docs/development/validation.md`
- `docs/specification/README.md`
- `docs/specification/Trackvance_Core_Especificacion_Tecnica_v1.1.md`
- `docs/specification/Trackvance_Core_Especificacion_Tecnica_v1.1.pdf`
- `docs/specification/validation_results_0.3.0.json`
- `frontend/src/app/App.tsx`
- `frontend/src/components/ui.tsx`
- `frontend/src/features/connections/Connections.test.tsx`
- `frontend/src/features/connections/Connections.tsx`
- `frontend/src/features/connections/SourceRefresh.tsx`
- `frontend/src/features/connections/connections.css`
- `frontend/src/features/datasets/Datasets.tsx`
- `frontend/src/features/datasets/VersionIdentity.test.tsx`
- `frontend/src/features/datasets/VersionIdentity.tsx`
- `frontend/src/routes/Operations.test.tsx`
- `frontend/src/routes/Operations.tsx`
- `frontend/src/test/render.tsx`
- `frontend/tests-e2e/connections.spec.ts`
- `frontend/tests-e2e/demo-access-clean.spec.ts`
- `scripts/backup_local.py`
- `scripts/start-local.ps1`
- `scripts/tests/connections_cycle.py`
- `scripts/tests/docker_e2e_cycle.py`
- `scripts/tests/fixtures/connections-postgresql.sql`
- `scripts/tests/fixtures/connections-sqlserver.sql`
- `scripts/tests/test_connections_cycle.py`
- `scripts/tests/test_docker_e2e_cycle.py`
- `scripts/tests/test_operations.py`

Fuera del repositorio también se actualizaron los archivos oficiales en `ProductOne/Documentación`: especificación Markdown/PDF, `validation_results_0.3.0.json` y `build_specification.py`. El PDF final tiene 27 páginas, 24 marcadores y copia idéntica en `docs/specification/`.
