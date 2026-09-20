# Validación del ciclo de correcciones 0.3.0

Última verificación: 19 de septiembre de 2026. Aplicación disponible en
**http://localhost:3100**, proyecto Docker `trackvance-certification`.

## Evolución Conexiones

| Verificación | Resultado |
| --- | --- |
| Backend + scripts | 363 aprobadas; Ruff y Mypy (27 módulos) aprobados |
| Frontend | ESLint, TypeScript y build aprobados; Vitest 89/89 en 9 archivos |
| Motores externos reales | PostgreSQL 16 y SQL Server 2022: 62 comprobaciones aprobadas |
| Permisos externos | Usuarios SELECT; escrituras rechazadas en ambos motores |
| Conexión y descubrimiento | Acceso, credenciales/host/puerto inválidos, schemas, tablas, vistas, tipos y preview aprobados |
| Snapshot y módulos | Parquet, ArtifactStore, versiones, linaje, Intake, evidencia y Excel aprobados en ambos motores |
| Resiliencia y secretos | Caída/reconexión, reinicio, config versionada y ausencia de secretos en logs/auditoría/SQL dump aprobados |
| Migraciones PostgreSQL | Upgrade, historial, paridad, downgrade/upgrade y Alembic check aprobados |
| Regresión funcional | Smoke API 84/84; Playwright 15 aprobadas (48,2 s) y acceso limpio opt-in aprobado por separado (1,1 s) |
| Instancia principal | Datos previos preservados, migración 0005 aplicada y cuatro servicios healthy en localhost:3100 |

La evidencia efímera está en
`.codex-local/connections-e2e/final-20260919/result.json`; el runner eliminó
únicamente su proyecto aislado y seis volúmenes. Capturas y dos informes Intake
XLSX (uno por motor) quedaron bajo el subdirectorio `test-results` de esa evidencia.
Backup v2/restore de credenciales aprobado. La instancia principal quedó sin datasets, runs ni conexiones de prueba, con `restart=no`. Avisos no bloqueantes: dos deprecaciones de Starlette/AnyIO y bundle Vite de 500,53 kB; Playwright informa variables de color de terminal concurrentes.

Los siguientes resultados conservan también el ciclo base del 16 de septiembre.

Se leyeron completos los tres PDF obligatorios: especificación v1.1 (52 páginas),
informe integral (10) y pruebas pendientes (5). Se conserva el monolito modular.

## Resultados

| Verificación | Resultado |
| --- | --- |
| Backend pytest | 222 pruebas aprobadas |
| Operaciones pytest | 11 pruebas aprobadas; total pytest 233 |
| Ruff backend/tests/scripts | Aprobado |
| Mypy backend | Aprobado, 22 módulos |
| Frontend ESLint / TypeScript / build | Aprobados |
| Frontend Vitest | 67 pruebas, 8 archivos, aprobadas |
| Playwright en Docker/PostgreSQL | 13 flujos aprobados (40,4 s) |
| Smoke API en Docker | 84 comprobaciones aprobadas |
| Doctor | 7/7 correcto |
| Alembic | Upgrade, downgrade y paridad SQLite/PostgreSQL aprobados |
| Reinicio Docker aislado | 45 datasets, 59 versiones, 37 runs y 173 artifacts idénticos |
| Rebuild de instalación principal | 7 datasets, 12 versiones, 10 runs, 1 excepción y 39 artifacts preservados; cuatro servicios healthy, restart=no |
| XLSX | Tres informes reales; 12 hojas inspeccionadas, formatos/filtros/inyección verificados |

Los tests emiten dos avisos de deprecación de Starlette/AnyIO; no son fallos de
aplicación. GitHub Actions quedó preparado, pero no se ejecutó remotamente.

## No regresiones de negocio

| Escenario | Resultado observado |
| --- | --- |
| Intake 120 filas | 110 válidas, 10 con error, 91,67 %, REJECTED |
| Detalle Intake | REQUIRED 4, UNIQUE 4 registros, POSITIVE 2 |
| Recon 120/110 | MATCH 110, SOURCE_ONLY 6, DUPLICATE_SOURCE 4; resto 0; 91,67 % |
| Sentinel baseline | 110 filas, 9 checks, 0 fallos, 100 % |
| Sentinel v2 | 90 filas, volumen -18,18 %, 4 fallos, 55,56 % |
| Sentinel v3 | 90 filas, source_system ausente, 1 fallo, 88,89 % |
| Excepciones | OPEN → INVESTIGATING → PENDING_VALIDATION → RESOLVED solo después de un run posterior conforme; configuración, runs y timeline conservados. Cierre administrativo separado con motivo obligatorio |
| Auditoría | Eventos originales más export/download con actor estable y request/run |

La discrepancia 44/43 se debía al trim del lector anterior; nuevas cargas
cuentan 44 observaciones. Los perfiles históricos se preservan como legacy.

## Cobertura nueva

- `test_rules_v2.py`: observaciones/Unicode/null/vacío, inferencia ISO,
  identificadores, transforms declarados, reglas ampliadas, claves Recon,
  multi-columna, porcentajes/denominador cero, fechas, agregación, IQR y paridad
  nativa Polars/DuckDB de expresiones por registro, comparación y métricas.
- `test_artifacts_evidence.py`: Parquet aceptado/linaje, hash/rutas/inmutabilidad,
  backfill, identidad/auditoría, manifests v2 y golden del adaptador v1.
- `test_certified_cycle.py`: escenarios certificados completos y export por API.
- `test_exports.py`: estructura, estilos, tipos, tres módulos, sanitización,
  fórmula injection, etiquetas de comparación y contrato OpenAPI.
- `test_v2_api.py`: versionado inmutable/replay, org/permisos de descarga,
  corrupción, líneas CSV físicas y reutilización de configuración Recon.
- `test_planner.py`, `test_migrations.py`, `test_api_worker.py`: preflight,
  migrations, sesiones/CSRF, recuperación de lease, cancelación y concurrencia.
- `test_exception_validation.py`: matriz de validación para Intake, ReconOps y
  Sentinel, protección frente a runs obsoletos, trazabilidad, cierre
  administrativo y lectura de históricos.
- `scripts/tests/test_operations.py`: backup/restore, WAL, hashes y destinos.
- `test_architecture_ports.py`: contratos de storage/fuente/ejecución/cola y
  sustitución de adaptadores. Un provider de prueba con locators opacos
  `test-object://` recorre carga, perfil, esquema, worker, output derivado,
  resultados, manifiesto y XLSX; detectó y corrigió una lectura directa del
  path canónico en el endpoint de perfil.
- `scripts/tests/test_docker_e2e_cycle.py`: protege instalaciones existentes,
  rechaza el nombre del proyecto principal y limita la limpieza a recursos
  nuevos del proyecto aislado, incluso si falla el arranque.
- Componentes y E2E detallados en [validación frontend](frontend-validation.md).

Playwright cubre upload, Intake not_future, VALUE_MISMATCH, excepción con bloqueo,
validación técnica por una ejecución Recon posterior, trazabilidad entre ambos
runs, resolución verificada y cierre administrativo separado,
schema drift Sentinel, download XLSX/status/filename, tres módulos históricos,
vista móvil y publicación/ejecución de tres formularios de reglas.
El ciclo actual añade carga UI de CSV, XLSX con selección de hoja, JSON,
Parquet y TXT con delimitador, además del recorrido de navegación completo.

## Preservación y operación

La migración más reciente es `0005_external_connections`; las migraciones ya
aplicadas conservan su hash.
El snapshot de la SQLite previa conservó todas sus columnas históricas en
10 datasets, 20 versiones, 9 configuraciones, 25 runs, 67 findings,
13 excepciones y 108 eventos. Docker usa PostgreSQL y volúmenes propios;
la instalación SQLite `.local` se mantiene independiente y no fue reemplazada.

El arranque Docker usa rutas de Alembic explícitas en el paquete instalado.
API/worker/PostgreSQL operan en red interna. Nginx puede tener salida en Docker
Desktop; no se certifica bloqueo total de red del host. No hay llamadas cloud
obligatorias. Reinicio sin borrar volúmenes y comparación estricta de huellas
pasaron. Backup/restauración SQLite WAL también pasó en destino nuevo.

El ciclo de arquitectura se ejecutó con
`python scripts/tests/docker_e2e_cycle.py` en proyectos `trackvance-e2e-*`
temporales. Sus volúmenes se eliminaron al terminar. La evidencia final está en
`.codex-local/architecture-e2e/verified-cycle.log` y
`.codex-local/architecture-e2e/trackvance-e2e-architecture-verified/`.
La comparación de la instalación principal está en
`.codex-local/architecture-alignment/before.json` y `after.json`; todas las
huellas coinciden. Las pruebas no agregaron datos a la instalación del usuario.

## Entregables

- [Archivos modificados y nuevos](changed-files.md).
- [Contrato HTTP](../../backend/API_CONTRACT.md) y [OpenAPI](../../backend/openapi.json).
- [Semántica de evidencia](../adr/0002-evidence-and-artifacts.md),
  [reglas](../adr/0003-rule-semantics.md), [catálogo](../rules-catalog.md),
  [Excel](../exports-xlsx.md) y [operación](operations.md).
- `outputs/corrections/`: tres XLSX reales, `examples.json`, previews y screenshots.

## Pendientes explícitos

- PySpark/lane local, paridad completa entre tres motores y benchmarks 250k/1M:
  adaptador aún no implementado; se mantienen 10 MiB/100k y rechazo por presupuesto.
- Timeout duro por proceso y pruebas de caída real de worker/DB bajo carga:
  hay cobertura controlada de leases/cancelación/corrupción, no certificación de estrés.
- Mascarado CONFIDENTIAL/RESTRICTED y permisos por asignación individual:
  esta entrega aplica roles y organización, sin ese modelo de sensibilidad.
- Backup integral automático PostgreSQL+artifacts y fresh clone remoto/CI:
  backup SQLite probado; build limpio Compose probado; no se hizo push ni clone remoto.
- Apertura manual en Excel/LibreOffice: validación con openpyxl y render de las
  doce hojas completada; no se automatizó una sesión nativa de Excel.

Por estos límites no se declara cerrada toda la aceptación técnica de Etapa 1
que exige el PDF de pruebas pendientes. El flujo funcional local corregido sí
está disponible y verificado. Algunas opciones avanzadas de reglas se configuran
por API, según el alcance documentado del editor.

Capturas: [Intake](../../outputs/corrections/screenshots/intake-excel-button.png), [ReconOps](../../outputs/corrections/screenshots/recon-excel-button.png), [Sentinel](../../outputs/corrections/screenshots/sentinel-excel-button.png), [versión derivada](../../outputs/corrections/screenshots/derived-version.png).

Durante la validación se corrigieron un path Alembic instalado en Docker,
roundtrip de metadata vacía en Recon, selectores E2E ambiguos y la selección
inestable del fixture del smoke. En el ciclo de arquitectura se corrigieron
además dos selectores de los nuevos E2E y la dependencia de path del perfil;
las repeticiones finales completas se registran arriba.
