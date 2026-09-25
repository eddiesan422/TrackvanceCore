# Benchmark específico de Data Delivery 0.5.1

Fecha de ejecución: 25 de septiembre de 2026. Este ensayo mide DataSink sobre
PostgreSQL 16 y SQL Server 2022 Developer reales en proyectos Docker desechables.
No mezcla sus números con Intake, ReconOps o Sentinel y no certifica capacidad
productiva, un tamaño máximo universal ni percentiles de rendimiento.

## Método y reproducibilidad

El runner `scripts/tests/delivery_benchmark_cycle.py` crea un proyecto nuevo con
prefijo exclusivo `trackvance-delivery-bench-`, puerto local libre y contraseñas
aleatorias que no publica. Rechaza proyectos con contenedores, volúmenes o redes
existentes antes de asumir su limpieza. Arranca los cinco servicios de Trackvance
y dos destinos, con los overlays `compose.delivery-test.yml` y
`compose.delivery-benchmark.yml`. Las pruebas de Conexiones y Delivery terminaron
y limpiaron sus entornos antes del benchmark. La instalación habitual en 3100
no se usó como destino del runner ni se detuvo.

El CSV se genera determinísticamente con `SHAKE256_URLSAFE_V1`: cada fila lleva
una clave, importe, fecha y payload ASCII distinto. No es una columna repetida
que se comprima de forma extrema. El tamaño solicitado corresponde al payload;
encabezado, separadores y las otras columnas hacen que el archivo real sea mayor.
La ingestión normal conserva el hash y registra cuatro tipos lógicos:
`record_key: STRING`, `amount: DECIMAL`, `event_date: DATE`, `payload: STRING`.
El mapping conserva esos tipos; `amount` se publica como `DECIMAL(18,0)`.

Cada motor recibe en orden:

1. `CREATE_AND_LOAD`: crea tabla y carga N filas.
2. `APPEND`: añade N filas; el destino pasa a 2N.
3. `OVERWRITE`: elimina el contenido anterior y deja N filas.
4. `UPSERT`: fuera del cronómetro añade PK, elimina la mitad de claves y cambia
   los importes restantes a cero. La entrega debe actualizar N/2 e insertar N/2.

No se ejecuta SQL de usuario: el SQL auxiliar del runner prepara/verifica sólo
el fixture desechable. El producto sigue usando su mapping declarativo. Cada
caso exige un único intento `COMMITTED`, Run `SUCCESS / COMMITTED`, identidad
coherente de receipt/manifest, métricas iguales en los cuatro documentos y
duraciones positivas finitas. Se comprueba por SQL independiente el total de
filas, claves distintas, suma de importes y longitud mínima/máxima del payload.
Así, contar filas sin actualizar los importes no basta para aprobar UPSERT.
Esto no es una comparación byte a byte de cada celda del destino.

### Qué significan los tiempos y recursos

| Campo | Intervalo medido / limitación |
| --- | --- |
| Preflight explícito | HTTP `/delivery/preflight`; incluye su transporte local |
| Preflight worker | `perf_counter` alrededor de `preflight_delivery` al ejecutar |
| Escritura | `perf_counter` alrededor de `deliver_prepared`: conexión, checks/locks transaccionales, DML y commit; no incluye preparación local anterior |
| Total del caso | Desde preflight explícito hasta obtener receipt/manifest: incluye publicación de configuración, encolado y polling; excluye arranque, ingestión, preparación auxiliar del target y comprobación SQL independiente |
| Encolado → terminal | POST de Run y polling cada 0,5 s hasta observar estado terminal; no es latencia exacta del worker |
| Filas/s de escritura | N / segundos de escritura |
| MB fuente/s | Bytes del CSV / 1.000.000 / segundos de escritura; **no** mide bytes de red o archivos internos de SQL |
| CPU | Delta de `cpu.stat` por contenedor en la ventana del caso, con actividad incidental del mismo contenedor |
| Memoria cgroup | Pico acumulado desde que arrancó el contenedor; **no** pico exclusivo de un caso |
| I/O | Deltas de `io.stat` cuando el entorno los expone, incluyendo checkpoints y actividad incidental; no se atribuye todo al payload |
| Storage / temporal | Muestreo de `/var/lib/trackvance` y su `tmp`, con pausa de 0,2 s más coste de `docker exec`; puede omitir picos breves |

El wall time del runner incluye build/arranque, ingestión, ocho casos,
instrumentación y verificaciones hasta el inicio de la limpieza. No debe
compararse directamente con la suma de tiempos de escritura. Cada combinación
se ejecutó una vez por tier, sin aleatorización del orden ni repeticiones para
estimar varianza; cachés, calentamiento y tareas de fondo afectan el resultado.

Los tiempos locales publicados conservan su ejecución original. Una corrida
posterior de CI descubrió que COMMITTED puede observarse antes de publicarse el
receipt: el runner ahora espera de forma acotada su referencia, sólo con GET,
antes de descargar evidencia. PENDING_REPAIR falla explícitamente; no repara ni
repite la entrega para lograr PASS. Esa espera forma parte del total del caso,
no del tiempo de escritura; los números previos no se recalculan ni se atribuyen
a una nueva corrida. El smoke de CI posterior aporta evidencia independiente.

### Máquina y límites

Windows 11 x86-64, Docker Desktop 29.1.3/overlayfs, 16 CPU lógicas asignadas a
Docker y 16.326.520.832 bytes de memoria Docker. Los JSON registran disco libre
y capacidad exactos de cada ejecución. Los techos por contenedor son:

| Servicio | Techo de memoria |
| --- | ---: |
| Metadata PostgreSQL | 384 MiB |
| API | 1 GiB |
| Worker DEFAULT | 768 MiB |
| Delivery worker | 1 GiB |
| Web | 128 MiB |
| PostgreSQL destino | 384 MiB |
| SQL Server destino | 2.560 MiB; límite interno MSSQL 2.048 MiB |

Se conservan `MAX_UPLOAD_BYTES=10.485.760` y 100.000 filas, incluso si el shell
contiene overrides de un benchmark anterior. El overlay sólo impone techos de
recursos; no aumenta límites de entrada ni planner. El preflight del runner exige
6 GiB de memoria Docker y 12 GiB de disco libre. El watchdog corta al superar
memoria Docker menos 1 GiB, disco libre menor de 10 GiB o el plazo configurado.
Un corte no se convierte en PASS. La instrumentación tiene resolución limitada
y no sustituye límites duros ni una monitorización productiva.

## Smoke ejecutado

Archivo real **1.074.923 bytes**, 1.000 filas y cuatro columnas; payload de 1.049
caracteres por fila. SHA-256:
`1214a61a8b7c53cebd613447febd9845b50786130e625ec36f8351f5fcd0fd7d`.
**8/8 PASS**, limpieza PASS; wall registrado **143,59 s**. Este tier comprueba
el runner, no certifica volumen. Datos completos en
[smoke aprobado](evidence/0.5.1/delivery-benchmark-smoke-retry/result.json).

| Motor | Estrategia | Preflight HTTP s | Preflight worker s | Escritura s | Total s | Filas/s escritura | MB fuente/s |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| PostgreSQL 16 | CREATE_AND_LOAD | 0,048 | 0,049 | 0,0255 | 0,732 | 39.247 | 42,188 |
| PostgreSQL 16 | APPEND | 0,090 | 0,085 | 0,0168 | 1,273 | 59.601 | 64,066 |
| PostgreSQL 16 | OVERWRITE | 0,094 | 0,077 | 0,0247 | 0,761 | 40.463 | 43,495 |
| PostgreSQL 16 | UPSERT | 0,087 | 0,082 | 0,0392 | 1,308 | 25.503 | 27,414 |
| SQL Server 2022 | CREATE_AND_LOAD | 0,131 | 0,042 | 0,4034 | 1,287 | 2.479 | 2,665 |
| SQL Server 2022 | APPEND | 0,206 | 0,073 | 0,4097 | 1,424 | 2.441 | 2,624 |
| SQL Server 2022 | OVERWRITE | 0,089 | 0,079 | 0,4727 | 1,279 | 2.116 | 2,274 |
| SQL Server 2022 | UPSERT | 0,101 | 0,077 | 1,7187 | 3,366 | 582 | 0,625 |

Ambos motores registraron 1.000 filas preparadas/enviadas y 1.070.886 bytes de
valores preparados por caso. Los primeros tres casos reportaron 1.000 inserciones
y cero actualizaciones; UPSERT SQL Server reportó 500/500. UPSERT PostgreSQL 16
conservó `null / null`: la verificación independiente aprobada no se usa para
inventar un conteo que el adaptador no pudo observar transaccionalmente.

El watchdog obtuvo 28 muestras, pico agregado de memoria de 1.251.307.681 bytes y
CPU agregada máxima de 141,38 % (porcentaje de núcleos, no del total de 16 CPU).
No se disparó ningún umbral ni hubo OOM observado. El almacenamiento de artifacts
creció entre 10.121 y 10.187 bytes por caso; el temporal máximo muestreado fue
cero. Esto no demuestra que nunca existiera un archivo temporal breve. El JSON
incluye los deltas CPU/I/O y picos acumulados por cada servicio y caso.

## Carga representativa

Archivo real **8.917.809 bytes**, 20.000 filas y cuatro columnas; payload de 420
caracteres por fila. SHA-256:
`a6fee7bb1c434f5ae4e28dfa741724aebe500f204935c0edd5331e0aa28af204`.
**8/8 PASS**, limpieza PASS; wall registrado **207,70 s**. Es mayor que el smoke
en filas y bytes totales, pero tiene celdas más cortas; no es una progresión del
mismo ancho por fila. Datos completos en
[carga representativa](evidence/0.5.1/delivery-benchmark-representative/result.json).

| Motor | Estrategia | Preflight HTTP s | Preflight worker s | Escritura s | Total s | Filas/s escritura | MB fuente/s |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| PostgreSQL 16 | CREATE_AND_LOAD | 0,246 | 0,270 | 0,2082 | 1,624 | 96.071 | 42,837 |
| PostgreSQL 16 | APPEND | 0,511 | 0,464 | 0,1945 | 2,586 | 102.807 | 45,841 |
| PostgreSQL 16 | OVERWRITE | 0,496 | 0,472 | 0,2191 | 2,631 | 91.273 | 40,698 |
| PostgreSQL 16 | UPSERT | 0,539 | 0,514 | 0,6329 | 3,252 | 31.599 | 14,090 |
| SQL Server 2022 | CREATE_AND_LOAD | 0,280 | 0,287 | 7,1269 | 9,340 | 2.806 | 1,251 |
| SQL Server 2022 | APPEND | 0,584 | 0,463 | 7,1833 | 9,303 | 2.784 | 1,241 |
| SQL Server 2022 | OVERWRITE | 0,479 | 0,481 | 7,2688 | 9,778 | 2.751 | 1,227 |
| SQL Server 2022 | UPSERT | 0,497 | 0,505 | 31,7338 | 34,458 | 630 | 0,281 |

Cada caso registró 20.000 filas preparadas/enviadas y 8.837.772 bytes de valores
preparados. Los primeros tres casos reportaron 20.000 inserciones y cero
actualizaciones. UPSERT SQL Server reportó 10.000/10.000 y PostgreSQL 16
`null / null`. Por separado, la consulta física encontró 40.000 filas tras
APPEND y 20.000 tras cada otra estrategia; siempre 20.000 claves distintas,
payload de 420 caracteres y la suma de importes esperada. Estos fixtures carecen
de triggers/rules que supriman o desvíen acciones; las filas enviadas no se
generalizan a un censo físico de cualquier target.

### Recursos de la carga representativa

| Motor / estrategia | CPU Delivery worker s | CPU destino s | Pico cgroup destino MiB acumulado | I/O escritura destino MiB delta | Crecimiento storage bytes |
| --- | ---: | ---: | ---: | ---: | ---: |
| PostgreSQL / CREATE_AND_LOAD | 1,410 | 0,364 | 122,70 | 25,82 | 10.189 |
| PostgreSQL / APPEND | 1,528 | 0,430 | 156,23 | 9,66 | 10.141 |
| PostgreSQL / OVERWRITE | 1,588 | 0,443 | 178,21 | 28,12 | 10.159 |
| PostgreSQL / UPSERT | 1,751 | 0,819 | 224,57 | 12,91 | 10.171 |
| SQL Server / CREATE_AND_LOAD | 3,265 | 6,993 | 746,41 | 84,47 | 10.177 |
| SQL Server / APPEND | 3,264 | 7,358 | 784,54 | 20,85 | 10.123 |
| SQL Server / OVERWRITE | 3,334 | 7,125 | 804,07 | 165,06 | 10.144 |
| SQL Server / UPSERT | 9,676 | 32,164 | 1.501,21 | 79,82 | 10.167 |

El watchdog obtuvo 40 muestras: pico agregado de memoria de **2.050.125.461
bytes**, CPU agregada máxima **213,85 %** y Block I/O acumulado máximo
848.686.889 bytes. Ningún umbral se activó; todos los contenedores observados
registraron `oom_killed=false`. El pico cgroup acumulado del Delivery worker
alcanzó 217.186.304 bytes. Los JSON también incluyen API, metadata, web y worker
DEFAULT: la tabla no presenta los dos servicios seleccionados como el total.

El storage compartido comenzó con 15.325.905 bytes y terminó con pico de
15.407.176 bytes. El crecimiento de esta ventana incluye evidencia, no el
tamaño inicial de los artifacts fuente ni el almacenamiento interno SQL. El
temporal máximo muestreado fue cero, con 6–85 muestras por caso. Los deltas
I/O no son una medida exclusiva de bytes de negocio: por ejemplo, checkpoints
pueden explicar que una ventana escriba más que el CSV.

El recibo de limpieza confirma eliminación de siete contenedores, ocho volúmenes
y una red del proyecto representativo. El smoke aprobado y el primer smoke
fallido limpiaron la misma cantidad de recursos de sus propios proyectos. Son
datos sintéticos desechables, sin recuperación mediante esos volúmenes borrados;
permanecen los resultados saneados y el generador determinista.

## Incidencias, tiers no ejecutados y límites

El primer smoke se detuvo antes de Delivery, **0/8 casos**, porque el mapping
original suponía INT64 para `amount`. El lector CSV preserva texto y el profiling
por valores registra DECIMAL. Se corrigió el mapping del runner, no la política
del producto, y se añadió una regresión con lector/profiler reales sin overrides.
El proyecto fallido también se limpió correctamente. Su resultado se conserva
en [primer smoke detenido](evidence/0.5.1/delivery-benchmark-smoke/result.json).

Los tiers 100 MiB, 500 MiB, 1 GiB, 2 GiB y 5 GiB están marcados
`NOT_RUN_RESOURCE_LIMIT` con razón `UNCHANGED_PRODUCT_UPLOAD_OR_ROW_LIMIT`:
exceden el límite de entrada de producto que este runner deliberadamente no
aumenta. No se afirma que el hardware sea incapaz de procesarlos ni que sean
volúmenes certificados. Los cases no alcanzados por un fallo reciben
`NOT_RUN_AFTER_FAILURE`; los no alcanzados por un límite se distinguen como
`NOT_RUN_RESOURCE_LIMIT`, con el caso activo y motivo registrados aparte.

Estos fixtures no cubren alta concurrencia, latencia WAN, targets de producción,
drivers distintos, tipos anchos arbitrarios, todos los triggers/RLS/rules ni
interrupciones de commit. La matriz de corrección PG16/PG18 y los escenarios
funcionales están en [Delivery E2E](evidence/0.5.1/delivery-e2e.json); sus resultados
no se transfieren a este benchmark como una medición de volumen.

## Comandos y evidencia

Desde la raíz del repositorio, con Docker Desktop disponible:

```powershell
python scripts/tests/delivery_benchmark_cycle.py --smoke --max-wall-seconds 1200
python scripts/tests/delivery_benchmark_cycle.py --target-mib 8 --rows 20000 --max-wall-seconds 1800
```

Por defecto se crea una carpeta nueva en `.codex-local/delivery-benchmarks/`.
`--evidence-dir` permite elegir una carpeta inexistente para el informe saneado;
no se debe reutilizar una carpeta o proyecto existente. El runner elimina el CSV
al terminar y limpia sólo recursos de su proyecto mediante un plan de reset
con hash de integridad e inventario exacto. Publica `result.json`, `reset-plan.json` y
`reset-plan-receipt.json`, sin credenciales ni payloads de filas. La limpieza
elimina definitivamente esos datos sintéticos desechables, no la instalación
habitual. No limpia imágenes o cachés globales de Docker.

La suite del runner tiene **36 pruebas PASS**, incluido rechazo de proyectos
existentes, tipos/hash de ingestión, identidad/semántica de evidencia, null frente
a cero, tiempos inválidos, verificación SQL, skips sin recursos creados y fallo
de limpieza que no puede esconderse detrás de un exit code de skip. Ruff
de los dos archivos PASS. El primer Ruff de los tests nuevos señaló una llamada
`dict()` innecesaria; se sustituyó por literal y se repitió con éxito. El smoke
de Delivery puede ejecutarse en CI separado del tier representativo local;
el resultado remoto de ese job se informa en [validación](validation.md).

La última regresión de cleanup/exit code se añadió tras iniciar la carga
representativa y volvió a pasar junto con las otras 35 y Ruff. Sólo afecta el
camino de skip/fallo de limpieza, no los tiempos ni el camino exitoso medido.
