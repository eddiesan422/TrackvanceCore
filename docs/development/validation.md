# Validación de Trackvance Core 0.4.1

Corte incremental del 21 de septiembre de 2026 (America/Bogota). La revisión
0.4.1 cambia la experiencia de configuración y conserva contratos, datos y
semántica backend. Ningún éxito se hereda de la entrega anterior.

| Comprobación 0.4.1 | Resultado ejecutado |
| --- | --- |
| pytest backend y scripts | 533 aprobadas en 37,36 s; dos avisos de deprecación. |
| Ruff | PASS. |
| Mypy | PASS: 33 archivos fuente. |
| ESLint / TypeScript | PASS. |
| Vitest | 119 aprobadas en 13 archivos. Focales 16/16 y 34/34 aprobados durante el desarrollo. |
| Build frontend | PASS; bundle JavaScript principal 550,78 kB / 164,59 kB gzip, con advertencia informativa de tamaño. |
| Playwright con fuentes reales | 23 aprobadas y 1 opt-in omitida; 62 comprobaciones PostgreSQL/SQL Server, credenciales/logs y restart: PASS. |
| Playwright instalación limpia | 1 aprobada: acceso sin seed, logout, redirección a `/` e inicio visible. Total: 24 escenarios distintos aprobados. |
| Compose integral | 19 aprobadas y cinco opt-in cubiertas por otros ciclos; doctor 7/7, smoke 84/84, migraciones y persistencia tras restart: PASS. |
| GitHub Actions 0.4.1 | **PENDIENTE DEL WORKFLOW DEL COMMIT DE ENTREGA.** |

OpenAPI, paquetes y lock declaran 0.4.1. Esta revisión no crea endpoints, DTOs ni
migraciones; la API y Alembic permanecen compatibles. La cobertura incremental
incluye identificadores desde esquema, catálogo de responsables, transforms y
normalización de claves con preview, selectores Sentinel y logout con regreso al
inicio.

## Base certificada de 0.4.0

Certificación local del 19 de septiembre de 2026 (America/Bogota). Los resultados
de 0.3.0 quedan en [el informe histórico](validation-history-0.3.0.md).
El [informe de entrega](roadmap-0.4.0-delivery.md) resume el alcance funcional.

## Calidad y regresión

| Comprobación | Resultado ejecutado |
| --- | --- |
| pytest backend y scripts | 533 aprobadas en 41,66 s; incluye regresiones de restore y linaje. |
| Ruff | PASS: `uv run ruff check src tests ../scripts`. |
| Mypy | PASS: 33 archivos fuente, check-untyped-defs e ignore-missing-imports. |
| ESLint / TypeScript | PASS. |
| Vitest | 109 aprobadas en 12 archivos. |
| Build | PASS; JS 535,26 kB, 160,00 kB gzip. |
| PostgreSQL / Alembic | Upgrade histórico, paridad ORM, check y downgrade/base/upgrade temporal: PASS hasta 0007. |
| API smoke / doctor | 84 comprobaciones API y 7 comprobaciones doctor: PASS. |
| PostgreSQL y SQL Server externos | 62 comprobaciones reales: PASS. |
| Playwright con ambas fuentes | 23 aprobadas; un opt-in de instalación limpia omitido y aprobado por separado. |
| Playwright instalación limpia | 1 aprobada; siete colecciones vacías y acceso sin demo seed. Total: 24 escenarios distintos cubiertos. |
| Compose integral | 19 escenarios aprobados; cinco opt-in cubiertos en los otros ciclos. Restart, hashes y 21 tablas: PASS. |
| Backup/restore | PASS: 7 artifacts, 1 credencial, 124 relaciones; caso/adjunto, programación y 31 métricas recuperados tras destruir el origen. |
| Benchmark | PASS: 106.194.531 bytes, 50.000 filas y cuatro columnas, ambos motores y tres módulos. Smoke CI de 1 MiB: PASS, no certificante. |
| GitHub Actions del tag v0.4.0 | [PASS: seis jobs](https://github.com/eddiesan422/TrackvanceCore/actions/runs/35489379287) sobre 69d48a0f; backend, frontend, Compose, conexiones, backup/restore y benchmark smoke. Historial completo y repetición de rama descritos abajo. |

Avisos no bloqueantes: dos deprecaciones de Starlette/AnyIO y advertencia Vite
por chunk mayor de 500 kB. No se ocultan ni se deshabilitan controles.

## Evidencia versionada y reproducción

- [Conexiones reales](evidence/0.4.0/connections.json): credenciales y host/puerto inválidos, permisos insuficientes, cuenta SELECT sin escritura, schemas/tablas/vistas, tipos, preview, snapshots, linaje, Intake/Excel, refresh, caída/reconexión y ausencia de secretos.
- [Compose](evidence/0.4.0/compose.json) y [persistencia](evidence/0.4.0/persistence-summary.json): 56 datasets, 70 versiones, 43 runs, 206 artifacts, 1.562 relaciones y un adjunto íntegros tras restart; incluye tablas de programación.
- [Backup/restore](evidence/0.4.0/recovery.json): PostgreSQL externo reconectado con secreto restaurado; nuevo snapshot e Intake APPROVED, original REJECTED conservado, caso validado sin autocierre e historial Sentinel intacto.
- [Instalación limpia](evidence/0.4.0/clean-demo.json): acceso sin sembrar datos.
- [Benchmark 100 MiB](evidence/0.4.0/benchmark-100mib.json), [smoke](evidence/0.4.0/benchmark-smoke.json) y [análisis de volumen](volume-benchmark.md): recursos, fixture, tiempos y motivos de detención.

Los JSON publicados contienen resultados e identidades de fixtures, no passwords,
credenciales cifradas, claves ni dumps SQL. Los proyectos aislados se eliminan al
terminar; los backups sensibles quedan fuera de Git y de los artifacts de CI.

```powershell
# Desde backend
uv run pytest tests ../scripts/tests
uv run ruff check src tests ../scripts
uv run mypy src/trackvance --check-untyped-defs --ignore-missing-imports
# Desde frontend
pnpm lint
pnpm typecheck
pnpm test
pnpm build
# Desde la raíz; cada runner protege y limpia sólo su proyecto aislado
python scripts/tests/docker_e2e_cycle.py
python scripts/tests/connections_cycle.py --full-playwright
python scripts/tests/docker_backup_cycle.py
python scripts/tests/benchmark_cycle.py
```

El job backend valida Alembic directamente sobre PostgreSQL temporal. Los jobs de
integración repiten doctor, API, navegador, persistencia, fuentes, recuperación y
smoke de volumen. El benchmark completo no se sustituye por el smoke pequeño de CI.

## Cobertura nueva

- Intake: condiciones limitadas, unicidad compuesta, longitud, tipos, comparación de columnas y referencia inmutable; identidad de regla y métricas de población aplicable. Cero evaluadas no demuestra corrección.
- ReconOps: N:1/1:N, SUM/COUNT por columna, transforms ordenados, null policies, comparación múltiple y rechazo de campos agregados ambiguos.
- Sentinel: scheduler real del worker, revisión/cursor concurrentes, coalescencia, errores aislados, ocurrencia y manifest SYSTEM, histórico separado por método y alertas navegables.
- Excepciones: asignación/SLA/prioridad/comentarios/adjuntos, descarga autorizada, reapertura sin reutilizar evidencia previa, CAS UI/worker, cierre administrativo con motivo nuevo y autoresolución opt-in con prueba suficiente.
- Usuarios: CRUD local, cinco roles con accesos permitidos/denegados, reset, sesiones revocadas, último administrador y aislamiento por organización.
- Operación: hash/traversal/corrupción/relaciones, propiedad de recursos antes de limpiar, reset con inventario/confirmación, backup integral y restore a proyecto nuevo.

Se mantienen las regresiones de cinco formatos, perfiles observados, identificadores,
overrides de esquema, Todos, ordenamiento, Centro de Control, versiones inmutables,
manifests compatibles, Excel seguro y separación de fallo técnico/hallazgo.
Los escenarios históricos siguen cubiertos: Intake 110/120 válido, Recon MATCH 110 /
SOURCE_ONLY 6 / DUPLICATE_SOURCE 4 y Sentinel 100 / 55,56 / 88,89 %.

## Incidencias y límites reales

Las repeticiones corrigieron un selector de fecha ambiguo, una edición de excepción
perdida durante refresco, la evidencia insuficiente de reglas legacy y el tipo
EXCEPTION ausente en el verificador de linaje. El primer CI 0.4.0 detectó además
PermissionError de backup en Linux: se reemplazó el bind escribible por streaming
sin elevar permisos. La repetición remota aprobó todos los jobs. Los fallos del harness de volumen
(413 del proxy y timeout fuera del contrato) se documentan en el análisis de volumen.

La publicación del tag v0.4.0 aprobó sus seis jobs. En la ejecución simultánea de
la rama, el escenario PostgreSQL de `roadmap-source-cycle.spec.ts` llegó a la
siguiente edición antes de que React Query mostrara la nueva revisión del caso.
Se corrigió exclusivamente el test: espera el PATCH 200, la versión visible y el
selector habilitado. No cambian producto, timeouts ni retries. Las ejecuciones
originales se conservan en [el historial CI](evidence/0.4.0/ci.json), incluido el
fallo; los commits posteriores y sus resultados están en [los workflows de la
rama](https://github.com/eddiesan422/TrackvanceCore/actions/workflows/ci.yml?query=branch%3Afeat%2Flocal-prototype).
La repetición local con el test corregido aprobó los 23 escenarios Playwright,
62 comprobaciones de conectores, 84 de API y persistencia tras restart. El opt-in
de instalación limpia se mantiene certificado por su ejecución separada. Se
repitieron también las 109 pruebas frontend, ESLint y TypeScript: PASS.

500 MiB, 1 GiB, 2 GiB y 5 GiB quedaron NOT_RUN_RESOURCE_LIMIT: no se generaron
ni ejecutaron por exceder el presupuesto. El máximo certificado corresponde al
fixture y overrides documentados, no a cualquier forma de datos ni concurrencia.
Se mantienen los defaults conservadores del prototipo; no se implementa PySpark.

La [instalación principal](evidence/0.4.0/main-upgrade.json) se actualizó a 0.4.0 y
0007 después de un backup integral verificado. Sus registros previos se conservan;
cuatro servicios healthy, diez comprobaciones doctor de recuperación aprobadas y
restart=no. No se insertaron datos de prueba. Productización y Data Delivery
permanecen explícitamente fuera de este ciclo.
