# Validación del ciclo C01–C06 de Trackvance Core 0.7.0

Baseline `12ca7061696d3581a18237dc7737348a3462e2c4`, rama `feat/local-prototype`. Implementación formal `393b7e25e413bf641d5483c25c53951642611f51`, con árbol limpio al ejecutar. Los [resultados iniciales](validation-0.7.0-initial.md) se conservan como antecedentes. Este ciclo permanece **ABIERTO** hasta pruebas completas, PDF revisado, CI del HEAD final y upgrade real verificado. Ningún verde previo se hereda.

El formal de esa revisión terminó **FAIL en el primer navegador de 400.000 filas**, después de aprobar los ocho tiers, la cadena API/SQL de un millón y los ensayos de control. La causa confirmada fue del harness: navegaba tras re-login antes de completar la autenticación. El informe completo se conserva como [evidencia intermedia saneada](evidence/0.7.0-corrections-20261004/formal-393b7e2-browser-fail.json), sin convertir su FAIL global en PASS. Las repeticiones de navegador y el backup/restore posterior tendrán informes propios.

## Gates de esta corrección

| Alcance | Estado actual y condición |
| --- | --- |
| C01 adquisición XLSX | Ocho tiers reales PASS con defaults normales: inline/shared de 100.000, 100.001, 400.000 y 1.000.000 filas. Huella completa, numeración física, inferencia tardía, cancelación y crash/lease PASS. Navegadores 400.000/1.000.000 pendientes tras FAIL intermedio. |
| C02 diagnóstico y límites | Host PASS; exceso real de 1.000.001 filas → ACQUISITION_ROW_LIMIT con detalle/referencia coherentes en adquisición, historial y aviso, sin versión parcial. Validación completa del recorrido de navegador pendiente. Errores históricos intactos. |
| C03 áreas | Host PASS, incluidos 151 datasets propios y exclusión de otra organización. Ambos formularios y conservación por navegador/restore todavía pendientes. |
| C04 timezone | Host PASS: fechas inválidas bloqueadas, ancla UTC/cursor preservados y DST existente. Crear, editar y reabrir en navegador real pendientes. |
| C05 despacho | PostgreSQL real PASS: cuatro programaciones sobre dos datasets/versiones distintos de un millón, targets distintos y quinto Job con lease vivo. Dispatch 0,204167 s; 15 puntos de I/O instrumentados sin llamadas. Esperas de cola censuradas; no se afirman cuatro commits SQL. |
| C06 bandeja | Host y transición read/unread idempotente por API PASS en la cadena real, con delta de contador de uno. Recarga/logout/reinicio, navegador y preservación por backup/restore pendientes. |
| Regresión | Backend/frontend/scripts host PASS del alcance indicado abajo. CI exacto final y sus suites PostgreSQL/Spark/Docker/SQL/E2E pendientes; no se heredan jobs de otros SHA. |
| Recuperación | Huella actual state 7/0016 de 42 tablas; state 6/0015 soportado mediante proyección estricta de sólo los dos diagnósticos NULL. Backup/restore real nuevo pendiente. |
| Documento v1.1 | Fuente, generador e inputs corregidos; PDF candidato, extracción y revisión visual de todas las páginas pendientes. |
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

El checkpoint GitHub de `393b7e2` conserva **once jobs SUCCESS, dos FAIL de transporte, un FAIL del selector Playwright legacy y un job XLSX todavía RUNNING**, quince en total. Las tres causas de FAIL tienen fixes locales verificados; **CI del SHA final sigue pendiente**. La aprobación local no cambia retroactivamente el resultado de esos jobs ni del formal que falló en navegador. Las URLs y conclusiones completas del run final se registrarán cuando termine.

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

La cancelación real se pidió tras 10.000 registros observados y terminó CANCELLED sin versión. El crash/lease de un millón produjo dos intentos y una sola versión íntegra, con huella y numeración físicas correctas. La fixture de **1.000.001 filas** falló con `ACQUISITION_ROW_LIMIT`, máximo 1.000.000 y observado 1.000.001, mantuvo la versión previa íntegra y no publicó otra parcial. El diagnóstico nuevo quedó registrado y visible en su aviso; es un caso negativo esperado PASS aunque la adquisición termine FAILED. Backup/restore nativo de estos diagnósticos/read_at, navegadores finales y upgrade permanecen pendientes porque el formal se detuvo antes de ese gate.
