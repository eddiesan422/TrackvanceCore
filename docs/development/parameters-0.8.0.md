# Parámetros efectivos y límites de 0.8.0

Los tamaños expresados en KiB, MiB y GiB usan unidades binarias; los valores en bytes son enteros exactos. Cambiar `.env` requiere recrear/reiniciar los servicios afectados; los trabajos registrados mantienen el plan y límites congelados. Presupuesto estimado, memoria JVM y límite cgroup son controles distintos.

| Variable | Default de librería / unidad | Rango / alcance / aplicación |
|---|---|---|
| `TRACKVANCE_ACQUISITION_MAX_UPLOAD_BYTES` | 1073741824 bytes | 1 KiB..5 GiB. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_ACQUISITION_MAX_ROWS` | 5000000 registros | 1..100.000.000. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_ACQUISITION_MAX_OBSERVED_BYTES` | 2147483648 bytes UTF-8 observados | 1 KiB..20 GiB. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_ACQUISITION_BATCH_ROWS` | 5000 registros/lote | 1..50.000. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_ACQUISITION_BATCH_BYTES` | 8388608 bytes/lote | 64 KiB..64 MiB. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_ACQUISITION_BOUNDED_FORMAT_BYTES` | 10485760 bytes JSON no lineal | 1 KiB..64 MiB. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_ACQUISITION_BOUNDED_FORMAT_ROWS` | 100000 filas JSON no lineal | 1..100.000. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_ACQUISITION_MEMORY_BYTES` | 268435456 bytes de analítica | 64 MiB..4 GiB. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_ACQUISITION_MIN_FREE_BYTES` | 536870912 bytes reserva de disco | 16 MiB..20 GiB. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_ACQUISITION_TIMEOUT_SECONDS` | 1800 segundos | 30..86.400. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_ACQUISITION_UPLOAD_TTL_SECONDS` | 86400 segundos | 300..604.800. API / acquisition-worker. Cota aplicada durante recepción, lectura, perfil o cleanup según el campo; congelada en AcquisitionRun. |
| `TRACKVANCE_DELIVERY_BATCH_ROWS` | 1000 filas/lote | 1..10.000. API / delivery-worker. Scan completo, spool/keys en disco y control cooperativo; no cambia garantías de commit remoto. |
| `TRACKVANCE_DELIVERY_SCAN_MEMORY_BYTES` | 268435456 bytes scan DuckDB | 64 MiB..2 GiB. API / delivery-worker. Scan completo, spool/keys en disco y control cooperativo; no cambia garantías de commit remoto. |
| `TRACKVANCE_DELIVERY_MAX_ROWS` | 5000000 filas | 1..100.000.000. API / delivery-worker. Scan completo, spool/keys en disco y control cooperativo; no cambia garantías de commit remoto. |
| `TRACKVANCE_DELIVERY_RESERVE_DISK_BYTES` | 536870912 bytes reserva de disco | 16 MiB..20 GiB. API / delivery-worker. Scan completo, spool/keys en disco y control cooperativo; no cambia garantías de commit remoto. |
| `TRACKVANCE_DELIVERY_SYNCHRONOUS_ROWS` | 100000 filas endpoint síncrono | 1..100.000. API / delivery-worker. Scan completo, spool/keys en disco y control cooperativo; no cambia garantías de commit remoto. |
| `TRACKVANCE_DELIVERY_PREPARATION_TIMEOUT_SECONDS` | 1800 segundos | 30..86.400. API / delivery-worker. Scan completo, spool/keys en disco y control cooperativo; no cambia garantías de commit remoto. |
| `TRACKVANCE_SPARK_MASTER` | local[2] URI/modo | local[K] K>=1 <=TOTAL_CORES; spark://host:port. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_DRIVER_MEMORY_MB` | 768 MiB JVM | 512..16.384. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_EXECUTOR_MEMORY_MB` | 768 MiB JVM/executor | 512..16.384. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_EXECUTOR_CORES` | 1 CPU/executor | 1..16. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_TOTAL_CORES` | 2 CPU total Spark | 1..64. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_PARTITIONS` | 4 particiones | 1..256. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_BATCH_ROWS` | 2048 filas | 1..10.000. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_BATCH_BYTES` | 8388608 bytes | 64 KiB..64 MiB. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_MAX_RECORD_BYTES` | 65536 bytes/fila | 1 KiB..1 MiB. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_MAX_GROUP_ROWS` | 10000 filas/grupo Recon | 1..100.000. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_MAX_GROUP_BYTES` | 16777216 bytes/grupo Recon | 64 KiB..64 MiB. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_MAX_RESULT_BYTES` | 16777216 bytes resultado driver | 1 MiB..64 MiB. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_SPARK_MEMORY_BUDGET_BYTES` | 2147483648 bytes estimados | Entero positivo; el overlay Standalone usa 3 GiB. API planner / DEFAULT / executors. Memoria JVM, recursos y cotas efectivas de fila/lote/grupo; budget estima y cgroup impone límite del contenedor. |
| `TRACKVANCE_WORKER_MEMORY_SOFT_BYTES` | 536870912 bytes | Positivo. API / DEFAULT planner. Presupuesto estimado Polars; no límite duro de RSS. |
| `TRACKVANCE_TEMP_MIN_FREE_BYTES` | 16777216 bytes | Positivo; Compose usa 536870912. API / DEFAULT. Reserva planner previa y control durante ejecución. |
| `TRACKVANCE_RUN_TIMEOUT_SECONDS` | 300 segundos | Positivo; Compose usa 1800. DEFAULT. Deadline cooperativo; no terminación arbitraria de un commit SQL. |
| `TRACKVANCE_SCHEDULER_POLL_SECONDS` | 5 segundos | 1..60. scheduler. Intervalo aplicado al bucle ligero. |
| `TRACKVANCE_SCHEDULER_BATCH_SIZE` | 100 ocurrencias/tick | 1..1.000. scheduler. Cantidad acotada por tick; no paralelismo de jobs. |
| `TRACKVANCE_EVENT_LEASE_SECONDS` | 300 segundos | 60..3.600. events-consumers. Lease CAS, heartbeat y fence final. |
| `TRACKVANCE_EVENT_POLL_SECONDS` | 1 segundo | 1..60. events-consumers. Intervalo del bucle independiente. |

## XLSX C01: límites independientes

| Variable XLSX asíncrona | Default / unidad | Rango y control |
|---|---|---|
| `TRACKVANCE_ACQUISITION_XLSX_MAX_ROWS` | 1000000 registros de datos | 1..1048575; espacio físico restante tras encabezado. Lectura completa; min con MAX_ROWS general y filas físicas disponibles. |
| `TRACKVANCE_ACQUISITION_XLSX_MAX_UPLOAD_BYTES` | 1073741824 bytes ZIP | 1 KiB..5 GiB. Recepción/registro/lectura; mínimo con upload general. |
| `TRACKVANCE_ACQUISITION_XLSX_MAX_EXPANDED_BYTES` | 4294967296 bytes XML expandidos | 1 KiB..20 GiB. Declaración ZIP y contador real de descompresión. |
| `TRACKVANCE_ACQUISITION_XLSX_METADATA_BYTES` | 8388608 bytes | 64 KiB..64 MiB. Directorio central antes de ZipFile y XML de metadata. |
| `TRACKVANCE_ACQUISITION_XLSX_INSPECTION_BYTES` | 4194304 bytes XML | 64 KiB..16 MiB. Presupuesto agregado de inspección HTTP; total desconocido si se agota. |
| `TRACKVANCE_ACQUISITION_XLSX_CACHE_BYTES` | 8388608 bytes | 64 KiB..16 MiB. Caché SQLite/shared strings; no es hard limit de RSS. |
| `TRACKVANCE_ACQUISITION_XLSX_MAX_ENTRIES` | 4096 miembros ZIP | 4..16384. EOCD antes de asignación de directorio y validación del paquete. |
| `TRACKVANCE_ACQUISITION_XLSX_MAX_STYLES` | 65536 estilos | 1..65536. Control de índices y formatos durante parse de estilos. |
| `TRACKVANCE_ACQUISITION_XLSX_MAX_CELLS` | 100000000 celdas de datos | 1..104857600. Conteo completo al materializar; además límites físicos/columnas. |
| `TRACKVANCE_ACQUISITION_XLSX_MAX_RECORD_BYTES` | 1048576 bytes UTF-8 por registro | 1 byte..8 MiB. Control antes de incorporar el registro al lote. |
| `TRACKVANCE_ACQUISITION_XLSX_TEMP_BYTES` | 8589934592 bytes temporales | 1 KiB..40 GiB. Índice, partes privadas y spill de perfil del intento; además reserva libre. |
| `TRACKVANCE_ACQUISITION_XLSX_METADATA_SECONDS` | 10 segundos | 1..60. Deadline de lectura de metadata. |
| `TRACKVANCE_ACQUISITION_XLSX_INSPECTION_SECONDS` | 5 segundos | 1..30. Deadline agregado HTTP metadata/muestra; inspection_limited sin falso total. |

La UI consulta `/acquisitions/limits?format=XLSX&route=ASYNC_ACQUISITION` antes de carga. Stage/inspect incorporan el descriptor efectivo. Excel tiene1048576 filas físicas incluyendo encabezado; el límite de datos también se reduce al espacio restante. No se anuncian5M filas XLSX. La carga rápida usa `route=LEGACY_UPLOAD` y conserva100k/10 MiB.

## Defaults de librería, Compose y certificación

La tabla describe los defaults del código cuando no hay un override de entorno.
Compose establece una reserva de disco de 536870912 bytes (512 MiB) para
`TRACKVANCE_TEMP_MIN_FREE_BYTES`, frente a los 16777216 bytes (16 MiB) de la
librería; también establece 1800 segundos para `TRACKVANCE_RUN_TIMEOUT_SECONDS`,
frente a los 300 segundos de la librería. Las demás variables de la tabla
conservan el mismo default en Compose, salvo un override explícito.

El ensayo CSV inicial de 0.8.0 usó upload de 2 GiB y bytes observados de
4 GiB por el overhead real del fixture nominal de 1 GiB. Esos overrides no
modifican la instalación del usuario ni certifican tamaños de 2/5 GiB sin
haberlos ejecutado. Las cotas de JSON no lineal siguen siendo menores.
El endpoint `/system/engines` expone los valores efectivos para la UI y los
benchmarks. La certificación C01–C06 de XLSX utiliza los defaults normales de
Compose, sin overrides exclusivos de los tests (`xlsx_test_limit_overrides=false`).

El overlay Standalone configura los puertos internos driver 7078/blockmanager
7079 y `MASTER=spark://spark-master:7077`. Los ejecutores no reciben secretos de
negocio. API y worker comparten MASTER y el presupuesto de 3 GiB del overlay.
El perfil local usa `local[2]` y un presupuesto de 2 GiB por defecto. El presupuesto
estimado del planner, la memoria JVM y el límite cgroup del contenedor son
controles distintos; ninguno sustituye a los otros.

## Controles fijos adicionales

- Concurrencia por proceso worker: 1 Job; múltiples réplicas requieren presupuesto adicional.
- MAX_UPLOAD_BYTES = 10 MiB y TRACKVANCE_MAX_ROWS = 100000 permanecen en las rutas síncronas legacy.
- Celdas de adquisición: 64 KiB; columnas: 100. El lote de Delivery admite 8 MiB observados además de BATCH_ROWS.
- PreparedRows: binding, hash y conteos completos; no hay commit por lote.
- Reintentos del consumidor de eventos: máximo 5, persistidos; las reservas y leases no autorizan replay de UNKNOWN.
- Página de resultados: hasta 100.000 filas y 16 MiB UTF-8 antes de decodificar; el exceso produce HTTP 422 RESULT_PAGE_BYTE_LIMIT, sin truncar. Muestra de perfil: hasta 20 filas y 8 MiB de JSON UTF-8.

La cota `TRACKVANCE_ACQUISITION_MEMORY_BYTES` controla el motor de perfil DuckDB, no todo el RSS ni `memory.current` del proceso. Lectores/writers, Python y file cache consumen memoria adicional; los picos reales y el límite de contenedor se registran por separado en la evidencia de volumen.

## Reportes: límites por perfil

Los límites siguientes son distintos de los de adquisición, calidad y Delivery.
Un límite configurable no certifica capacidad universal. /reports/limits expone
los valores efectivos; API, worker, proceso y cgroups aplican sus propias cotas.

| Variable | Default / unidad | Control |
| --- | --- | --- |
| `REPORT_THREADS` | 2 threads | 1 o más; limitado además por CPU del contenedor |
| `REPORT_MEMORY_MB` | 512 MiB motor | positivo, menor que PROCESS_MEMORY_MB |
| `REPORT_PROCESS_MEMORY_MB` | 2048 MiB proceso | positivo, mayor que MEMORY_MB; RLIMIT_AS |
| `REPORT_CONCURRENCY` | 2 ejecuciones globales | positivo; admission PostgreSQL serializada |
| `REPORT_BATCH_ROWS` | 512 filas/lote | 1..10.000 |
| `REPORT_BATCH_BYTES` | 8388608 bytes/lote | positivo |
| `REPORT_MAX_JOIN_ROWS` | 5000000 filas por cruce | positivo; validación de población completa |
| `REPORT_MAX_JOIN_EXPANSION` | 100 factor | positivo; N:M no elimina esta cota |
| `REPORT_PREVIEW_TIMEOUT_SECONDS` | 60 segundos | positivo; deadline y CPU |
| `REPORT_PREVIEW_MAX_ROWS` | 10 filas | 1..10 efectivo |
| `REPORT_PREVIEW_MAX_BYTES` | 8388608 bytes | positivo; payload y serialización |
| `REPORT_DOWNLOAD_TIMEOUT_SECONDS` | 180 segundos | positivo; deadline y CPU |
| `REPORT_DOWNLOAD_MAX_ROWS` | 100000 filas | positivo; exceso falla sin truncamiento exitoso |
| `REPORT_DOWNLOAD_MAX_BYTES` | 134217728 bytes | positivo; payload y serialización |
| `REPORT_XLSX_TIMEOUT_SECONDS` | 180 segundos | positivo; deadline y CPU |
| `REPORT_XLSX_MAX_ROWS` | 50000 filas | positivo; exceso falla sin truncamiento exitoso |
| `REPORT_XLSX_MAX_BYTES` | 67108864 bytes | positivo; payload y serialización |
| `REPORT_DATASET_TIMEOUT_SECONDS` | 1800 segundos | positivo; deadline y CPU |
| `REPORT_DATASET_MAX_ROWS` | 5000000 filas | positivo; exceso falla sin truncamiento exitoso |
| `REPORT_DATASET_MAX_BYTES` | 2147483648 bytes | positivo; payload y serialización |
| `REPORT_DATASET_TEMP_BYTES` | 2147483648 bytes privados | positivo; sólo DATASET; reserva física adicional512MiB |

PREVIEW y DOWNLOAD no escriben resultados ni spills en disco/tmpfs. Sólo DATASET
recibe report-staging privado. DuckDB512MiB y RLIMIT_AS2048MiB son cotas distintas;
RSS del hijo y memory.current del contenedor también son mediciones distintas.
Concurrencia2 es global sobre API/worker, mientras cada worker consume un Job.
Recrear servicios después de editar .env conserva versiones y contextos; no usa
una fuente nueva en un Job ya registrado. PREVIEW_MAX_ROWS se limita siempre a10.
