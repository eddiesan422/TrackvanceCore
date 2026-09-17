# ADR 0004 — Lectores de datasets y frontera común de calidad

Fecha: 2026-09-15. Estado: aplicado.

## Contexto

Data Intake recibía únicamente CSV y el servicio de versiones conocía detalles
del parser. Añadir Excel, JSON, Parquet y TXT en ese punto habría acoplado el
motor de calidad a cada origen y dificultado incorporar bases de datos, object
storage o APIs.

## Decisión

Cada origen implementa `DatasetReader.read(path, options, limit)` y devuelve un
`DatasetReadResult`. El registro de lectores resuelve el adaptador mediante
firmas fuertes cuando existen, contenido JSON, extensión y, como último recurso,
un delimitador reconocible. Agregar una fuente nueva exige registrar otro
adaptador; profiling, ArtifactStore, ExecutionPlanner y reglas conservan la
misma entrada.

El resultado común contiene:

- un `polars.DataFrame` con escalares representados como `String` o `Null`, sin
  trim, cambios de case, normalización Unicode ni transforms funcionales;
- formato, versión del lector y opciones efectivas no secretas;
- esquema nativo cuando la fuente lo ofrece;
- semántica de numeración de registros y metadata específica acotada.

El esquema nativo actúa como pista estructural para distinguir INT64, DECIMAL,
BOOLEAN, DATE y TIMESTAMP, incluso si una columna Parquet sólo contiene nulls.
El tag `IDENTIFIER` y los overrides explícitos siguen teniendo prioridad. El
perfil se calcula sobre los valores normalizados estructuralmente, conservando
su representación observada; las reglas reciben siempre el mismo modelo y no
conocen el formato original.

## Adaptadores locales

- CSV conserva UTF-8 y la política histórica de coma/punto y coma.
- TXT/TSV detecta coma, punto y coma, tabulación o barra vertical; el usuario
  puede fijar un delimitador de un carácter seguro.
- XLSX valida el paquete, rechaza macros, enumera hojas y procesa la hoja elegida
  en modo de sólo lectura. Las fórmulas se conservan como texto observado.
- JSON acepta un arreglo de objetos, un objeto individual, un objeto con un
  único arreglo tabular y JSON Lines. Los objetos anidados se aplanan con rutas
  separadas por punto; listas internas se conservan como JSON compacto.
- Parquet usa el esquema embebido. Antes de decodificar filas consulta el footer
  con DuckDB y rechaza exceso de filas, columnas o tamaño total descomprimido;
  después lee sólo la muestra solicitada durante la inspección.

La inspección previa usa hasta 100 registros. La carga está limitada a 10 MiB,
100.000 filas y 100 columnas. El archivo recibido se registra como
`ORIGINAL_UPLOAD`; el frame común se persiste además como `CANONICAL_PARQUET`.
`DatasetVersion.ingestion_metadata`, añadido por Alembic
`0003_dataset_ingestion_metadata`, conserva lector, formato, hoja o delimitador,
esquema nativo y numeración. Las versiones históricas reciben `{}` y usan el
adaptador legacy sin reescribir sus datos.

El MIME del `ORIGINAL_UPLOAD` procede del lector que identificó el contenido.
La descarga elige una extensión compatible con ese MIME, aunque el archivo
recibido tuviera un nombre engañoso.

## Evolución

El puerto `DatasetSource` y su adaptador `LocalFileDatasetSource`, descritos en
[ADR 0006](0006-architecture-ports.md), separan la adquisición del registro de
lectores. Un lector interpreta un formato; una fuente puede adquirir datos de
un origen remoto y entregar la misma estructura común.

Un conector PostgreSQL, SQL Server, S3, Azure Blob o API deberá resolver sus
credenciales fuera de `reader_options`, aplicar límites en el origen y devolver
el mismo `DatasetReadResult`. La adquisición puede pasar a trabajos asíncronos
sin cambiar los contratos del motor. Las opciones persistidas nunca incluyen
passwords, tokens ni cadenas de conexión.
