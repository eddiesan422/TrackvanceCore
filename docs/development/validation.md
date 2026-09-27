# Validación de Trackvance Core 0.6.0

Baseline real de repositorio `4519ed354202ea8f220682758da234e07b6df3ed`, sin
commits posteriores al iniciar. Rama `feat/local-prototype`. Producto
`979cc0f01d0e1ec694886a6f9764a3f856e93315`, harness final
`35c88fa2f0fd0c59dacda7faf76713e064364d25`. La instalación real detectada tenía
0.5.0/0008 y se actualizó conservando sus volúmenes; esa diferencia se distingue
de la baseline de código 0.5.1.

Los resultados siguientes corresponden a ejecuciones nuevas. Los conteos de
navegador se deduplican por escenario; los opt-in omitidos en una suite se
comprueban en su proyecto dedicado. No se suman pruebas repetidas ni se afirma
certificación externa de Microsoft/Google.

| Gate 0.6.0 | Resultado observado |
| --- | --- |
| Corte y procedencia | 26 septiembre 2026, rama feat/local-prototype; baseline 4519ed354202ea8f220682758da234e07b6df3ed. Implementación 979cc0f y corrección de harness 35c88fa2f0fd0c59dacda7faf76713e064364d25. Ejecuciones nuevas 0.6.0; no se heredan resultados 0.5.1. |
| Backend local | 1.141 pytest backend/scripts PASS en 79,57 s, 0 FAIL, 0 SKIP tras la corrección CI. Dos avisos upstream Starlette/httpx y AnyIO. Ruff PASS, Mypy: 42 archivos PASS, uv sync --frozen PASS. Se repitieron 20 pruebas focales del runner tras la corrección. |
| Frontend local | Instalación pnpm congelada, ESLint, TypeScript y production build PASS. 164 Vitest / 19 archivos PASS en 10,46 s. Cinco capturas sintéticas inspeccionadas sin credenciales. |
| OpenAPI y permisos | Runtime 0.6.0: 95 paths, 116 operaciones, 87 schemas; snapshot exacto probado. Catálogo: 41 códigos y 104 reglas protegidas, desconocidas denegadas. Nueva cadena 0010/0011/0012; 0001..0009 sin diferencias. |
| Migraciones PostgreSQL | PostgreSQL 16 real: upgrade/check/downgrade/upgrade PASS, paridad ORM, 24 tablas históricas 0008 y 8 vínculos conservados. SQLite poblada con FK reales también comprobada. |
| Compose integral | PASS: smoke API 84; Playwright 26 PASS / 15 opt-in SKIP / 0 FAIL en 117,646 s. Doctor, migraciones, política restart=no y snapshot exacto tras restart PASS. Volúmenes desechables propios retirados. |
| Conexiones reales | PASS: 96 comprobaciones PostgreSQL / SQL Server; smoke 84; Playwright 30 PASS / 11 opt-in SKIP / 0 FAIL en 159,572 s. Matriz temporal, adquisición/refresh/historia y módulos; secretos y persistencia tras reinicio PASS. |
| Identity/SSO | 9/9 Playwright PASS en 42,930 s con el verificador por stdin; total 112,142 s. Local temporal/primer acceso, roles dinámicos y cuatro perfiles OIDC simulados RS256. Expiración, SMTP FAILED/regeneración, ausencia de plaintext DB/log, restart y cleanup PASS. La recreación API no se observó en esta corrida local. CI: 9/9 en 54,267 s; recreación API observada y verificación por stdin PASS. |
| Clean demo | 1/1 Playwright PASS con Compose base, seed=false y demoaccess=true; colecciones vacías comprobadas. Proyecto nuevo eliminado, 0 recursos restantes. |
| Cobertura de navegador | 41 escenarios distintos cubiertos: 26 comunes + 4 conexiones + 9 identidad + 1 Delivery + 1 demo limpio. Las repeticiones de casos comunes no se suman como tests únicos. Los SKIP son opt-in y se ejecutan por separado. |
| Delivery real | 308 comprobaciones PASS en PostgreSQL 16/18 y SQL Server, audit columns/estrategias/policy/drift/identidad interna. Playwright 1/1 PASS en 6,816 s, scan de credenciales/reinicio PASS. Nueva ejecución final con reporte saneado. |
| UNKNOWN y límites | PASS para pérdida de acknowledgement inyectada en adapter después de commit SQL real, política required y recuperación deliberada sin replay. Fallo físico de red en ventana exacta: NOT_RUN_NONDETERMINISTIC. No se equiparan ambos escenarios. |
| Recovery nativo | PASS: 9 artifacts, 2 secretos SQL y 269 relaciones exactas, 6 roles / 95 grants, 1 ExternalIdentity y 1 OIDC attempt consumido de fixture, 1 notificación y 1 target policy. Origen desechable destruido antes de restore; no reenvío SQL. |
| Restore0.5.1 | PASS desde build auténtico 4519ed3 / 0009 / state 4 hasta 0012 / state 5. 78 artifacts y proyección histórica SHA-256 idéntica; 140,886 s. Restore incluye 5 roles y 1 usuario migrado. |
| Restore0.4.1/0.5.0 | Nuevos drills PASS de backups auténticos 0007/state 2 y 0008/state 3; 7/9 artifacts y 1/2 secretos respectivamente. DTOs y bytes receipt/manifest/Parquet históricos intactos. Instalación principal y backup original no alterados por drills. |
| Benchmarks acotados | General file-only PASS: 1.074.923 bytes / 1.000 filas / 4 columnas en 38,665 s, pico 390.311.443 bytes, sin OOM. Delivery smoke 8/8 PASS mismo input en 148,814 s; versión observada 0.6.0. Sin certificación de capacidad ni percentiles. |
| Proveedores externos | Microsoft personal/Entra y Google Gmail/Workspace: NOT_RUN_EXTERNAL_CREDENTIALS. SMTP externo también NOT_RUN; Mailpit real PASS. Guías de registro/consentimiento/variables y validación separadas; secretos no suministrados ni subidos. |
| Instalación Docker real | PASS: trackvance-certification, http://localhost:3100. Origen detectado 0.5.0/0008; backup nuevo PASS, upgrade 0.6.0/0012, proyección legacy-v3 exacta y restart state5 exacto. API y ambos workers 0.6.0; cinco servicios saludables, restart=no; doctor, login, footer y paneles PASS. Conservados 3 datasets, 1 usuario, 1 conexión, 1 destino, 4 Runs, 9 artifacts y 2 secretos SQL; 5 roles migrados. Sin mutaciones de negocio. |
| GitHub Actions producto | SUCCESS: nueve jobs, commit 35c88fa2f0fd0c59dacda7faf76713e064364d25, workflow 36289364326, intento 1. Backend, frontend, compose-e2e, connections-e2e, delivery-e2e, identity-sso-e2e, backup-restore-e2e, benchmark-smoke y delivery-benchmark-smoke. https://github.com/eddiesan422/TrackvanceCore/actions/runs/36289364326. Primer workflow de 979cc0f: 8 SUCCESS / 1 FAIL del verificador post-restart; corregido con commit nuevo, sin rerun cosmético. |
| GitHub Actions publicación | El commit documental se somete de nuevo a los nueve jobs. El informe de entrega identifica su HEAD SHA, workflow inmutable, resultados y duración después de observar SUCCESS. El PDF no incorpora el SHA de su propio commit para evitar una referencia circular. |
| Publicación PDF | Candidato renderizado e inspeccionado antes de --publish. Páginas, SHA-256, hashes de fuente/OpenAPI/validación, source commit e inspección completa se registran en pdf-verification.json. La edición oficial en Documentación se sincroniza después de revisar y conserva 0.5.1 archivada. |

Evidencia verificable en [evidence/0.6.0](evidence/0.6.0/README.md), [matriz de
aceptación de los 36 criterios](acceptance-0.6.0.md), [permisos](permission-matrix.md)
y [revisión de seguridad](security-review-0.6.0.md). Las guías de [SSO](sso-setup.md)
y [SMTP](smtp-setup.md) documentan configuración real y pruebas opt-in.

## Fallos encontrados y correcciones

- Primer barrido Python: 9 fallos/1.099 aprobados por fixtures de identidad
  heredados, aislamiento de roles, snapshot OpenAPI y mocks de recovery. El
  segundo quedó en 1 fallo/1.134 aprobados: un test de locking simulaba User sin
  los campos requeridos por la autorización dinámica. Se corrigieron fixtures,
  nunca se relajó la autoridad del backend. La suite final pasó completa.
- La revisión independiente corrigió la revocación durante first-login, la
  reasignación errónea tras renombrar un Role, Secure detrás del proxy HTTPS, la
  carrera del email en primer vínculo SSO y el permiso de lectura del receipt.
  Cada corrección tiene regresión permanente.
- La migración SQLite poblada descubrió un default no preservado de sesión y
  restricciones FK durante batch rebuild. Se corrigió la frontera de migración
  y se probó upgrade/downgrade/upgrade con FK reales e historia intacta.
- Playwright Chromium empaquetado de este Windows falló antes de abrir con
  `spawn UNKNOWN`; las ejecuciones locales usan Chrome instalado. CI usa
  Chromium. Dos iteraciones de identidad corrigieron selectores ambiguos y una
  espera que aceptaba también la pantalla restringida; no se contaron como PASS.
- Primer Compose: 25 PASS/15 SKIP/1 FAIL. El reenvío después de editar usaba una
  revisión obsoleta; el modal ahora espera la actualización de la consulta. Se
  añadió una regresión de componente y la suite completa se repitió.
- Un reporte del benchmark conservaba la etiqueta fija 0.5.1. El runner ahora
  obtiene la versión de `/health` y se repitieron sus ocho casos reales; el JSON
  final declara 0.6.0. No se maquilló la evidencia anterior.
- El último Ruff encontró el orden de imports de la nueva prueba de privacidad;
  se corrigió, se repitió Ruff completo y las dos pruebas afectadas. Sin cambios
  funcionales después de la suite completa posterior de 1.141 en 79,57 s.

- El primer workflow de producto tuvo ocho jobs SUCCESS y un FAIL en identity:
  nueve flujos de navegador pasaron, pero la instantánea posterior al reinicio
  terminó con exit2. El stderr no se conservaba; la pérdida del archivo temporal
  por recreación se consideró una inferencia, no una causa demostrada. El runner
  ahora envía el verificador por stdin en cada instantánea, incluye etapa/exitcode
  saneados y tiene una regresión que simula un contenedor sin ese archivo. El
  ciclo local volvió a pasar y el commit nuevo completó los nueve jobs de CI.

Se mantienen dos advertencias upstream: Starlette recomienda migrar de httpx a
httpx2 para TestClient; AnyIO depreca el alias BlockingPortal. No afectan al
resultado actual, pero no se ocultan. No se incorporó una actualización amplia
sin relación con este release para suprimirlas.

## Límites y procedencia

El mock RS256 certifica nuestro flujo Code/PKCE/OIDC, no los proveedores reales.
Los cuatro casos externos y SMTP externo quedan NOT_RUN_EXTERNAL_CREDENTIALS.
La inyección de acknowledgement perdido tras commit SQL demuestra UNKNOWN;
la ventana física exacta de red permanece NOT_RUN_NONDETERMINISTIC.

Los benchmarks smoke de 1.000 filas no certifican 100/500 MiB ni 1/2/5 GiB, percentiles,
concurrencia o capacidad productiva. Los límites por defecto 10 MiB / 100.000 filas /
100 columnas no aumentaron. El drill nativo de recovery valida UI HTTP 200 y declara
browser NOT_RUN_IN_THIS_DRILL; el navegador completo se certifica separadamente.

El PDF conserva nombre técnico v1.1 y cambia IMPLEMENTACIÓN a 0.6.0. El commit de
producto y el HEAD documental se verifican por separado en Actions. Para evitar
una referencia circular, la procedencia del PDF usa el commit de producto más
hashes de fuente/OpenAPI/validación; el informe final identifica el SHA y workflow
del HEAD publicado.

## Antecedentes conservados

Lo siguiente es histórico. No certifica 0.6.0 ni sustituye los resultados anteriores.

# Validación de Trackvance Core 0.5.1

Corte de trabajo: 25 de septiembre de 2026, America/Bogota. Baseline
`e7838c3c86b2117605a889f23242e75d9b09577d`, rama `feat/local-prototype`.
No se heredan éxitos 0.5.0. El código final `8927ea0` completó los ocho jobs de CI.
La publicación documental posterior se verifica también por CI antes de entregar;
se distingue del commit de código para evitar referencias circulares en el PDF.

| Gate 0.5.1 | Evidencia ejecutada hasta este corte |
| --- | --- |
| Python backend + scripts | Pasada completa sobre 8927ea0: 1.091 PASS en 65,97 s; dos avisos Starlette/AnyIO existentes. Incluye las esperas de publicación de evidencia y sus regresiones. |
| Ruff / Mypy | PASS; Mypy 39 archivos fuente. |
| Frontend | ESLint, TypeScript, build PASS. 154 Vitest/17 archivos PASS en 10,96 s. |
| Bundle | 611.407→364.966 bytes iniciales; gzip 179.471→116.768; 19 JS / 4 CSS; advertencia >500 kB eliminada. |
| Playwright interceptado | 7/7 PASS en 13,6 s sobre build real de preview; API simulada, no certifica SQL. Incluye receipt tardío y PENDING_REPAIR tardío. |
| Conexiones reales | 96 comprobaciones PASS, smoke 84 PASS, Playwright 28 PASS / 2 opt-in omitidos; matriz temporal PG 14 columnas / SQL Server 18 y tránsito completo por módulos. |
| Delivery real | 114 comprobaciones PASS incluidas 22 pruebas de métricas PostgreSQL 16/18; navegador focal 1/1 PASS, reinicio y búsqueda de secretos PASS. |
| Migraciones / recovery | PASS real: 0008→0009→0008 con 24 tablas históricas y ocho enlaces preservados. Backup nativo recuperó 9 artifacts, 2 secretos, 164 relaciones, 2 intentos y 1 revisión; reparación concurrente y post-restore sin reenvío (4 filas remotas invariantes). |
| Compose integral local | PASS: doctor, migraciones, smoke API 84/84 (10,9 s), Playwright 24 PASS / 6 opt-in omitidos (2,0 min); 206 artifacts / 1.605 relaciones y huella exacta tras restart, state 4 / 0009. Este ciclo precede los dos escenarios UI de publicación tardía; el CI del código final se informa separadamente. |
| Benchmark general smoke | PASS acotado: 1.074.923 bytes / 1.000 filas / cuatro columnas, 39,4225 s; memoria agregada pico 379.479.652 bytes, sin OOM ni corte. No certifica volumen. |
| Benchmark Delivery | Smoke real 8/8 PASS: 1.074.923 bytes / 1.000 filas / 143,59 s. Representativo 8/8 PASS: 8.917.809 bytes / 20.000 filas / 207,70 s. Primer intento falló por mapping del fixture y quedó conservado, no contado como éxito. |
| Compatibilidad 0.4.x/0.5.0 | PASS nuevo: backup auténtico 0.4.1 state 2/0007, 7 artifacts/1 secreto; runtime auténtico 0.5.0 state 3/0008, 9 artifacts/2 secretos. Ambos llegan a state 4/0009 con proyección exacta; DTOs y bytes históricos Delivery intactos. Tooling nuevo sobre 0008 produce exactamente la misma huella y cinco inventarios de volúmenes que el antiguo. |
| GitHub Actions | SUCCESS: ocho jobs sobre 8927ea0, [workflow 36194431770](https://github.com/eddiesan422/TrackvanceCore/actions/runs/36194431770). Pytest 1.091/62,78 s; Vitest 154/17 archivos. Cada resultado está en [ci.json](evidence/0.5.1/ci.json). |
| Playwright del código final en CI | Compose 26 PASS/6 opt-in omitidos, más clean-demo 1/1; Conexiones 30 PASS/2 omitidos; Delivery 1/1. Deduplicación de archivo/título parametrizado: 32 escenarios distintos con PASS, siete de API interceptada y 25 restantes. Los SKIP no se suman como éxito. |
| PDF | Publicado tras inspección visual de las 52 páginas; 111 marcadores/27 secciones. Hash idéntico al candidato revisado y 52 PNG publicados idénticos: [verificación](evidence/0.5.1/pdf-verification.json). |

Evidencia nueva en [evidence/0.5.1](evidence/0.5.1). No se suben backups, claves,
contraseñas, CSV de población ni volcados de base como evidencia pública.

## Cobertura y límites nuevos 0.5.1

Reparación local valida COMMITTED, hashes/identidades/métricas, no toca DataSink
ni SecretStore, es idempotente y conserva evidencia 0.5.0. UNKNOWN mantiene Run e
intento y anexa revisión independiente. La única migración nueva es 0009; los
hashes de 0001..0008 se contrastan contra la baseline. OpenAPI runtime 0.5.1 tiene
83 paths / 71 schemas, incluidos 16 paths Delivery; un test compara el JSON con runtime.

Los conteos PostgreSQL 18 provienen de RETURNING OLD/NEW dentro de la transacción.
PostgreSQL 16 conserva N/D cuando el desglose no es fiable. La matriz temporal
descubrió una expectativa incorrecta del harness: SQL Server estilo 127 normaliza
datetimeoffset(7) a UTC. Se corrigió la expectativa y se mantuvo la política
0.5.0, conservando el séptimo dígito como STRING. Otro fallo del harness fue
null_policy ubicado fuera de parameters en el nuevo control Recon; se corrigió
y se repitió el ciclo completo. Ninguno se presentó como defecto de producto
ni como prueba aprobada antes de repetirlo.

El recovery encontró dos incidencias y repitió el ciclo completo tras corregirlas:
transporte stdin CP1252 en Windows, ahora UTF-8 explícito; y backfill genérico de
arranque que añadía RUN_INPUT no canónico a Runs Delivery, ahora excluidos de esa
rutina legacy. No se borran enlaces históricos ni se relaja la comparación exacta.
El verificador distingue state 2/3/4 por revisión real e inventario congelado,
sin etiquetar un runtime antiguo como si ya hubiese migrado a 0009.

Los intentos CI fallidos se conservan en [ci-attempts.json](evidence/0.5.1/ci-attempts.json).
El primero detectó permisos ejecutables de dos scripts Linux. Otro benchmark
detectó `RECEIPT_NOT_READY`: SUCCESS/COMMITTED se persiste antes del receipt.
Los runners ahora esperan su referencia sólo con GET y fallan ante PENDING_REPAIR;
no reparan ni reenvían para lograr PASS. La UI espera hasta 60 segundos y permite
actualizar estado manualmente. El CI completo posterior aprobó sin relajar
verificaciones. La causa exacta de los fallos recovery de diagnóstico suprimido
no pudo establecerse y no se atribuye retrospectivamente a esa misma carrera.

En los drills legacy, dos intentos iniciales fallaron; el segundo precisó un GET
de configuración individual inexistente en el harness, corregido por el listado
público. Un tercer intento falló durante creación Compose antes del arranque API;
su diagnóstico suprimido no permite atribuir una causa exacta. El cuarto ciclo
completo aprobó ambos orígenes. Todos limpiaron recursos propios y conservaron
el inventario de la instalación principal. No se cuentan los intentos fallidos
como PASS ni se atribuyen automáticamente a defectos del producto.

La pérdida exacta post-commit de confirmación remota conserva
NOT_RUN_NONDETERMINISTIC; tests deterministas y fixture UNKNOWN de recovery no
prueban esa ventana física. La revisión humana no demuestra commit por sí sola.
El benchmark no cuenta filas físicas a partir de rows_written ni publica ceros
en lugar de métricas desconocidas. Volumen fuera de límites mantiene NOT_RUN.

## Antecedente 0.5.0, no certificación 0.5.1

Corte documental del 23 de septiembre de 2026 (America/Bogota). La implementación
0.5.0 añade Data Delivery y cambia código, migración, topología Compose y contratos;
por tanto, ningún resultado de 0.4.1 se hereda como certificación de esta entrega.
Las cifras siguientes proceden de ejecuciones cerradas; los estados remotos se
registran únicamente después de observar el workflow correspondiente.

| Comprobación 0.5.0 | Resultado de publicación |
| --- | --- |
| pytest backend y scripts; Ruff; Mypy | 869 aprobadas en 54,78 s; dos avisos de deprecación Starlette/AnyIO. Ruff PASS. Mypy PASS sobre 38 archivos fuente. |
| ESLint; TypeScript; Vitest; build frontend | PASS; 128 pruebas Vitest en 14 archivos. Bundle JavaScript principal 611,41 kB / 179,47 kB gzip, con advertencia informativa de tamaño. |
| Playwright funcional | PASS: 20 pruebas aprobadas y 5 opt-in omitidas en el ciclo integral; las omisiones se cubren en runners especializados y no se cuentan como éxito. El flujo Delivery real aprobó builder, preview, preflight, publicación, ejecución y receipt. |
| Compose integral y migración `0008_data_delivery` | PASS: doctor 8/8, smoke API 84/84, Alembic `check` y round trip upgrade/downgrade/upgrade, workers DEFAULT/DELIVERY y persistencia tras restart. |
| `connections-e2e` | PASS: 62 comprobaciones reales PostgreSQL/SQL Server y 2/2 Playwright focales; incluye temporales sin zona, smoke 84/84, credenciales y persistencia tras restart. Se informa por separado del 20/5 integral. |
| `delivery-e2e` | PASS: 92 comprobaciones reales PostgreSQL/SQL Server; cuatro estrategias, target nuevo/existente, preflight inválido, permisos, fallo remoto, reinicio, secretos, auditoría, receipt y manifest. Playwright focal: 1/1. La reproducción real de UNKNOWN quedó `NOT_RUN_NONDETERMINISTIC`; su semántica se certifica con pruebas deterministas. |
| Backup/restore/reset | PASS: migración 0008, 7 artifacts, 2 secretos —1 fuente y 1 destino—, 127 relaciones y destrucción del origen antes del restore. La credencial destino restaurada produjo una Delivery COMMITTED de 4 filas; doctor, smoke y búsqueda de secretos también pasaron. |
| Compatibilidad backup 0.4.1→0.5.0 | PASS desde `92c58eae9a1ddef653c0c9c888cfd7698ee1af3a`: manifest 1/state 2/0007, 21 tablas legacy y SHA canónico idénticos; restore actual en 0008 con 24 tablas, tres tablas Delivery vacías, 19 jobs históricos en lane DEFAULT, ambos workers y doctor recovery-ready. |
| Benchmark smoke | PASS: 1.074.923 bytes, 1.000 filas y cuatro columnas, 40,31 s; peak total 379.941.026 bytes. Es una prueba acotada del harness, no una certificación de volumen. |
| GitHub Actions | [PASS: siete jobs](https://github.com/eddiesan422/TrackvanceCore/actions/runs/35875426350), commit `57c0319ae2f0ef27ae63eced8e354258efc0854d`, sin reintentos. Backend 869/869; frontend 128/128; Conexiones 62/62; Delivery 92/92. |

CI aprobó `backend`, `frontend`, `compose-e2e`, `connections-e2e`, `delivery-e2e`,
`backup-restore-e2e` y `benchmark-smoke`. Los logs remotos registran Python en
50,08 s; Playwright Compose 19 aprobadas/6 opt-in omitidas y demo limpio 1/1;
Conexiones 23 aprobadas/2 omitidas; Delivery focal 1/1. Son ejecuciones separadas:
no se suman como escenarios únicos ni se convierten omisiones en éxitos. El
restore CI verificó siete artifacts, dos secretos y 127 relaciones, con uso
efectivo de la credencial destino restaurada y receipt COMMITTED.
El cruce de nombres en los logs confirma 25 escenarios Playwright distintos
con al menos un PASS: los cuatro opt-in de fuentes SQL pasan en Conexiones;
demo limpio pasa en su paso separado y Delivery en su job. Los IDs de jobs y
este cruce están en [el resumen CI](evidence/0.5.0/ci.json).

La [evidencia local saneada](evidence/0.5.0/README.md) conserva los resultados
de Delivery, Conexiones, recuperación y compatibilidad 0.4.1. El último caso
se ejecutó localmente, no como job adicional de CI. El commit posterior del PDF
y del resultado de Actions constituye el cierre documental de este código.

El código preparado para esta certificación usa proyectos y volúmenes aislados.
`scripts/tests/delivery_cycle.py` levanta destinos reales, aplica fixtures, prueba
API y puede ejecutar `frontend/tests-e2e/delivery.spec.ts`; no debe apuntar a la
instalación principal. El workflow declara `backend`, `frontend`, `compose-e2e`,
`connections-e2e`, `delivery-e2e`, `backup-restore-e2e` y `benchmark-smoke`.

La aceptación funcional debe demostrar, sin inferirla sólo de unitarios: destino
versionado y secreto no expuesto; metadata/preview/preflight; cada estrategia
habilitada; lane y heartbeat DELIVERY; receipt/manifest/linaje; recuperación de
los dos nuevos volúmenes; y que una confirmación ambigua queda `UNKNOWN` sin
reintento automático. El RBAC granular de Delivery es un pendiente de producto;
la certificación 0.5.0 verifica la reutilización de permisos documentada.
`UNKNOWN` no se fuerza contra un motor real porque perder exactamente la
confirmación posterior al commit no es reproducible de forma determinista; las
pruebas backend inyectan esa frontera y verifican que no se traduzca a éxito,
fallo ni reintento automático.

La frontera `STARTED` quedó endurecida: toda conversión/serialización local ocurre
en `PreparedDelivery`; el worker reclama lease con orden Run→Job antes de
reconciliar; un resultado remoto conocido prevalece sobre cancelación posterior.
Los targets existentes se bloquean incluso con cero filas. PostgreSQL revalida
bajo lock la constraint UPSERT nombrada y rechaza RLS activa en OVERWRITE. SQL
Server rechaza `IGNORE_DUP_KEY`; OVERWRITE exige `SELECT` y `VIEW DEFINITION` y
rechaza FILTER security policies. Las pruebas cubren drift, collation nativa,
cancelación y pérdida de lease sin repetir una escritura.

El backup SQLite copia a un destino nuevo y verifica tamaño/hash antes de abrir o
mutar la base; Docker usa staging temporal privado/read-only y reverifica cada
componente antes de consumirlo. Ambos evitan carreras TOCTOU. El formato legacy state 2 no
guardaba una huella estructural del catálogo: la compatibilidad se verifica por
el contrato exacto de 21 tablas y la proyección canónica disponible, no por un hash
histórico inexistente.

## Base certificada de 0.4.1

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
| GitHub Actions 0.4.1 | [PASS: seis jobs](https://github.com/eddiesan422/TrackvanceCore/actions/runs/35638817011) sobre `38a8c7a3`: backend, frontend, connections-e2e, compose-e2e, benchmark-smoke y backup-restore-e2e. |

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
python scripts/tests/delivery_cycle.py
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
