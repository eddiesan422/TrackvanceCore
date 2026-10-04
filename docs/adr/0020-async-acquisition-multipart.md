# ADR 0020: adquisición asíncrona y snapshots multipart

- Estado: implementado; certificación de volumen registrada por tier y motor.
- Fecha: 2026-10-03.

## Decisión y diagnóstico

La adquisición anterior devolvía un DataFrame completo, perfilaba toda la población
en memoria y publicaba un Parquet único dentro de la petición HTTP. Aunque SQL
utilizaba `fetchmany`, concatenaba los registros; el límite de 100.000 filas no
constituía un contrato para millones de registros. El parser multipart también
recibía la carga antes de aplicar el límite del endpoint.

La recepción nueva utiliza un cuerpo binario directo con tamaño anunciado y
observado, reserva de disco y limpieza de transferencias incompletas. La respuesta
201 confirma exclusivamente la transferencia y una inspección de hasta 100
registros. Las opciones de hoja/delimitador y los tipos confirmados se registran
después mediante 202, con `AcquisitionRun`, límites efectivos congelados y un Job
de lane ACQUISITION. La petición no materializa ni perfila la población completa.
Las rutas históricas sincrónicas conservan sus contratos.

El worker dedicado posee únicamente secretos de fuentes. Congela revisión de
conexión, selección de tabla/vista, esquema nativo y usuario; revalida permiso y
estado antes de publicar. PostgreSQL utiliza transacción read-only REPEATABLE
READ y cursor de servidor. SQL Server utiliza FreeTDS `dbnextrow`, `fetchmany`
acotado y SERIALIZABLE: conserva una lectura consistente mediante locks y puede
bloquear escrituras concurrentes. No declara aislamiento SNAPSHOT no habilitado.
Las consultas CASE con sentinel detectan celdas demasiado grandes en el servidor
antes de enviarlas al driver; el sentinel falla toda la adquisición y nunca
publica columnas truncadas. Los timeouts de consulta de cada conexión continúan
vigentes, además del deadline acumulado de adquisición.

## Lectura y perfil

CSV/TXT y NDJSON se recorren por registros. NDJSON descubre completamente la unión
de columnas sin conservar registros y vuelve a recorrer el archivo para generar
lotes. Parquet verifica footer, tamaño expandido y tamaño de grupos de lectura,
materializando ventanas acotadas. XLSX y JSON no lineal tienen un contrato menor
explícito: 10 MiB y 100.000 filas por defecto; su incumplimiento falla y recomienda
un formato de volumen, sin truncar la población. Los límites se configuran mediante
`TRACKVANCE_ACQUISITION_*` y se conservan en cada adquisición.

Parquet utiliza `ParquetFile.iter_batches`, sin pre-buffer ni hilos, después de
verificar el máximo de bytes expandido de cada grupo. Cada grupo se descomprime
una sola vez; no se repite el scan del archivo para cada slice. Los valores de
timestamp nanosegundo se convierten a texto antes del paso por datetime Python.

Los defaults de volumen son 1 GiB de archivo, cinco millones de registros, 2 GiB
de valores UTF-8 observados, lotes de 5.000 filas / 8 MiB, celdas de 64 KiB,
presupuesto analítico de 256 MiB y reserva de 512 MiB de disco. Los fetches además
reducen filas por el peor ancho permitido; la cantidad de registros del lote nunca
es una autorización para un lote ilimitado en bytes. Un grupo Parquet mayor que
la mitad del presupuesto de memoria se rechaza antes de descomprimirlo.

La inferencia final, null/distinct/uniqueness y las validaciones de overrides
cubren todas las partes. DuckDB realiza las agregaciones globales con memoria
limitada, un hilo y spill privado; no se suman distintos calculados por lote.
Estadísticas monetarias se acumulan con Decimal y precisión suficiente para todo
el conjunto. Pruebas comparan el resultado entero con `profile_frame`, incluyendo
decimales de más de 28 dígitos. Se conservan null frente a texto vacío, Unicode
compuesto/descompuesto, espacios, ceros iniciales y valores temporales observados.

El fetch del perfil usa el máximo UTF-8 de la columna completa, calculado dentro
del motor acotado, y una cota conservadora de memoria Unicode. El perfil publica
`observed_record_bytes_upper_bound = sum(32 + 4 * max_length)` sobre todos los
tipos, incluidas columnas sólo NULL. Spark puede limitar Arrow y la conversión a
Python sin estimar el ancho a partir de promedios o muestras.

## Almacenamiento, publicación y numeración

StorageProvider publica un descriptor JSON versión1, MIME
`application/vnd.trackvance.parquet-set+json`, y registra cada Parquet como
Artifact inmutable. El descriptor declara esquema físico, orden, filas, bytes,
SHA-256 y locators relativos de cada parte. `dataset_paths` valida límites,
traversal, identidad, hash/tamaño, esquema y conteos antes de escanear. Las partes
tienen linaje DATASET_PART; versiones y resultados conservan linaje de fuente,
run y parent. Los Parquet únicos anteriores mantienen su identidad y lectura.

Las partes físicas contienen `__tv_record_number` Int64; el esquema de negocio
y la muestra excluyen esa columna. CSV/TXT conserva la línea física inicial de
cada registro, incluyendo campos multiline; JSON/Parquet usa ordinal de registro;
SQL declara SNAPSHOT_ROW sin inventar una línea física de tabla. Accepted output
conserva el número del origen en lugar de renumerarlo. El contrato de bytes de
DatasetVersion sigue asociado al Artifact que define su SHA; el costo materializado
se declara separadamente como `ingestion_metadata.canonical_size_bytes`.

La publicación final bloquea Acquisition → Job → Dataset, comprueba owner, lease,
attempt y cancelación, y confirma versión READY, artifacts, linaje y estados
terminales en una sola transacción. Un fallo, cancelación o lease perdido conserva
las versiones anteriores y no expone una versión parcial. El helper de publicación
no hace commit; Spark lo utiliza bajo el fence del caller. Los intentos perdidos
pueden dejar bytes inmutables huérfanos, pero no sustituir otros artifacts.

La recuperación reclama un lease vencido con attempt nuevo, hasta tres intentos.

Entre fetches estrechos se consulta cancelación/lease como máximo cuatro veces
por segundo; cada frontera de materialización, progreso o etapa consulta el estado
inmediatamente. El bloqueo y comprobación final siguen siendo el fence de
publicación. Así un cursor acotado a pocas filas no introduce una consulta al
metastore por cada pequeño fetch.
La limpieza usa locks en el mismo orden y elimina sólo intentos privados muertos
después de gracia, transferencias RECEIVED vencidas sin adquisición y staging de
adquisiciones FAILED/CANCELLED vencidas sin salida. Excluye leases vivos, trabajo
QUEUED y toda referencia Artifact/DatasetVersion. No recorre directorios de negocio.

## Interfaz y certificación

La consulta de presentación del perfil devuelve las estadísticas globales
persistidas y lee como máximo veinte registros con Arrow, sin abrir una conexión
analítica ni recalcular conteos. Verifica primero todos los hashes, esquemas y
conteos de los footers de las partes. La conversión a Python utiliza el ancho
global observado para acotar el lote; una versión histórica sin cota utiliza
un solo registro. La muestra JSON tiene un presupuesto de 8 MiB y declara
`sample_limited` cuando una fila no cabe, sin esconder el perfil completo.

La UI muestra transferencia real de XHR, confirmación de recepción, opciones,
registro202, etapa, filas, bytes observados, duración, error seguro y cancelación
cooperativa. Polling e historial consultan el estado durable; salir de la pantalla
no cancela un Job registrado. Sólo SUCCESS selecciona la versión publicada.

`scripts/tests/volume_cycle.py` y `frontend/tests-e2e/volume.spec.ts` registran
fixtures streaming reproducibles y resultados por fase/tier. Un límite de recurso
o una fase no ejecutada figura explícitamente; los tiers mayores no se certifican
por extrapolación. Las pruebas rápidas usan DB y almacenamiento desechables.

La medición real de 100/500/1024 MiB, sus bytes reales, hashes completos y límites
de interpretación se conservan en
[`acquisition-volume-0.7.0.md`](../development/acquisition-volume-0.7.0.md).
El HTTP504 inicial del perfil1 GiB permanece como intento fallido; la reanudación
no elimina la incidencia ni vuelve a ejecutar la cadena CSV ya aprobada. La CPU
que cruza un reinicio de cgroup se marca incompleta, en lugar de publicar deltas
negativos. La prueba de navegador se certifica separadamente con hashes de toda
la salida source/accepted y de todos los registros comprometidos en SQL.
