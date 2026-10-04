# ADR 0025 — Correcciones C01–C06 de 0.7.0

Estado: aceptada. Fecha: 4 de octubre de 2026. Baseline diagnosticada:
`12ca7061696d3581a18237dc7737348a3462e2c4`, rama `feat/local-prototype`.
La solicitud del ciclo amplía expresamente el contrato de adquisición XLSX.
Las garantías de publicación, identidad, permisos y commit remoto siguen vigentes.

## Causas y evidencia inicial

Los originales de Excel no están disponibles en las ubicaciones autorizadas.
No se atribuye retrospectivamente una causa a adquisiciones del usuario. La
reproducción sintética de 100001 filas (760864 bytes ZIP, 7794405 bytes expandidos)
falla antes del primer lote por el límite heredado de 100000 filas. Un XML con
57 registros y dimensión declarada `A1:A1` produce cero registros en el lector
anterior. La implementación anterior usa filas/columnas completas y `openpyxl`
precarga shared strings y estilos incluso en modo `read_only`.

En C02, el worker transforma un `ProcessingError` sin código estructurado en
`ACQUISITION_INVALID_DATA`. La UI no consulta límites efectivos por formato y
ruta. En C03, la adquisición usa texto libre y el área predeterminada, mientras
la carga rápida usa otro control; reutilizar un dataset puede mostrar
Operaciones aunque su dominio sea otro. En C04, las zonas vacía, parcial
`America/Bogot` e inválida lanzan `RangeError` antes de guardar. Reabrir edición
también sustituye el inicio por la hora actual. En C05, la resolución de fuente
invoca `dataset_paths` bajo el lock de automatización; la planificación Sentinel
también abre el descriptor. En C06 falta un setter y una acción para no leída.
Los resultados completos posteriores se conservan separadamente del diagnóstico.

## C01: contrato incremental de XLSX

El worker interpreta ZIP/OOXML con SAX y chunks de 64 KiB. Comprueba el directorio
central antes de construir `ZipFile`, limita miembros/metadatos/estilos, rechaza
macros, rutas inválidas, paquetes divididos y DTD/entidades. Cuenta la expansión
real al leer XML. Shared strings y líderes de fórmulas compartidas usan un índice
SQLite privado con cachés limitadas; el worksheet nunca se materializa completo.
La inferencia conserva estados por columna y abarca todas las filas.

Se ignoran las dimensiones declaradas como total. La primera fila no vacía es
encabezado; las filas que sólo tienen formato no son registros. Se preservan la
hoja elegida, la fila física de encabezado y `__tv_record_number` original. Las
fórmulas se observan como texto, sin evaluación; las fechas respetan estilo y
epoch 1900/1904. El índice y partes privadas pertenecen al intento y se limpian
con sus reglas de propiedad. Cancelación, deadline y lease se comprueban durante
indexación/lectura; la publicación completa reutiliza AcquisitionRun, Job,
StorageProvider y su fence final. No se publica una muestra o versión parcial.

El recorrido HTTP recibe el ZIP e inspecciona metadata/muestra con presupuesto
independiente: 100 registros, 4 MiB XML y 5 segundos. Una referencia compartida
que excede ese presupuesto devuelve `inspection_limited`, total desconocido y
permite registro sin overrides cuando el encabezado no se conoce. En ese caso,
overrides no vacíos producen `ACQUISITION_INSPECTION_LIMITED`; la resolución y
validación completa de población corresponden al worker.

Los defaults son 1000000 filas de datos, 1 GiB comprimido, 4 GiB expandido,
100 columnas, 100000000 celdas materializadas, 64 KiB por celda, 1 MiB por
registro, lotes de 5000 filas/8 MiB, 8 MiB metadata, 8 MiB caché, 4096 miembros,
65536 estilos y 8 GiB temporales. Se aplican además 2 GiB observados, reserva
512 MiB, perfil DuckDB 256 MiB y deadline 1800 s. Presupuesto analítico/caché no
es límite de todo el RSS; cgroup y mediciones de proceso se informan aparte.
Excel permite 1048576 filas físicas incluyendo encabezado: la cota efectiva
es el mínimo del límite de datos y el espacio físico restante. No se anuncian
5 millones de filas XLSX. JSON no lineal y carga rápida conservan sus cotas.

## C02: diagnósticos y límites efectivos

Errores nuevos transportan código estable, mensaje público, detalles de límites
y referencia diagnóstica. Las dos columnas nullable de 0016 no reescriben errores
históricos. Sólo formas legacy reconocidas tienen un mapeo seguro; no se publica
el texto arbitrario de una excepción. Se distinguen filas, bytes comprimidos y
expandidos, celda/registro, columnas, hoja, estructura, formato, tiempo, disco,
memoria y permisos. API, historial y avisos conservan la misma causa pública.

`GET /api/v1/acquisitions/limits?format=XLSX&route=ASYNC_ACQUISITION` devuelve
el descriptor efectivo del backend. `LEGACY_UPLOAD` describe la carga rápida.
La UI consulta antes de enviar y sólo rechaza anticipadamente excesos fiables,
como tamaño conocido. Transferencia, registros materializados y versión
publicada son magnitudes separadas. Cero registros durante indexación no significa
una hoja vacía; superar una cota no revela el total de una población no leída.

## C03 y C04: formularios

Un selector compartido conserva las cuatro áreas iniciales y agrega dominios
existentes de la organización, ordenados y deduplicados. Agregar nueva área
valida 1..80 caracteres. Reutilización/nueva versión muestra el dominio real y
no modifica etiquetas históricas; persiste `Dataset.domain` sin modelo nuevo.

La zona horaria editable es un draft validado antes de formatear o convertir.
Vacía/parcial/inválida muestra error y bloquea guardar, conservando otros campos
e instante previo. Crear, editar y reabrir usan la zona válida explícita y el
`starts_at` guardado. Editar campos de negocio conserva el ancla UTC y el cursor next_run_at, incluso si la fecha ya pasó; una ancla nueva pasada se rechaza. Cambiar el calendario calcula el siguiente slot futuro y no rearma ONCE consumido. La política DST no cambia: hora inexistente se omite,
ambigua usa fold=0; no se inventa UTC ni zona del navegador como fallback.

## C05: responsabilidades del despacho y worker

Scheduler, CHAINING, manual y planificación transitiva consultan exclusivamente
metadata persistida: organización, actores/permisos, publicación, identidad,
conteos, hashes registrados, revisiones y políticas. Run/ocurrencia fijan
DatasetVersion, configuración y destino exactos; CHAINING usa exactamente
`Intake.output_version_id`. UNIQUE/claims/guard del target evitan repetición y
solapamiento. Metadata faltante bloquea con diagnóstico; no estima bytes de
población a partir del tamaño de un descriptor.

El worker compara las identidades congeladas y verifica descriptor, todas las
partes, tamaños, hashes, esquema, conteos y spool antes de `DeliveryAttempt.STARTED`.
Las lecturas caras ocurren fuera de transacciones/locks prolongados de metadata.
Revalida identidad y permisos bajo el fence final. Corrupción/desaparición después
de despacho termina `FAILED_PRECONDITION` antes de DDL/DML; no sustituye hashes ni
elige otra versión. `StorageProvider.dataset_paths` conserva verificación global
completa; no hay bypass/cache que convierta metadata en autorización de escritura.
PreparedRows, autenticación, leases, cancelación, única transacción remota y
UNKNOWN/PENDING_REPAIR conservan sus garantías.

## C06: setter personal de no leída

`POST /api/v1/notifications/inbox/{id}/unread` fija `read_at=null` idempotentemente.
Emite UPDATE explícito para que una lectura concurrente no sobreviva por caché ORM. Exige misma organización, destinatario y `notifications:read`, además de permisos
actuales del recurso enlazado. Administrator no accede a otra bandeja personal.
La UI actualiza lista/filtros/contador tras confirmación y muestra errores sin
mutación optimista. Recarga/logout/reinicio conservan el estado. No crea Run,
outbox, segunda entrega ni auditoría de negocio ficticia.

## Certificación y operación

El generador independiente `scripts/xlsx_fixtures.py` produce inline/shared strings
de alta cardinalidad, IDs, Unicode, null/vacío, fórmulas, fechas, encabezado físico 4,
dimensiones falsas y cambio de tipo después de 100k. Su oráculo decodifica todos
los valores por otro camino; detecta alteraciones con conteo idéntico. El gate
`xlsx-corrections-e2e` ejecuta 100k/100001/400k/1M, integridad completa, PySpark,
Delivery SQL, navegador, cancelación, crash/lease y backup/restore reales. El mismo gate ejecuta cuatro programaciones reales sobre dos datasets/versiones distintos de 1 M y targets distintos mientras una quinta entrega ocupa el worker; mide setup, dispatch y espera de cola separadamente.
Resultados, métricas y estado se derivan de ejecuciones efectivas; no se heredan
los verdes de la publicación inicial. CI final debe corresponder al SHA documental
final, incluido PDF. Upgrade principal sólo después de todos los gates, con
despacho detenido controladamente, jobs del usuario finalizados, backup verificado,
mismo proyecto/volúmenes/puerto/secretos y aplicación exclusiva de 0016.

Referencias primarias: [límites Excel](https://support.microsoft.com/es-es/excel/excel-specifications-and-limits),
[lector openpyxl](https://openpyxl.pages.heptapod.net/openpyxl/_modules/openpyxl/reader/excel.html),
[inicialización React](https://react.dev/reference/react/useState),
[Intl DateTimeFormat](https://tc39.es/ecma402/).
