# Revisión de arquitectura 0.4.0 - 19 de septiembre de 2026

## Estado actual del código y de la instalación principal

La revisión 0.4.0 conserva el monolito modular, los contratos HTTP y los puertos
`StorageProvider`, `DatasetSource`, `ExecutionEngine` y `JobQueue`. Mantiene los
cinco lectores de archivo y las fuentes PostgreSQL/SQL Server detrás de
`DatasetSource`; los motores de calidad siguen consumiendo snapshots y no escriben
en bases externas. `SecretStore` mantiene credenciales fuera de metadata y el
worker continúa sin acceso a sus volúmenes.

El grafo de migraciones del código llega a `0007_monitor_scheduling` y define
**21 tablas de aplicación**. La instalación principal sigue en
`0005_external_connections` mientras se prepara el upgrade final; este documento
no la presenta como migrada todavía. Sus datos, proyecto, puerto web 3100 y puertos
internos se conservan. Solo la web permanece publicada en localhost; API y
PostgreSQL continúan dentro de la red Compose.

## Cambios de arquitectura en 0.4.0

| Área | Cambio implementado |
| --- | --- |
| Intake y ReconOps | Semántica avanzada en servicios y motores portables, referencias a versiones inmutables y evidencia por regla/comparación. |
| Sentinel | Scheduler local en el worker, revisiones inmutables, ocurrencias transaccionales, series y alertas internas. |
| Excepciones | API dedicada para asignación, SLA, comentarios y adjuntos publicados por `StorageProvider`; validación técnica compartida con el worker. |
| Identidad | Administración local mediante API, roles base, permisos efectivos, Argon2, revocación de sesiones y protección del último administrador. |
| Operación | Backup/restore coordinado de PostgreSQL, artifacts, secretos y clave; reset con plan exacto; doctor, verificación de linaje y benchmark aislado. |
| Presentación | Conexiones, usuarios, programación y gestión de casos se integran en React conservando el cliente HTTP y el control autoritativo del backend. |

Las migraciones son aditivas y no reescriben revisiones ya aplicadas:

| Migración | Persistencia añadida |
| --- | --- |
| `0005_external_connections` | `external_connections`, `external_connection_versions` y `dataset_source_bindings`. |
| `0006_local_identity_exceptions` | Campos de usuarios/excepciones y `exception_attachments`. |
| `0007_monitor_scheduling` | `monitor_schedules`, `monitor_schedule_versions` y `monitor_occurrences`. |

## Evidencia frontend de 0.4.0

ESLint, TypeScript y build terminaron correctamente; el bundle JavaScript de
535,26 kB (`535.26 kB` en la salida de Vite) produjo una advertencia de tamaño no
bloqueante. Vitest aprobó **109
pruebas en 12 archivos**. El ciclo Playwright `connections-final` aprobó **23
escenarios** y omitió el escenario limpio opt-in; ese escenario se ejecutó y pasó
una vez en un ciclo limpio separado. El total es **24 escenarios distintos
aprobados**. El detalle está en [validación frontend](frontend-validation.md).

## Historial explícito de 0.3.0

### Evolución Conexiones - 19 de septiembre de 2026

Esa revisión implementó PostgreSQL y SQL Server como fuentes de solo lectura
mediante `DatasetSource`; añadió `SecretStore`, configuraciones inmutables, bindings
y snapshots Parquet. Su head era `0005_external_connections` y OpenAPI incorporó
las rutas `/connections` y `/datasets/{id}/refresh-source`. La decisión se conserva
en [ADR 0007](../adr/0007-external-connections.md).

### Revisión de arquitectura base - 16 de septiembre de 2026

La revisión mantuvo el monolito modular, los datos y los contratos HTTP. En ese
corte no se crearon migraciones: el head era `0004_exception_validation`.

### Código y pruebas de aquella revisión

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

### Resultados históricos

- pytest: 233 aprobadas (222 backend y 11 scripts).
- Ruff/Mypy/ESLint/TypeScript/build: aprobados.
- Vitest: 67 aprobadas; Playwright: 13 aprobadas; smoke: 84 comprobaciones.
- PostgreSQL: upgrade histórico, paridad de modelos y roundtrip aprobados.
- Restart aislado: 173 artifacts y registros idénticos.
- Main: 7 datasets, 12 versiones, 10 runs, 1 excepción y 39 artifacts conservados.
- Cuatro servicios saludables en localhost:3100; restart=no.

En aquel ciclo se registraron dos avisos de deprecación de Starlette/AnyIO. Los
adaptadores de producto (PySpark, Redis/Celery, cloud, OIDC e infraestructura
distribuida) quedaron documentados como pendientes, sin dependencias adicionales
en el runtime.
