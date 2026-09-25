# Informe de cierre A–M: Trackvance Core 0.5.1

Corte: 25 de septiembre de 2026, America/Bogota. Informe de implementación y evidencias reales de este ciclo; **CI del código aprobado y PDF publicado tras revisión completa**. No hereda PASS de 0.5.0. La instalación
habitual `trackvance-certification` en `localhost:3100` **no fue actualizada** ni utilizada como entorno destructivo.

## A. Versión y commits

Producto: **0.5.1**, backend/frontend consistentes, rama `feat/local-prototype`. Baseline: `e7838c3c86b2117605a889f23242e75d9b09577d` (0.5.0). Código final probado: `8927ea00703ff090708c302d7755dae20a7687b9`.

| Commit | Cambio del ciclo |
| --- | --- |
| `4451cb6c8e6ce71510cf60a4a46c2aad36eb9868` | Ocho correcciones, revisión operacional y métricas 0.5.1. |
| `ff525cee368c9befe9e0b34b562286c4534796d9` | Modos ejecutables Linux y procedencia del runtime legacy. |
| `705a1f07ae54a837be48d07f131455b12d4b448d` | Conservación de linaje Delivery y huellas nativas de backup. |
| `8927ea00703ff090708c302d7755dae20a7687b9` | Espera acotada de evidencia tras commit durable, sin replay. |

El commit documental que contiene este informe se identifica en Git y en la entrega final, después de verificar también sus ocho jobs. No se inserta su propio SHA dentro del PDF; no sustituye el SHA del código probado.

## B. Ocho correcciones: antes, después y archivos

### 1. Reparación de evidencia

Antes: `PENDING_REPAIR` conservaba el commit, pero no tenía reparación operativa. Después: validación local estricta de identidades, hashes y métricas; reconstrucción idempotente de receipt/manifest, sin
DataSink, secretos ni transacción remota. Archivos: `backend/src/trackvance/delivery_service.py`, `delivery_api.py`, `delivery_schemas.py`; `frontend/src/features/delivery/DeliveryOperations.tsx`.

### 2. Revisión de UNKNOWN

Antes: incertidumbre bien representada, sin historial operacional estructurado. Después: observaciones append-only con actor, fecha, nota y resultado externo; Run e intento conservan UNKNOWN. No se crea Job/Run
ni replay al revisar. Archivos: `backend/src/trackvance/models.py`, `delivery_service.py`, `worker.py`, `backend/migrations/versions/0009_delivery_reviews.py`, `DeliveryOperations.tsx`.

### 3. Semántica de métricas

Antes: rows_written podía confundirse con población física; null parecía cero. Después: semántica aditiva compartida en DTO/receipt/manifest, N/D explícito, ayuda funcional y tiempos monotónicos de
preflight/escritura en Runs nuevos. Archivos: `backend/src/trackvance/delivery_metrics.py`, `data_sinks.py`, `delivery_service.py`; `frontend/src/features/delivery/DeliveryRunDetail.tsx`.

### 4. Conteos UPSERT PostgreSQL

Antes: desglose insert/update no disponible de forma general. Después: PostgreSQL 18+ usa RETURNING OLD documentado dentro de la transacción; 16/17 conserva null en UPSERT con actualizaciones; sólo-claves DO
NOTHING y vacío tienen conteos conocidos. No se usa preconsulta, xmax ni estadísticas. Archivos: `backend/src/trackvance/data_sinks.py`, `backend/tests/test_data_sinks.py`,
`scripts/tests/delivery_metrics_probe.py`, `scripts/tests/delivery_cycle.py`.

### 5. Regresión temporal

Antes: política conservadora correcta, con cobertura parcial del recorrido. Después: matrices reales PG/SQL Server, refresh/historia y consumo por Intake/Recon/Sentinel/Delivery; STRING temporal no se convierte
en TIMESTAMP. SQL Server estilo 127 normaliza offset a UTC y conserva el séptimo decimal textual. Archivos: `backend/tests/test_dataset_sources.py`, `test_data_sinks.py`, `scripts/tests/connections_cycle.py` y
fixtures; política de producto preservada.

### 6. Linaje canónico

Antes: documentación confundía relación y tipo destino. Después: `DELIVERED_TO` apunta a `DELIVERY_DESTINATION_VERSION`; no se renombra historia. El backfill legacy excluye Delivery y no añade RUN_INPUT
genéricos. Archivos: `backend/src/trackvance/services.py`, tests de contrato/operaciones, `scripts/verify_storage.py`, ADR 0015 y especificación.

### 7. Benchmark Delivery

Antes: benchmark general sin matriz propia de escritura remota. Después: runner separado, dos motores reales, cuatro estrategias, fases, throughput, recursos, watchdog, verificación SQL y limpieza de proyectos
propios. Archivos: `scripts/tests/delivery_benchmark_cycle.py`, `scripts/tests/test_delivery_benchmark_cycle.py`, `deploy/docker/compose.delivery-benchmark.yml`, `.github/workflows/ci.yml`.

### 8. Carga diferida

Antes: entry de 611.407 bytes y warning Vite >500 kB. Después: React.lazy/Suspense por rutas, shell continuo, error recuperable, detalle Delivery separado y espera de publicación de evidencia acotada. Archivos:
`frontend/src/app/App.tsx`, `RouteContent.tsx`, `features/runs/Runs.tsx`, `features/delivery/DeliveryRunDetail.tsx` y pruebas unitarias/navegador. Nombres Python abreviados: backend/src/trackvance (o
backend/tests para tests). Componentes Delivery abreviados: frontend/src/features/delivery; rutas: frontend/src.

## C. Migración y compatibilidad

Única nueva migración: **0009_delivery_reviews**, sobre 0008_data_delivery. Crea `delivery_reviews`: organización, Run, intento, usuario/nombre snapshot, outcome, nota, verified_at y created_at; FKs, índices y
CHECK de tres outcomes. Se necesita historial consultable, no una frase aislada en AuditEvent. 0001–0008 permanecen sin cambios; no se reescriben intentos, métricas o artifacts. Alembic check/paridad y
roundtrip reales PASS: 24 tablas y ocho enlaces históricos preservados. Restauraciones auténticas 0.4.1 y 0.5.0 llegan a state 4/0009; el ensayo 0.5.0 conserva DTOs y bytes de receipt/manifest sin metadata
retroactiva.

## D. API

Se conservan contratos existentes; base `/api/v1`, dos paths y tres operaciones nuevas.

| Operación | Permiso y resultado |
| --- | --- |
| POST `/delivery/runs/{run_id}/repair-evidence` | `runs:execute`, CSRF; 200 REPAIRED/ALREADY_VALID e IDs receipt/manifest. |
| GET `/delivery/runs/{run_id}/reviews` | `runs:read`; colección items/total. |
| POST `/delivery/runs/{run_id}/reviews` | `runs:execute`, CSRF; 201 revisión; body delivery_attempt_id, outcome, note y verified_at opcional. |

Scope ajeno/inexistente: 404; reparación no verificable o estado no permitido: 409; revisión con cuerpo/fecha inválidos: 422. Fecha con zona, no futura ni anterior al intento; si se omite, reloj del servidor.
Nota 1–4.000 caracteres tras trim. OpenAPI 0.5.1: 83 paths, 71 schemas, 16 paths Delivery; igualdad con runtime probada. Receipt schema 1 y manifest schema 2 mantienen lectura de evidencia 0.5.0.

## E. UI

- COMMITTED + PENDING_REPAIR: aviso de entrega confirmada y **Reparar evidencia**.
- UNKNOWN: **Revisar resultado**, advertencia sin replay e historial externo con actor y ambas fechas.
- Acciones operativas sólo con `runs:execute`; lectura no adquiere permisos de escritura.
- Filas preparadas/enviadas, insertadas/actualizadas y ayuda; null es N/D, cero sólo si conocido.
- SUCCESS/COMMITTED sin receipt: descargas bloqueadas y GET cada 1,5 s hasta 60 s;
  no cancela GET lento ni reinicia plazo por rerender. Al vencer, **Actualizar estado**.
- Lazy routes mantienen navegación/sesión, loader accesible, deep links y error con recarga.

La UI no sustituye autorización backend ni declara confirmación remota por una revisión humana.

## F. Estados, métricas y linaje

No se añaden ni reinterpretan estados históricos de DeliveryAttempt. STARTED puede finalizar COMMITTED/FAILED/UNKNOWN; UNKNOWN no cambia al revisarse. Run SUCCESS/COMMITTED precede normalmente a publicación
local. PENDING_REPAIR es una marca de evidencia, no un nuevo intento ni permiso de repetir escritura.

| Campo | Significado |
| --- | --- |
| rows_attempted | Filas fuente preparadas. |
| rows_written | Filas enviadas en operación confirmada; no COUNT(*) final remoto. |
| rows_inserted / rows_updated | Acciones fiables del adaptador o null; no censo de efectos de triggers/rules. |
| bytes_sent | UTF-8 preparado de valores no null; no bytes de red/TLS. |
| preflight_seconds / write_seconds | Reloj monotónico; escritura incluye deliver_prepared/commit, no publicación local. |

`metric_semantics` versión 1 es aditiva; históricos ausentes no se rellenan con cero. Revisión: REMOTE_COMMIT_OBSERVED, REMOTE_NOT_COMMITTED_OBSERVED o INCONCLUSIVE. Audita
DELIVERY_EVIDENCE_REPAIR_STARTED, DELIVERY_EVIDENCE_REPAIRED, DELIVERY_EVIDENCE_REPAIR_FAILED y DELIVERY_UNKNOWN_REVIEWED.
Grafo: DatasetVersion —DELIVERY_INPUT→ Run —DELIVERED_TO→ DeliveryDestinationVersion; Run —DELIVERY_RECEIPT→ Artifact —EVIDENCE_OF→ DeliveryAttempt; RUN_OUTPUT enlaza evidencia.

## G. Benchmark Delivery

Fixtures CSV SHAKE256 deterministas, cuatro columnas STRING/DECIMAL/DATE/STRING; importe DECIMAL(18,0), payload distinto por fila. Motores PG16 y SQL Server 2022. Smoke: 1.074.923 bytes / 1.000 filas, 8/8 PASS,
wall 143,59 s. Representativo: 8.917.809 bytes / 20.000 filas, 8/8 PASS, wall 207,70 s. Cada tier ejecuta CREATE_AND_LOAD → APPEND → OVERWRITE → UPSERT mixto; SQL independiente verifica filas, claves, suma de
importes y longitudes de payload.

| Representativo | Preflight HTTP / worker s | Escritura s | Total caso s | Filas/s escritura | MB fuente/s |
| --- | ---: | ---: | ---: | ---: | ---: |
| PG CREATE_AND_LOAD | 0,246 / 0,270 | 0,2082 | 1,624 | 96.071 | 42,837 |
| PG APPEND | 0,511 / 0,464 | 0,1945 | 2,586 | 102.807 | 45,841 |
| PG OVERWRITE | 0,496 / 0,472 | 0,2191 | 2,631 | 91.273 | 40,698 |
| PG UPSERT | 0,539 / 0,514 | 0,6329 | 3,252 | 31.599 | 14,090 |
| SQL Server CREATE_AND_LOAD | 0,280 / 0,287 | 7,1269 | 9,340 | 2.806 | 1,251 |
| SQL Server APPEND | 0,584 / 0,463 | 7,1833 | 9,303 | 2.784 | 1,241 |
| SQL Server OVERWRITE | 0,479 / 0,481 | 7,2688 | 9,778 | 2.751 | 1,227 |
| SQL Server UPSERT | 0,497 / 0,505 | 31,7338 | 34,458 | 630 | 0,281 |

Orden anterior, smoke escritura PG: 0,0255/0,0168/0,0247/0,0392 s; SQL Server: 0,4034/0,4097/0,4727/1,7187 s. UPSERT SQL desglosa 500/500 y 10.000/10.000 por tier; PG16 mantiene null/null, aunque la
verificación SQL apruebe. Representativo: pico agregado 2.050.125.461 bytes, CPU máxima 213,85 % de núcleos, Block I/O acumulado máximo 848.686.889 bytes, sin OOM/corte observado. Storage compartido
15.325.905→15.407.176 bytes; temporal máximo muestreado cero, no garantía de ausencia de picos breves. Pico cgroup es acumulado; I/O incluye fondo. Una ejecución por combinación, sin percentiles. Total incluye
cola/polling/evidencia; MB/s usa bytes del CSV, no red. Los tiempos locales preceden al arreglo de espera: no se atribuyen a una nueva corrida; el smoke CI posterior es evidencia independiente. 100/500 MiB y
1/2/5 GiB: NOT_RUN_RESOURCE_LIMIT por límites 10 MiB/100.000 filas, sin elevarlos. Primer smoke falló por mapping del fixture antes de escribir (0/8); se corrigió y repitió. Los tres proyectos limpiaron siete
contenedores/ocho volúmenes/una red cada uno. Método, hashes, CPU/I/O por caso y JSON: [benchmark Delivery](delivery-benchmark-results-0.5.1.md).

## H. Bundle frontend

| Medida | Baseline | Final 0.5.1 |
| --- | ---: | ---: |
| Entry JS bytes | 611.407 | 364.966 |
| Entry gzip bytes | 179.471 | 116.768 |
| Reducción entry | — | 40,31 % raw / 34,94 % gzip |
| Chunks finales | — | 19 JavaScript / 4 CSS |
| Warning >500 kB | Sí | No, sin elevar umbral |

Node 22.14.0/Vite 7.3.6, 1.718 módulos, build final 2,15 s; detalle Delivery 18.331 bytes/5.820 gzip. Medición inicial posterior al splitting: 116.764 gzip, antes del arreglo de evidencia; queda preservada
junto al resultado final. No equivale a mejora medida de latencia/Core Web Vitals ni suma de todas las rutas. Inventario completo: [frontend-bundle.json](evidence/0.5.1/frontend-bundle.json).

## I. Pruebas ejecutadas

| Suite/gate local | Resultado real |
| --- | --- |
| Pytest backend + scripts final | 1.091 PASS, 65,97 s, dos deprecaciones Starlette/AnyIO. |
| Ruff / Mypy | PASS / PASS, 39 archivos fuente. |
| Vitest final | 154 PASS / 17 archivos, 10,96 s; incluye seis regresiones de publicación tardía. |
| ESLint / TypeScript / producción | PASS / PASS / PASS. |
| Playwright preview final | 7 PASS, 13,6 s; API completamente interceptada, no SQL real. |
| Compose local | Smoke 84 PASS; navegador 24 PASS/6 opt-in omitidos; 206 artifacts/1.605 relaciones exactos tras restart. |
| Conexiones reales | 96 checks, smoke 84 PASS, navegador 28 PASS/2 opt-in omitidos; matriz temporal 14 PG/18 SQL Server. |
| Delivery real | 114 checks, incluidos 22 PG16/18 de conteos; navegador focal 1/1; restart/secretos PASS. |
| Recovery/migraciones | 9 artifacts, 2 secretos, 164 relaciones, 2 intentos/1 revisión; reparación concurrente y post-restore sin replay. |
| Compatibilidad auténtica | 0.4.1: 7 artifacts/1 secreto; 0.5.0: 9 artifacts/2 secretos; proyección/bytes exactos PASS. |
| Benchmark general / Delivery | Smoke general PASS 39,4225 s; Delivery smoke y representativo 8/8 cada uno. |

La reparación concurrente produjo REPAIRED/ALREADY_VALID; cuatro filas remotas permanecieron invariantes. UNKNOWN del recovery es SIMULATED_UNKNOWN_NO_REMOTE_IO. Los ciclos locales Compose/Conexiones/Delivery
preceden al último arreglo de UI; no certifican sus dos nuevos casos. Éstos pasan en preview final y CI se separa abajo. No se suman suites como escenarios únicos; skips y fallos previos quedan registrados.
Evidencias: [índice saneado 0.5.1](evidence/0.5.1/README.md) y [validación](validation.md).

## J. GitHub Actions — código final aprobado

Workflow `Trackvance local checks`, archivo `.github/workflows/ci.yml`. [Corrida 36194431770](https://github.com/eddiesan422/TrackvanceCore/actions/runs/36194431770), commit
`8927ea00703ff090708c302d7755dae20a7687b9`. Resultado final **SUCCESS, ocho de ocho jobs**, observado el 25 de septiembre de 2026 a las 22:07:31 UTC.

| Job | Resultado observado |
| --- | --- |
| backend | SUCCESS |
| frontend | SUCCESS |
| delivery-e2e | SUCCESS |
| backup-restore-e2e | SUCCESS |
| benchmark-smoke | SUCCESS |
| delivery-benchmark-smoke | SUCCESS |
| compose-e2e | SUCCESS |
| connections-e2e | SUCCESS |

CI: 1.091 pytest/62,78 s; 154 Vitest/17 archivos/14,50 s. Compose 26 PASS/6 skips, además clean-demo 1 PASS/2,4 s; Conexiones 96 checks y 30 PASS/2 skips; Delivery 114 checks y 1 navegador/7,1 s; recovery 9
artifacts/2 secretos/164 enlaces/1 review; benchmark Delivery 8/8. Son **32 escenarios Playwright únicos**, deduplicados por archivo/título parametrizado/línea: siete interceptados y otros 25; no se suman jobs.
Resultados verificables: [ci.json](evidence/0.5.1/ci.json). Los tres workflows previos conservan sus fallos en `ci-attempts.json`; no se heredaron sus éxitos parciales. El commit posterior de publicación
documental se identifica y verifica por separado.

## K. Documentación

Actualizados README, CHANGELOG, arquitectura, roadmap, prototype-scope, validación, operación, ADR 0015, API_CONTRACT y OpenAPI. ADR 0015 incorpora la decisión de review estructurada; no se crea un ADR
redundante. Nuevos informes de benchmark/splitting y evidencia 0.5.1 distinguen mediciones, límites y procedencia. La especificación amplía Delivery A–Z y demás módulos; sección 24 explica las ocho
correcciones. Generador portable y validation_results_0.5.1.json son entradas del PDF. Fuentes: `docs/specification/Trackvance_Core_Especificacion_Tecnica_v1.1.md`, `build_specification.py` y README de esa
carpeta; el JSON final se consolida con la corrida aprobada.

## L. PDF publicado y verificado

**52 páginas, 111 marcadores y 27 secciones principales.** Se renderizaron y revisaron visualmente todas las páginas antes de `build_specification.py --publish`: portada, índice, diagramas, tablas, JSON, márgenes, footers y saltos. Sin cortes ni solapamientos observados.
Tras corregir un redondeo en la página 31, se verificaron 51 PNG idénticos y se inspeccionó otra vez la página modificada. Los 52 PNG publicados son idénticos al candidato aprobado. Fuente y PDF conservan v1.1; software 0.5.1.

SHA-256 publicado: `c69609f26e117b60ea4fe77d893879a064d2c0c242ca1bd8941d9274e34e2813`.
Fuente, generador, resultados, PDF y extraído coinciden con la copia oficial de Documentación. La edición 0.5.0 quedó archivada; evidencia: [pdf-verification.json](evidence/0.5.1/pdf-verification.json).

## M. Límites y pendientes fuera de 0.5.1

- UNKNOWN físico por pérdida exacta de confirmación: NOT_RUN_NONDETERMINISTIC; revisión humana no demuestra commit.
- Tiers Delivery fuera del límite: NOT_RUN_RESOURCE_LIMIT; sin promesa de capacidad productiva o exactly-once universal.
- PG16/17 update-capable conserva null; conteos no incluyen censo de efectos indirectos de triggers/rules.
- Sin conectores nuevos, SHIST, scheduler de Delivery, email, SSO ni RBAC granular por destino/estrategia.
- Sin 2PC, replay automático de UNKNOWN, transforms de negocio Delivery, cloud ni productización.
- Persisten límites de carga/recursos; memoria/temporal muestreados no representan máximos universales.
- La instalación principal 3100 sigue sin actualizar: requiere su procedimiento operativo con backup y rebuild.
- Para crear un backup sobre una instalación aún en 0007 se usan sus herramientas archivadas; el CLI actual exige seis volúmenes y dos workers. Restore actual sí acepta su backup verificado; sobre 0008 el tooling nuevo fue comparado exactamente con el antiguo.
- Ningún skip o NOT_RUN se convierte en PASS; las limitaciones anteriores siguen explícitas después del cierre de los gates ejecutados.
