# Validación de Trackvance Core 0.8.0

La implementación actual incorpora Catálogo, gobierno y Reportes sobre el
baseline auténtico 0.7.0 `d9b6856e757a2a1fcab3913209146f3b7b79d70c`.
El contrato vigente es `0017_catalog_reports`, 55 tablas y backup manifest 2 /
state 8. Las referencias a 42 tablas, state 7 y migración 0016 en los antecedentes
de este documento describen ejecuciones históricas, no el runtime actual.

El ciclo 0.8.0 permanece abierto hasta completar los oráculos de volumen,
recuperación nativa y desde el commit auténtico, revisión del documento, todos los
jobs GitHub Actions del SHA final y la actualización autorizada de la instalación
habitual. Las pruebas de desarrollo y los resultados de otra revisión no cierran
esos gates. El registro final se conserva en
[validation_results_0.8.0.json](../specification/validation_results_0.8.0.json).

## Contrato CI actual y mediciones de optimización

La [CI actual](ci.md) declara 19 grupos y 64 escenarios obligatorios en
`scripts/ci/scenarios.json`. Conserva los 16 jobs funcionales anteriores y reparte
el recorrido XLSX en adquisiciones/límites, navegador, despacho/cadena y
recuperación. Los ocho tiers inline/shared, los navegadores de 400k/1M, la cadena,
los negativos y los restores siguen siendo obligatorios. `fast` ejecuta tres
grupos existentes de desarrollo y su gate indica `certifies_final=false`.
Código, dependencias, migraciones, harnesses, CI y paths inciertos fuerzan `full`.
La actualización habitual requiere el modo completo, evidencia y hashes del
SHA final y todos los jobs aplicables completados SUCCESS.

La [baseline de costes saneada](evidence/0.8.0/ci-optimization-baseline.json)
se calculó sólo desde logs/metadatos ya conservados. El intento 4 del run
37368090419/495f4eb terminó CANCELLED: 15 SUCCESS y un XLSX CANCELLED.
Los intervalos de jobs suman 303,35 minutos de runner, sin cola, incluyendo el
frontend reutilizado y el job incompleto; no son minutos facturados ni coste de
certificación completa. Catálogo tomó 4.050 s de job y 3.934,695 s de ciclo;
los tiers API 120/400k/1M tomaron 38,412/1.045,771/2.343,219 s con fixtures y
oráculos completos. Los builds explícitos visibles suman al menos 180 s;
los internos no tienen desglose exacto. Las esperas anteriores de backend sin
runner ni steps, 903/902/902 s, son fallos de asignación externos separados del
runtime funcional. El historial CANCELLED/FAIL permanece íntegro.

El snapshot posterior conservado de 37388511854/63096e6 tenía 11 SUCCESS y cinco
jobs sin terminar de 16. No permite cerrar ese run ni comparar su coste completo.
Las verificaciones locales de ese SHA limpio pasaron: frontend 33 archivos/252
tests, lint, tipos y build; backend Windows 1.269 PASS/46 SKIP en 330,30 s, Ruff y
mypy75. Backend Windows sin scripts y Linux CI backend+scripts tienen alcances
distintos; sus conteos no se suman ni las omisiones Windows acreditan aislamiento
Linux o JVM.

La nueva estructura y sus guardas se verifican con pruebas puras; todavía requiere
ejecuciones completas fría y caliente del SHA que las versiona. Deben medir
build/transferencia, instalación, fixtures, adquisiciones, oráculos, SQL, navegador,
recuperación, subida/cleanup y esperas de runners por separado. No hay una mejora
porcentual, presupuesto final ni certificación de la optimización acreditados.
La fuente PDF se sincroniza con este contrato antes de generar un candidato
nuevo, revisar todas sus páginas y hacer el commit documental final. Respaldo
fresco, restore aislado, promoción verificada y cleanup propio siguen siendo
gates distintos y pendientes hasta su ejecución real.

## Checkpoint de desarrollo del 5 de octubre de 2026

La regresión Windows posterior produjo **1.857 PASS, 42 SKIP** en 340,54 segundos,
tras el import nativo y la limpieza de staging de perfilado. Las omisiones por
plataforma/JVM se conservan explícitas. El checkpoint previo produjo
**1.841 PASS, 40 SKIP** en 350,92 segundos;
frontend **252 PASS**, lint, tipos y build. El navegador real final de ese
checkpoint completó la cadena de Catálogo/Reportes sin omisiones ni reintentos en
34,88 segundos. Las poblaciones de 120 y 400.000 filas por cada una de tres
fuentes y el millón pasaron con oráculos completos. La ejecución de un millón
registra cambios de fuente durante el recorrido y no certifica el SHA final.
La observación HTTP sigue su propio registro. Los tres primeros CI 0.8.0 fallaron
en 21 pruebas del ejecutor Linux; el cuarto, sobre `09730f0`, produjo
1.860 PASS / 23 FAIL / 16 SKIP y un SIGABRT en el probe nativo. Sus dependientes
quedaron SKIPPED. Esos fallos se conservan en
[el historial de intentos](evidence/0.8.0/attempt-history.json); los ajustes de
biblioteca del runtime, fallback de threads e import nativo no habían resuelto
todos los fallos de ese runner. La comparación posterior aisló la causa restante.
La comparación posterior en un mismo cgroup anidado reprodujo el SIGABRT y
EACCES de `memory.max` con el código anterior; la revisión `3b48d97` completó
el protocolo bajo los mismos límites. Tres regresiones Linux verifican permisos
por archivo para cgroups v1/v2, exclusión de vecinos y rechazo de traversal/symlinks.
La observación HTTP completó diez casos en 111,255 s sin escrituras durante
los flujos API/motor/proxy; conserva tres FAIL previos del harness. El workflow
37357555598 completó runtime/1.886 tests/Ruff y frontend con éxito, pero falló
mypy Linux por stdout Optional en el closure del lector. Se captura ahora el
stream no nulo; pasos posteriores y jobs omitidos no certifican el SHA final.
La suite actual de Reportes en Ubuntu/Python Actions completó **70 PASS, cero
FAIL/SKIP** en 33,63 s. El primer ensayo de ese conjunto produjo seis fallos de
reserva de disco por el tmpfs insuficiente del fixture; se conserva y su repetición
usa un volumen exclusivo con capacidad comprobada, sin cambiar límites de producto.
La recuperación real nativa e histórica terminó **PASS en 388,57 s**: nativo con
55 tablas y las trece entidades nuevas pobladas; origen auténtico 0.7.0 con 42→55,
proyección exacta, archivos/secretos y diagnóstico real preservados. Los dos fallos
previos del harness también se conservan. Esos ensayos no certifican el HEAD final.
Estos números corresponden a árboles de desarrollo identificados en la
evidencia y no sustituyen la certificación del SHA definitivo.

El workflow `37359580093` sobre `3832596` pasó backend **1.887 PASS / 16 SKIP**
en 306,08 s, Ruff, mypy75, contratos y migraciones. Diez jobs terminaron SUCCESS;
XLSX y los tres tiers async fallaron antes de población porque un `up --no-build`
sin lista de servicios intentaba arrancar REPORT sin imagen privada preparada.
La restauración 0.6.1 tenía todavía guards de 42 tablas; la huella actual exige
55/0017 y conserva la proyección histórica exacta. Esos cinco FAIL se registran
en [el checkpoint de CI](evidence/0.8.0/ci-3832596-failure.json); el gate de Catálogo
y Reportes seguía ejecutándose al corte. Las correcciones conservan la cobertura
y los tiers obligatorios. El siguiente HEAD documental necesita todos los jobs
SUCCESS de una ejecución completa.

Ese workflow terminó después con **16 jobs: diez SUCCESS y seis FAIL**. El
sexto fallo ocurrió en la recuperación histórica de Catálogo/Reportes: el
checkout superficial no contenía el commit auténtico `d9b6856` que necesita
`git archive`. La reproducción aislada conserva el mismo HEAD y pasa de exit
128 a exit 0 al descargar el historial completo; no atribuye su stderr al runner
original. El job descarga ahora todo el historial, manteniendo sus gates.
El [resultado completado](evidence/0.8.0/ci-3832596-completed-failure.json) conserva
los seis fallos y los PASS reales de navegador, tres tiers, diez casos HTTP y
recuperación nativa. El CI íntegro del nuevo SHA continúa siendo obligatorio.

El workflow `37368090419`, intento 4 sobre `495f4eb`, terminó **CANCELLED: 15
SUCCESS y un CANCELLED entre los 16 jobs**. XLSX agotó el presupuesto de 90
minutos; el log no muestra un fallo de assertion. Su único navegador completado
pasó en 387,001 s y corresponde a 400.000 por inferencia del orden del driver,
no por metadata del artefacto. Dispatch pasó; el ciclo XLSX integral y su
recuperación nativa quedaron sin acreditar. [La evidencia real](evidence/0.8.0/ci-495f4eb-timeout.json)
conserva hashes y estados. La revisión posterior 630 amplió ese job monolítico
a 180 minutos y añadió checkpoints saneados, manteniendo poblaciones y límites;
la estructura actual lo divide en cuatro grupos conforme al contrato anterior.
El nuevo CI íntegro y
la instalación habitual siguen pendientes; este intento no cierra la certificación.

La revisión posterior del descriptor produjo **87 PASS / cero FAIL o SKIP** en
41,48 s en Linux: 71 Reportes y 16 almacenamiento/puertos. Incluye una caída real
`os._exit(77)` después de escribir el JSON del descriptor dentro del staging
privado; el reintento publica una vez, la limpieza reclama sus candidatos sin
metadata y conserva otro intento RUNNING. El área temporal global permanece
intacta. Esas 71 pruebas se solapan con las 70 previas y no se suman como casos
distintos. [La evidencia](evidence/0.8.0/descriptor-recovery.json) conserva límites,
hashes y fallos del fixture. La fuente actual pasó también mypy75 y Ruff en Linux
en 13,255 s, con prueba de bytes y dependencias bloqueadas, según
[su registro](evidence/0.8.0/linux-typecheck-final-source.json).

La recuperación posterior desde la baseline auténtica 0.6.1 `6fac26b` terminó
**PASS en 164,566 s**: 31→55 tablas, 0012→0017, state 5→8 y proyección histórica
exacta. Conservó 29 artefactos, los secretos cifrados de fuente y destino y la
notificación histórica sin reproducirla; las trece tablas nuevas quedaron vacías.
Origen destruido antes del restore fresco, destino STOPPED_VERIFIED, instalación
habitual UNCHANGED y cleanup propio completo. [Su evidencia](evidence/0.8.0/authentic-061-current-restore.json)
identifica la imagen y los bytes actuales montados; no certifica el SHA final de CI.

El checkpoint PDF 0.8.0 anterior se publicó tras revisar las **143 páginas**, con 280 marcadores
y 40 secciones principales. Las dos copias oficiales tienen el SHA-256
`223175c92b9a13a031a9eea99e8cdba671e9fbda2461edede2cd3f032f15055c`;
fuente, generador, insumos y extracción coinciden. La [verificación de publicación](evidence/0.8.0/pdf-verification.json)
conserva la revisión visual completa y los hashes históricos intactos. Este PASS
abarca únicamente el documento: el workflow completo del SHA que lo versiona y
la actualización habitual mantienen sus gates externos hasta verificarse realmente.

El checkpoint anterior se conserva a continuación:

La suite completa de backend y scripts produjo **1.825 PASS, 32 SKIP, cero fallos**
en 372,71 segundos sobre el árbol de desarrollo de ese checkpoint. Las omisiones
de Windows no acreditan el ejecutor Linux ni JVM. Los cambios posteriores de
paginación del catálogo, retención de asociaciones inactivas, guardas de
recuperación y selección de revisión tienen verificaciones focales separadas:
la última selección de gobierno, lifecycle de Reportes y recuperación produjo
**47 PASS y 9 SKIP** en 19,30 segundos. Son conjuntos solapados y no se suman.
Ruff del alcance backend/scripts y mypy de los 75 módulos fuente pasaron; el CI
exacto final debe volver a ejecutar las comprobaciones sobre su propio SHA.

El contrato exportado tras añadir selección de revisión contiene 152 paths HTTP,
56 permisos y 55 tablas. La migración PostgreSQL aislada
0016→0017→0016→0017 preservó la proyección anterior y la estructura física:
55 tablas, 645 columnas y 109 claves foráneas. Ese ensayo de migración no sustituye
un backup/restore con las trece clases nuevas pobladas ni una recuperación desde
un backup auténtico 0.7.0.

Las verificaciones de gobierno incluyen aprobación estricta con evidencia real,
integridad de archivos, linaje y configuración; rechazo de errores, advertencias,
cobertura insuficiente, salida vacía y enlaces incompatibles; autorización
transitiva en rutas nativas y exportación; retirada de permisos o bloqueo antes
de publicar; aislamiento de organización y paginación SQL. La revisión de las
fronteras implementadas está en
[security-review-0.8.0.md](security-review-0.8.0.md). La disponibilidad de un runner,
un PASS unitario o un endpoint no demuestra su gate de volumen o recuperación.

## Antecedente: ciclo C01–C06 de Trackvance Core 0.7.0

Baseline `12ca7061696d3581a18237dc7737348a3462e2c4`, rama `feat/local-prototype`. Implementación formal `393b7e25e413bf641d5483c25c53951642611f51`, con árbol limpio al ejecutar. Los [resultados iniciales](validation-0.7.0-initial.md) se conservan como antecedentes. Este ciclo permanece **ABIERTO** hasta pruebas completas, PDF revisado, CI del HEAD final y upgrade real verificado. Ningún verde previo se hereda.

El formal de esa revisión terminó **FAIL en el primer navegador de 400.000 filas**, después de aprobar los ocho tiers, la cadena API/SQL de un millón y los ensayos de control. La causa confirmada fue del harness: navegaba tras re-login antes de completar la autenticación. El informe completo se conserva como [evidencia intermedia saneada](evidence/0.7.0-corrections-20261004/formal-393b7e2-browser-fail.json), sin convertir su FAIL global en PASS. La repetición e75 y su backup/restore se documentan más abajo con un informe independiente.

## Formal e75e1038: repetición completa y procedencia

La nueva ejecución formal terminó **PASS**, con certificación `e75e1038f845979da8cdf7cc38de91bfe359d349` y árbol limpio. La implementación de producto sigue siendo `393b7e25e413bf641d5483c25c53951642611f51`: los árboles `backend/src` y `frontend/src` son idénticos entre ambas revisiones. El SHA e75 añade fixes del harness/transporte y documentación; los tiempos y recursos siguientes provienen exclusivamente de esta nueva ejecución. El ciclo continúa **ABIERTO** hasta CI del HEAD final y upgrade principal verificado; el PDF corregido ya tiene revisión fresca completa PASS.

La [evidencia formal saneada](evidence/0.7.0-corrections-20261004/formal-e75e1038-pass.json) tiene SHA-256 `03a7f6bd9c2533b0237e1e83b5216b8350a95f1c344f6b8ad59201d13a4a0eff`. Conserva valores null, etapas muestreadas, presión de memoria, contadores incompletos y esperas censuradas. El formal 393 permanece FAIL y los focales quedan separados; ningún resultado antiguo sustituye una medición nueva. Las fixtures son sintéticas con oráculo independiente, no originales del usuario.

| Registros | Variante | ZIP físico (bytes) | Expansión declarada (bytes) | Worker (s) | Adquisición y verificación completa (s) |
| --- | --- | --- | --- | --- | --- |
| 100.000 | inline | 12.712.418 | 57.545.861 | 13,674062 | 18,381 |
| 100.000 | shared | 12.458.465 | 54.985.067 | 16,474737 | 21,911 |
| 100.001 | inline | 12.712.556 | 57.546.478 | 13,677396 | 18,398 |
| 100.001 | shared | 12.458.607 | 54.985.623 | 16,099988 | 21,704 |
| 400.000 | inline | 51.594.508 | 250.635.773 | 55,606279 | 62,525 |
| 400.000 | shared | 50.450.321 | 242.714.008 | 67,601451 | 73,992 |
| 1.000.000 | inline | 129.356.448 | 636.815.736 | 140,779197 | 150,987 |
| 1.000.000 | shared | 126.421.197 | 619.575.971 | 171,468868 | 181,775 |

Los ocho tiers nuevos verificaron toda la población, perfil exacto, inferencia posterior a 100.000 y filas físicas 5..N+4. Los límites efectivos normales fueron upload XLSX 1.073.741.824 bytes, 1.000.000 filas de datos, expansión 4.294.967.296 bytes, observados 2.147.483.648 bytes y analítica DuckDB 268.435.456 bytes, sin overrides XLSX.

La última columna es un reloj de adquisición y verificación, no el E2E de calidad/SQL. Transferencia, inspección y registro permanecen separados en el JSON. Polling nominal de 1 s y sondas nominales de 2 s tienen intervalos reales variables; las ventanas de stage no se presentan como tiempos internos exactos. READING incluye XML/SST; las etapas no observadas y los recursos sin muestra siguen desconocidos/null.

| Tiers grandes | PROFILING observado (s) | RSS worker (bytes) | Cgroup worker muestreado (bytes) | Temporales worker muestreados (bytes) | Delta CPU worker (s) |
| --- | --- | --- | --- | --- | --- |
| 400.000 inline | 5,063 | 311.410.688 | 697.643.008 | 530.410.490 | 59,056554 |
| 400.000 shared | 5,106 | 341.590.016 | 848.224.256 | 602.096.549 | 72,360197 |
| 1.000.000 inline | 14,317 | 379.621.376 | 986.009.600 | 655.668.709 | 148,650163 |
| 1.000.000 shared | 14,301 | 419.602.432 | 1.272.107.008 | 650.739.429 | 181,331285 |

El pico conjunto cgroup muestreado fue **3.401.781.248 bytes**, máximo de sumas simultáneas de los cuatro servicios instrumentados del backend (API, DEFAULT, Delivery y adquisición), bajo el presupuesto 6.442.450.944 bytes. No suma los máximos individuales ni representa el total de los nueve servicios del contexto. Docker disponía de 16.326.524.928 bytes; la comprobación inicial exigía reserva host 21.474.836.480 bytes. Cada fase conserva su propia reserva/guard y mínimo de disco libre. Los temporales incluyen archivos del proceso y no equivalen exclusivamente a spill DuckDB. `memory.peak` es máximo de vida del contenedor, separado del pico por fase.

Presión/eventos observados en `1000000_inline/xlsx_acquisition_whole`/`api`: `{"max": 466}`. El contador `max` no se sustituye por OOM; se conserva su causa reportada sin atribución adicional.
Presión/eventos observados en `1000000_inline/intake_pyspark`/`api`: `{"max": 88}`. El contador `max` no se sustituye por OOM; se conserva su causa reportada sin atribución adicional.
Presión/eventos observados en `1000000_shared/xlsx_acquisition_whole`/`api`: `{"max": 971}`. El contador `max` no se sustituye por OOM; se conserva su causa reportada sin atribución adicional.
Contabilidad incompleta registrada en `xlsx_crash_lease_recovery`/`acquisition-worker`: CPU 219,560104 s, reinicios de contador 1, `cpu_measurement_complete=false` y `memory_events_measurement_complete=false`. Son deltas parciales; no se inventa contabilidad tras el reinicio.
Presión/eventos observados en `xlsx_crash_lease_recovery`/`api`: `{"max": 1145}`. El contador `max` no se sustituye por OOM; se conserva su causa reportada sin atribución adicional.
Presión/eventos observados en `xlsx_row_limit_failure_preserves_previous_version`/`api`: `{"max": 1167}`. El contador `max` no se sustituye por OOM; se conserva su causa reportada sin atribución adicional.
Contabilidad incompleta registrada en `personal_unread_api_restart`/`api`: CPU 0,355001 s, reinicios de contador 1, `cpu_measurement_complete=false` y `memory_events_measurement_complete=false`. Son deltas parciales; no se inventa contabilidad tras el reinicio.

El perfil propio asignó 2 CPU/1.536 MiB a adquisición y Delivery; main conserva 1 CPU/1.536 MiB en ambos. DEFAULT tiene 2 CPU/3 GiB en ambos. No se extrapolan tiempos a main. El parámetro de perfil de 268435456 bytes acota DuckDB, no el RSS de todo el worker con lector, Python, writers, índice y file cache.

La cadena nueva de **un millón inline** completó Intake PySpark APPROVED, accepted exacto, Delivery SQL e inbox. Fuente, accepted y SQL comparten huella completa `90c27748be21d71481a567a5ab209f021a462c3f6626a72ee3b97248bc7750d2`. El reloj real upload→Delivery e inbox confirmados fue **299,554 s**; no procede de sumar fases. Sus fases de preflight, Intake, Delivery e inbox se conservan separadas en el JSON.

C05 volvió a probar cuatro programaciones sobre dos datasets/versiones distintas de un millón y quinto Job con lease vivo. Dispatch, incluido commit de metadata: **0,207531 s**; excluye setup, espera de cola y commit SQL. Los 15 puntos instrumentados de población tuvieron cero llamadas. Las observaciones de cola originales quedan censuradas cuando `started_at=null`; no se afirman cuatro commits SQL terminados.

Cancelación terminó CANCELLED sin versión tras 10.000 registros observados. Crash/lease tuvo 2 intentos y una sola versión íntegra. El negativo de 1.000.001 terminó FAILED con ACQUISITION_ROW_LIMIT, sin versión parcial, diagnóstico idéntico en historial/aviso y versión previa íntegra: gate esperado PASS.

Los dos navegadores reales de 400.000/1.000.000 terminaron PASS. Registraron dominio, validación de zona y conservación del instante UTC, recorrido Intake→Delivery, filtro personal y transición no leída persistente tras reload/logout. El runner verificó fuente y accepted contra toda la población/numeración física y destino SQL contra la huella completa. Los relojes del reporter de esta ejecución fueron **194,743311 s para 400.000** y **339,777340 s para 1.000.000**, ambos 1 PASS/0 SKIP. Se registran como evidencia suplementaria: el primero fue verificado por root en la salida del proceso 17494; su resumen standalone fue reemplazado por el último de un millón. El segundo conserva SHA y startTime de browser-summary.json en la evidencia pública. Son relojes completos de Playwright, no tiempos internos de adquisición ni medidas del focal anterior.

Backup/restore nativo nuevo PASS: 42 tablas, state 7/0016_acquisition_diagnostics, estado fuente/restaurado exactamente igual, multipart y bytes históricos intactos, destino STOPPED_VERIFIED y procesos automáticos sin activar. La no leída se conservó tras reinicio de API y comparación nativa. Main permaneció UNCHANGED. Esta recuperación aislada no sustituye el upgrade final de main.

El [CI 393 completado](https://github.com/eddiesan422/TrackvanceCore/actions/runs/37226453887) conserva **11 SUCCESS y 4 FAIL** de 15 jobs: dos fallos de transporte, selector legacy y carrera de re-login XLSX. Su [registro público](evidence/0.7.0-corrections-20261004/ci-393b7e2-failures.json) contiene conclusiones/URLs reales; los fixes e75 no convierten retrospectivamente esos FAIL en SUCCESS. **CI del HEAD final y main siguen pendientes.** La [revisión PDF](evidence/0.7.0-corrections-20261004/pdf-verification.json) certifica las 120 páginas finales y copias byte idénticas.

## Gates de esta corrección

| Alcance | Estado actual y condición |
| --- | --- |
| C01 adquisición XLSX | Formal e75 PASS: ocho tiers nuevos con defaults normales, huella/perfil completos, numeración física, inferencia tardía, cancelación, crash/lease y navegadores de 400.000/1.000.000. |
| C02 diagnóstico y límites | Formal e75 PASS: negativo de 1.000.001 ACQUISITION_ROW_LIMIT sin versión parcial, historial/aviso coherentes y recorrido de navegador. Histórico intacto por comparación nativa; no se atribuyen causas a originales ausentes. |
| C03 áreas | Host 393 PASS; los dos navegadores e75 registraron y conservaron su dominio en ambos formularios. Estado completo preservado en restore aislado; main pendiente. |
| C04 timezone | Host 393 y ambos navegadores e75 PASS: invalidaciones, creación/revisión/reload y conservación del instante UTC. La cobertura DST host conserva su procedencia propia. |
| C05 despacho | Nuevo PostgreSQL real PASS: cuatro programaciones, dos datasets/versiones de un millón, quinto Job con lease vivo y 15 puntos I/O en cero. Dispatch 0,207531 s; esperas censuradas, sin afirmar cuatro commits SQL. |
| C06 bandeja | Formal e75 PASS: transición idempotente/delta 1, filtros, reload/logout, reinicio de API y preservación exacta por backup/restore. Estado personal y notificaciones históricas intactos; main pendiente. |
| Regresión | Backend/frontend/scripts host PASS del alcance indicado abajo. CI exacto final y sus suites PostgreSQL/Spark/Docker/SQL/E2E pendientes; no se heredan jobs de otros SHA. |
| Recuperación | Nuevo backup/restore aislado e75 PASS, state 7/0016, 42 tablas y todos los artefactos/relaciones; comparación exacta antes de procesos automáticos. No sustituye upgrade main. |
| Documento v1.1 | PROBADO: PDF corregido de 120 páginas, 246 marcadores, 38 secciones; render y revisión visual fresca completa PASS, reproducción byte idéntica y once inputs/copia oficial/archivo histórico verificados. |
| Publicación | Sólo feat/local-prototype; todos los jobs del SHA final deben finalizar SUCCESS sin omitir gates. |
| Main | Sigue intacto durante certificación; upgrade final autorizado con backup nuevo verificado, mismos seis volúmenes/puertos/secretos y sólo migración 0016. |

## Resultados de desarrollo conservados

La primera prueba real de 400000 registros inline publicó la población completa y conservó todos los valores. La comprobación independiente rechazó numeración ordinal 1..400000 en lugar de filas físicas 5..400004. Ese FAIL y su duración de adquisición (55.947177 s) se conservan; no certifican la corrección. La revisión del lector mantiene números físicos y tiene regresiones de huecos y encabezados anteriores.

La segunda prueba de 400000 filas verificó valores, numeración física y perfil, pero el mapping del harness convirtió DATE a STRING. Preflight terminó técnicamente SUCCESS con decisión FAIL, y la publicación se bloqueó correctamente con VALIDATION_BINDING_MISMATCH. El harness ahora exige SUCCESS+PASS y respeta los tipos lógicos completos. Este intento permanece FAIL y separado de posteriores ejecuciones.

Las mediciones por etapa se obtienen por polling de 1 s y sondas de recursos de 2 s: son ventanas observadas, no relojes internos exactos. La lectura incluye indexación SST/XML. Los picos RSS/cgroup/temporales y deltas CPU se distinguen de las cotas de caché, lote y motor; una etapa no observada no se inventa.

Ver [ADR 0025](../adr/0025-corrections-c01-c06.md), [operación](operations.md), [parámetros](parameters-0.7.0.md) y [Spark](spark-volume-0.7.0.md).

La ejecución completa de desarrollo del 4 de octubre reunió 1696 PASS, 17 SKIP explícitos y un FAIL en cancelación durante preparación local de Delivery (286.67 s). Se conserva como intento intermedio y exige investigación/repetición; no es un gate aprobado. La revisión actual de frontend produjo 223 PASS (15.38 s), lint/types/build PASS. Mypy 63 módulos y Ruff del alcance CI PASS. Los fixes posteriores de setters de lectura tienen 28 pruebas focales PASS; sus resultados completos definitivos se registrarán por separado.

## Regresión host y checkpoint CI

Tras corregir el fence de cancelación, la revisión de implementación `393b7e2` produjo **1.708 PASS, 17 SKIP en 287,59 s** en backend, incluidas las pruebas previas de scripts. Frontend produjo **223 PASS en 27 archivos, 15,38 s**. Las **140 pruebas focales de Delivery** incluyen las dos ventanas de cancelación controladas: antes pasaba una y fallaba la otra; después ambas preservan CANCELLED, sin intento remoto. Son subconjuntos de la regresión, no conteos adicionales. El superset de scripts posterior al fix local de transporte produjo **523 PASS en 8,35 s**; también se conserva por separado, sin sumarlo a otros conteos solapados.

El checkpoint GitHub de `393b7e2` terminó con **once jobs SUCCESS y cuatro FAIL**, quince en total: dos de transporte, uno del selector Playwright legacy y otro de la carrera de re-login XLSX. El [registro completo](evidence/0.7.0-corrections-20261004/ci-393b7e2-failures.json) conserva los jobs y sus URLs. Los fixes e75 y su repetición local no cambian esos resultados; **CI del HEAD final sigue pendiente**.

El focal de navegador de 400.000 filas con la espera explícita de autenticación produjo **1 PASS, 0 SKIP en 160,802 s**; se verificaron después todas las filas y valores de fuente, salida Intake y destino PostgreSQL, además de numeración física. Su [evidencia integral](evidence/0.7.0-corrections-20261004/browser-400k-relogin-fixed.json) conserva la huella completa, la zona y el instante UTC, el dominio y la persistencia personal de no leída. El focal de áreas produjo **1 PASS, 0 SKIP en 46,890341 s**. Son comprobaciones locales y no cierran el formal completo, el navegador de un millón ni su CI.

## Formal 393b7e2: ocho tiers y métricas observadas

Las fixtures son sintéticas y tienen oráculo independiente; los originales del usuario no estaban disponibles. Incluyen ocho columnas, IDs, Unicode, null/vacío, fechas, fórmulas observadas, payload de 96 bytes y cambio de tipo después de 100.000 filas. La variante shared de un millón contiene 3.880.029 shared strings. Se comprueba la población completa y las posiciones físicas 5..N+4 con encabezado en fila 4; no se usa la dimensión falsa como total. No hubo overrides de límites XLSX: upload 1 GiB, 1.000.000 filas de datos, expansión 4 GiB, observados 2 GiB y perfil DuckDB de 256 MiB.

| Registros | Variante | Bytes físicos ZIP | Bytes expandidos declarados | Duración del worker (s) | Recepción y verificación completa (s) |
| --- | --- | --- | --- | --- | --- |
| 100.000 | inline | 12.712.418 | 57.545.861 | 14,701146 | 19,656 |
| 100.000 | shared | 12.458.465 | 54.985.067 | 16,131037 | 19,695 |
| 100.001 | inline | 12.712.556 | 57.546.478 | 14,307072 | 18,581 |
| 100.001 | shared | 12.458.607 | 54.985.623 | 15,624890 | 19,457 |
| 400.000 | inline | 51.594.508 | 250.635.773 | 54,969027 | 60,902 |
| 400.000 | shared | 50.450.321 | 242.714.008 | 66,823773 | 72,801 |
| 1.000.000 | inline | 129.356.448 | 636.815.736 | 140,353548 | 151,185 |
| 1.000.000 | shared | 126.421.197 | 619.575.971 | 170,344284 | 179,697 |

La última columna es el reloj de recepción, espera y comprobación íntegra de esa adquisición; no equivale al reloj interno del worker ni al E2E de calidad/SQL. Transferencia, inspección y registro se guardan separadamente en el JSON. En el millón inline fueron 0,540/0,077/0,094 s; en shared, 0,535/0,087/0,131 s. El inspector observó como máximo 100 registros y dejó el total desconocido.

PROFILING se observó durante 6,121/6,101 s en 400.000 inline/shared y 14,290/14,264 s en el millón inline/shared. Son ventanas entre respuestas de polling, no mediciones internas exactas. READING incluye lectura XML/SST; MATERIALIZING puede alternarse con ella. PUBLISHING no se observó en algunos tiers y queda desconocido. Una ventana sin sonda de recursos conserva null; no se inventa CPU, RSS ni temporales a partir de fases vecinas.

| Adquisición de un millón | Pico RSS acquisition-worker (bytes) | Pico cgroup muestreado del worker (bytes) | Pico temporal muestreado del worker (bytes) | Delta CPU del worker (s) |
| --- | --- | --- | --- | --- |
| inline | 440.172.544 | 608.940.032 | 208.574.115 | 149,731553 |
| shared | 445.390.848 | 886.947.840 | 393.774.203 | 180,021885 |

El mayor pico conjunto cgroup observado fue **2.774.106.112 bytes**, suma simultánea muestreada de los servicios del contexto aislado, bajo el presupuesto de 6 GiB (6.442.450.944 bytes). No es la suma de máximos individuales ni un pico continuo exacto. El total Docker disponible era 16.326.524.928 bytes y la reserva de disco host exigida, 20 GiB. Sondas nominales de 2 s y polling nominal de 1 s tienen intervalos reales variables. `memory.peak` se conserva como máximo de vida del contenedor y no se atribuye a una fase concreta.

El formal asignó 2 CPU/1.536 MiB a acquisition-worker y delivery-worker; main conserva 1 CPU/1.536 MiB en cada uno. DEFAULT tiene 2 CPU/3 GiB en ambos perfiles. No se extrapolan estos tiempos exactos a main. `TRACKVANCE_ACQUISITION_MEMORY_BYTES=268435456` limita la analítica DuckDB, no el RSS completo del worker: lector, writers, Python, índice y file cache tienen consumos adicionales. Por eso un RSS observado mayor de 256 MiB no se presenta como una violación de un hard cap de RSS que ese parámetro no impone.

Los deltas disponibles no registraron OOM/oom_kill, errores de medición ni violaciones del guard de recursos. En crash/lease, acquisition-worker tuvo un reinicio de contador: CPU acumulada parcial 217,853356 s y `cpu_measurement_complete=false`; también `memory_events_measurement_complete=false`. Por ello no se declara contabilidad completa de CPU/eventos de ese ensayo.

## Cadena íntegra y controles reales antes del FAIL de navegador

La cadena API/worker de **un millón inline** completó Intake PySpark APPROVED con 1.000.000 aceptados y cero errores/advertencias, Delivery encadenado sobre su `output_version_id` exacto, SQL con 1.000.000 filas e inbox personal. Fuente, aceptados y destino tienen la misma huella completa `90c27748be21d71481a567a5ab209f021a462c3f6626a72ee3b97248bc7750d2`; null y texto vacío fueron 10.000 de cada uno. El reloj observado desde upload hasta Delivery e inbox confirmados fue **306,389 s**. Se conserva ese reloj; no se obtiene sumando fases. Preflight completo, Intake, Delivery e inbox tienen medidas separadas de 29,048/54,581/69,841/1,402 s. La transición leído → no leído → no leído → leído fue idempotente y cambió el contador en uno; su persistencia por backup/restore sigue pendiente.

C05 real preparó cinco preflights completos de entradas de un millón, separados del dispatch. Las cuatro programaciones seleccionaron dos datasets/versiones diferentes, con un quinto Job propio RUNNING y lease vivo antes/después del despacho. El dispatch medido, incluidos sus commits de metadata, fue **0,204167 s**; excluye setup, espera de cola y commit SQL. Las quince sondas de hashes, descriptor, footer, filas, escaneo, preparación y DataSink tuvieron cero llamadas. Las cuatro ocurrencias quedaron ENQUEUED con `started_at=null`, `queue_to_started_seconds=null` y `wait_censored=true`; los tiempos de cola son censurados y las ocurrencias propias se cancelaron en cleanup. No se presentan como cuatro escrituras remotas terminadas.

La cancelación real se pidió tras 10.000 registros observados y terminó CANCELLED sin versión. El crash/lease de un millón produjo dos intentos y una sola versión íntegra, con huella y numeración físicas correctas. La fixture de **1.000.001 filas** falló con `ACQUISITION_ROW_LIMIT`, máximo 1.000.000 y observado 1.000.001, mantuvo la versión previa íntegra y no publicó otra parcial. El diagnóstico nuevo quedó registrado y visible en su aviso; es un caso negativo esperado PASS aunque la adquisición termine FAILED. Backup/restore nativo de esos diagnósticos/read_at y navegadores quedaron pendientes en este intento 393 porque se detuvo antes de ese gate; la repetición e75 se registra por separado y el upgrade final sigue pendiente.
