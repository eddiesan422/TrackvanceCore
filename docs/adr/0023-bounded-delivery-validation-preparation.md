# ADR 0023: preflight durable y preparación Delivery en disco

Estado: implementado. Fecha: 3 de octubre de 2026. La evidencia de cada prueba
y tamaño se mantiene separada de la existencia del código.

## Problema

La API anterior verificaba y convertía una población completa dentro de una
petición. `PreparedDelivery.rows` retenía una tupla de registros, los scans
convertían el Parquet entero a DataFrame y la unicidad acumulaba claves en RAM.
Repetir ese recorrido después de adquirir por lotes anulaba el límite de memoria.
La corrección de mensajes de checks se implementó previamente en `e37d529`;
esta decisión no modifica sus códigos, reglas o condiciones para conseguir PASS.

## Preflight persistido

Se reutilizan Run/Job y lane DELIVERY con módulo `DELIVERY_PREFLIGHT`. Una
Configuration `VALIDATION_PRIVATE` conserva el borrador exacto; no aparece como
configuración publicada. POST `/delivery/validations` registra durablemente el
trabajo y devuelve 202, sin contactar SQL ni crear DeliveryAttempt. Lista/detalle
son personales dentro de la organización, incluso para Administrator.

El resultado distingue `SUCCESS` técnico de `PASS/FAIL`. La validación recorre
todas las partes verificadas con `DatasetRecords` y DuckDB en disco; el preview
usa ocho filas por defecto y hasta veinte, nunca certificación completa. La publicación
de configuraciones grandes exige validación personal SUCCESS+PASS, mismo draft
hash, DatasetVersion y SHA canónico. Una edición invalida la reutilización.
El endpoint síncrono legacy mantiene hasta 100.000 filas efectivas y devuelve
`PREFLIGHT_ASYNC_REQUIRED` para mayores. Al ejecutar se repite validación vigente
de usuario, permisos, destino y drift: el servidor remoto puede cambiar.

El constructor de `DatasetRecords` obtiene conteo y columnas de un footer Arrow
por parte; la vista previa limitada lee una fila por lote Arrow. Esas dos rutas
de API no inicializan consultas nativas DuckDB ni su caché de tipos de parámetros.
La iteración de toda la población en el worker conserva DuckDB y su cota de scan.
Se mantienen verificación de hashes, tamaño, esquema y conteo de todas las partes.
Un 504 de publicación fue seguido de configuración efectivamente publicada; la
captura tardía encontró threads en reposo y su causa completa queda NOT_PROVEN.
La corrección pasó concurrencia de cuarenta muestras y los tres recorridos
integrales posteriores, sin aumentar timeouts.

## Preparación y binding

`DataSink.prepare_batched` convierte lotes de hasta BATCH_ROWS y 8 MiB observados
y escribe `PreparedRows` en SQLite privado. Los escalares JSON tipados conservan
Decimal, fechas/zona, null y Unicode; no se usa float ni pickle. El spool queda
sellado con hash por fila, hash de archivo, conteo y binding canónico completo:
Run, fuente/schema/hash, configuración, destino/revisión/hash, columnas tipadas,
target, estrategia, keys y cantidad preparada. La lectura es repetible y acotada.

`verify(expected_binding)` verifica identidad completa y bytes antes de STARTED.
La unicidad fuente UPSERT usa un índice SQLite global en disco, con comparación
Decimal a precisión completa y claves compuestas tipadas. Duplicados entre lotes
y nulls se rechazan; no se evalúa unicidad por lote. El staging SQL nativo
posterior descubre colisiones bajo tipos y collation del target.

Cancelación, lease, tiempo y reserva de disco se revisan durante preparación.
Un archivo alterado, binding ajeno o fallo local antes de STARTED produce
FAILED_PRECONDITION; no se inventa incertidumbre remota. El caller toma el fence
Run/Job y target guard, revalida responsable vigente y registra STARTED en su
transacción autorizada. El owner antiguo no puede publicar un resultado nuevo.

## Transacción y estado remoto

Executemany por lotes no modifica la frontera del commit. CREATE_AND_LOAD,
APPEND, OVERWRITE y UPSERT conservan una única transacción por intento, con
locks, constraints, permisos, drift y rollback conocidos. OVERWRITE conserva
la tabla y población anterior al rollback. No hay JDBC distribuido desde Spark.
Los executors sólo calculan snapshots; delivery-worker conserva DataSink.

rows_prepared describe el payload, rows_written las filas enviadas de una entrega
confirmada; insertadas/actualizadas dependen de capacidades fiables del adaptador.
Un conteo desconocido sigue siendo null/N/D. PostgreSQL 16/17 no inventa desglose
UPSERT; PostgreSQL 18 usa OLD documentado. usuario captura username real de Run
y fechaIngesta conserva la semántica del intento, también cuando el trigger es SYSTEM.

COMMITTED confirmado prevalece sobre cancelación tardía. UNKNOWN bloquea target
hasta revisión y decisión operativa explícita; no autoriza retry. PENDING_REPAIR
reconstruye evidencia sin DDL/DML ni contacto con el destino. El spool se elimina
al terminar DataSink, también ante UNKNOWN, conservando evidencia durable.

## Límites y evidencia

DuckDB tiene memoria de scan configurable; SQLite/spool requiere reserva de
disco y deadline de preparación. Los límites duros cgroup y el presupuesto
estimado del planner se documentan separados. Excel mantiene cotas explícitas;
CSV completo conserva resultados y hash sin truncar ni acumular la población.

Las pruebas focales comprueban binding ajeno/tamper, Decimal/temporales/Unicode,
50.000 registros con spool de 25 MiB y pico tracemalloc menor de 12 MiB, claves
entre lotes, cancelación/revocación, resultado completo y fallo fuera del preview.
SQL real de ambos motores/cuatro estrategias y el recorrido de un millón se
certifican en suites dedicadas, con resultados exactos y fallos registrados.
