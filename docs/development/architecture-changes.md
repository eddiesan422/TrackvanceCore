# Revisión de arquitectura 0.3.0 - 16 de septiembre de 2026

La revisión mantiene el monolito modular, los datos y los contratos HTTP.
No crea migraciones nuevas: el head continúa en `0004_exception_validation`.
El archivo OpenAPI mantiene sus contratos; la especificación técnica enumera
las rutas generadas reales.

## Código y pruebas de esta revisión

| Archivo | Cambio |
| --- | --- |
| `backend/src/trackvance/artifactstore.py` | Puerto StorageProvider; publicación inmutable y materialización/staging sin exponer el layout local. |
| `backend/src/trackvance/dataset_readers.py` | DatasetSource y LocalFileDatasetSource sobre los cinco lectores existentes. |
| `backend/src/trackvance/execution.py` | ExecutionEngine y LocalExecutionEngine. |
| `backend/src/trackvance/jobqueue.py` | JobQueue y DatabaseJobQueue. |
| `backend/src/trackvance/api.py` | Materialización para perfil/esquema/download/evidencia, staging por provider y limpieza de uploads fallidos. |
| `backend/src/trackvance/services.py` | Versiones, resultados, outputs y manifests publicados por el puerto; lectura mediante materialización. |
| `backend/src/trackvance/worker.py` | Delegación a ExecutionEngine y sustitución controlada en pruebas. |
| `backend/tests/test_architecture_ports.py` | Contratos, sustitución de fuente/cola/ejecución y ciclo con locators opacos. |
| `frontend/tests-e2e/multiformat-navigation.spec.ts` | Cinco formatos y navegación completa. |
| `scripts/smoke_test.py` | 84 comprobaciones; fixtures propios y resolución técnica de excepciones. |
| `scripts/tests/docker_e2e_cycle.py` | Stack aislado, doctor, migraciones, smoke, E2E, restart/hash y limpieza segura. |
| `scripts/tests/test_docker_e2e_cycle.py` | Seis regresiones de aislamiento y protección de recursos. |
| `scripts/tests/fixtures/multiformat.xlsx.base64`, `multiformat.parquet.base64` | Fixtures sintéticos reproducibles de formatos binarios. |
| `scripts/check_postgres_migrations.py`, `scripts/verify_storage.py` | Orden de imports para Ruff. |
| `.gitignore`, `.dockerignore` | Exclusión de nuevos outputs/temporales y archivos locales del contexto de build. |

Se actualizaron README, CHANGELOG, docs/architecture.md, ADR 0004/0006,
operación, alcance y resultados de validación. La especificación oficial en
`ProductOne/Documentación` se reemplazó por una revisión de 26 páginas con
fuente editable y generador reproducible; la edición original se archivó.

## Resultados

- pytest: 233 aprobadas (222 backend y 11 scripts).
- Ruff/Mypy/ESLint/TypeScript/build: aprobados.
- Vitest: 67 aprobadas; Playwright: 13 aprobadas; smoke: 84 comprobaciones.
- PostgreSQL: upgrade histórico, paridad de modelos y roundtrip aprobados.
- Restart aislado: 173 artifacts y registros idénticos.
- Main: 7 datasets, 12 versiones, 10 runs, 1 excepción y 39 artifacts conservados.
- Cuatro servicios saludables en localhost:3100; restart=no.

Dos avisos de deprecación de Starlette/AnyIO permanecen. Los adaptadores de
producto (PySpark, Redis/Celery, cloud, OIDC e infraestructura distribuida)
están documentados como pendientes, sin dependencias adicionales en el runtime.
