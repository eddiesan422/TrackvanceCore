# Contrato y operación de Reportes 0.8.0

Este documento describe el backend implementado en 0.8.0. La decisión técnica está
en [ADR0027](../adr/0027-reports-frozen-context-sandbox.md); gobierno y aprobación
estricta en [la guía de catálogo](catalog-governance-0.8.0.md). El estado de
certificación de volumen, recuperación y CI se publica por separado.

## API, permisos y secuencia

Todos los endpoints se encuentran bajo `/api/v1/reports`, requieren sesión de la
organización y aplican CSRF en POST conforme al contrato de autenticación nativo.
Los permisos de las acciones son independientes.

| Método y ruta | Permiso | Resultado |
|---|---|---|
| GET `/limits` | `reports:read` | Perfiles efectivos y capacidades SQL/exportación |
| GET `/sources?search=&offset=0&limit=50` | `reports:read` | Identidades INPUT/contrato/revisiones y candidato OUTPUT |
| POST `/resolve` | `reports:read` | Contexto conjunto de quince minutos |
| GET `/definitions`, GET `/definitions/{id}` | `reports:read` | Definiciones y revisiones inmutables de la organización |
| POST `/definitions` | `reports:write` | Definición con revisión 1 |
| POST `/definitions/{id}/revisions` | `reports:write` | Nueva revisión mediante expected_version/CAS |
| POST `/preview` | `reports:preview` | Hasta diez filas del JOIN real y diagnóstico |
| POST `/download` | `reports:download` | Stream CSV o XLSX |
| POST `/datasets` | `reports:generate` | Ejecución durable admitida en JobQueue REPORT |
| GET `/executions`, GET `/executions/{id}` | `reports:read` | Historial propio, estado, fuentes, métricas y salida |
| POST `/executions/{id}/cancel` | `reports:generate` | Solicitud de cancelar un DATASET propio |

El usuario necesita además acceso efectivo al contenido de cada fuente y sus
dependencias. Roles/grants no se deducen del botón de la UI. La selección congelada
no concede acceso si luego cambia un permiso o bloqueo.

Primero consultar fuentes; construir draft; resolver; usar el mismo context_id
para vista previa, descarga o generación. Repetir resolve constituye una nueva
selección. El histórico de PREVIEW/DOWNLOAD contiene metadatos, nunca las filas.

## Draft, fuentes y filtros

POST `/resolve` recibe `{ "draft": ReportDraft, "revision_id": null }`. La revisión
es opcional y sólo debe enviarse si coincide la estructura guardada; pueden variar
los valores de parámetros manteniendo nombres y tipos declarados.

Ejemplo guiado de dos fuentes (IDs ilustrativos, reemplazar por la selección real):

```json
{
  "mode": "GUIDED",
  "sources": [
    {"alias":"ventas","input_dataset_id":"input-a","contract_id":"contract-a","contract_revision_ids":["revision-a"],"policy":"LATEST_APPROVED"},
    {"alias":"clientes","input_dataset_id":"input-b","contract_id":"contract-b","contract_revision_ids":["revision-b"],"policy":"SPECIFIC","input_version_id":"input-b-v2"}
  ],
  "joins": [{"left_alias":"ventas","right_alias":"clientes","type":"LEFT","keys":[{"left_column":"cliente_id","right_column":"id"}],"expected_cardinality":"N:1","allow_many_to_many":false}],
  "columns": [{"source_alias":"ventas","column":"cliente_id","alias":"cliente"},{"source_alias":"ventas","column":"importe","alias":"importe"},{"source_alias":"clientes","column":"nombre","alias":"nombre"}],
  "source_filters": {"ventas":{"operator":"AND","conditions":[{"column":"importe","operator":"GE","value":"10.00"}]}},
  "post_filter": {"source_alias":"clientes","column":"nombre","operator":"IS_NOT_NULL"},
  "order_by": [{"source_alias":"ventas","column":"cliente_id","direction":"ASC"}],
  "parameters": []
}
```

Una fuente admite 1–100 revisiones diferentes de la misma identidad de contrato.
LATEST_APPROVED prioriza la versión INPUT estrictamente aprobada más reciente,
no la fecha de creación de OUTPUT. Una entrada posterior pendiente/rechazada
produce advertencia; una salida seleccionada inhabilitada no causa fallback.

Alias: letra inicial, letras/números/guion bajo, máximo 63, únicos sin distinguir
mayúsculas, sin prefijo reservado tv_. Máximo ocho fuentes y cien columnas de
salida. Las llaves JOIN admiten 1–16 parejas; cardinalidad declarada 1:1, 1:N,
N:1 o N:M. N:M observado necesita allow_many_to_many=true y conserva límites.

Los filtros pueden ser condición o grupo `{operator:"AND"|"OR", conditions:[...]}`
hasta ocho niveles, 1–100 condiciones por grupo. Condición:
`{source_alias?, column, operator, value?}`. source_alias es opcional sólo en un
filtro previo asociado a una fuente. Operadores EQ, NE, GT, GE, LT, LE, IN,
IS_NULL, IS_NOT_NULL. IN recibe lista de 1–100 valores; null se compara usando
IS_NULL/IS_NOT_NULL, no EQ null. La columna debe ser de la fuente, incluso en el
filtro posterior. Un filtro posterior sobre el lado opcional de un LEFT puede
eliminar las filas sin coincidencia: conserva exactamente semántica WHERE.

`expected_schemas` se guarda automáticamente en las definiciones: mapa alias→
lista `{name, logical_type, ...}`. Las columnas usadas, incluidas llaves/filtros,
deben conservar su tipo; columnas añadidas que no se utilizan son compatibles.

## SQL y tipos exactos

En modo SQL se conservan sources, joins como políticas, source_filters y parameters,
y se envía `sql`. Ejemplo:

```sql
SELECT ventas.cliente_id AS cliente, SUM(ventas.importe) AS importe
FROM ventas LEFT JOIN clientes ON ventas.cliente_id = clientes.id
WHERE ventas.importe >= $minimo
GROUP BY ventas.cliente_id
ORDER BY cliente ASC
```

Parámetro correspondiente:
`{"name":"minimo","type":"DECIMAL","value":"10.00"}`.
TEXT recibe string, INTEGER int o string de dígitos con signo y rango int64,
DECIMAL string finita hasta 38 dígitos, DATE/TIMESTAMP ISO y BOOLEAN boolean o
"true"/"false". null está permitido con un tipo declarado válido. Ningún valor se
concatena como SQL. Los parámetros declarados quedan protegidos dentro del contexto.

Se admite SELECT plano único sobre exactamente los alias seleccionados, columnas
calificadas, COUNT/SUM/MIN/MAX, DISTINCT, CASE, COALESCE, ABS, ROUND, CAST controlado,
filtros, GROUP BY, HAVING, ORDER BY, LIMIT/OFFSET. Sólo COUNT(*) admite asterisco.
CTE/subconsultas, UNION, tablas ajenas, JOIN cartesiano/NATURAL/USING/rango/OR,
DDL/DML, funciones de archivo/red, extensiones, reloj/aleatoriedad y casts float
se rechazan. Consulta <=64 KiB y AST <=4.000 nodos. Los cruces guiados y SQL se
validan sobre la misma población completa; LIMIT no elude cardinalidad.

Los datos canónicos se leen String/null y se convierten conforme al tipo lógico
registrado, después de validar toda la fuente: STRING, INT64, DECIMAL, DATE,
TIMESTAMP y BOOLEAN. DECIMAL usa precisión/escala hasta 38 sin redondear;
exceso falla. Fechas deben ser ISO, booleanos true/false y timestamps con una
política consistente de zona horaria. Llaves de distinto tipo se rechazan, aunque
parezcan visualmente iguales. No se normaliza texto ni se eliminan ceros iniciales.
Los cálculos que produzcan float no se entregan como exactos; para promedios usar
SUM y COUNT y decidir explícitamente la precisión posterior. AVG decimal produce
diagnóstico específico porque DuckDB retorna float.

## Respuestas y estados

Resolve devuelve `{context_id, expires_at, sources, warnings, query_hash}`. Cada
fuente resuelta incluye alias; input_dataset_id/input_version_id/input_version;
contract_id/contract_revision_id; approval_run_id/approval_finished_at;
output_dataset_id/output_version_id/output_version; schema/schema_hash;
canonical_artifact_id/canonical_sha256/canonical_size_bytes; row_count/policy y
governance vigente. La API sources ofrece además contract_revisions y versiones
INPUT candidatas; sus columnas usan `logical_type`. Navegar no escanea filas.
Los contratos se paginan en SQL por identidad raíz. revision_offset y version_offset
permiten páginas de cien revisiones/versiones por tarjeta; se devuelven total,
offset y limit de cada historia. Sólo las revisiones visibles explícitamente
admitidas entran en su selección. El candidato se evalúa bajo esas revisiones.
contract_id opcional limita el listado a la identidad raíz solicitada para
recargar una tarjeta y paginar sus historias sin traer las demás.

PREVIEW recibe `{context_id}` y devuelve ejecución, columnas, rows, cardinality y
warnings. Decimal se representa como string exacta; fecha/hora ISO; entero que
excede el rango seguro JavaScript ±(2^53−1) se entrega como string con tipo lógico
en columns. Otros enteros, boolean y null conservan su tipo JSON. Es muestra real de hasta diez filas:
`sample=true`, total_rows desconocido. No implica total del reporte ni aprobación
de calidad. Un fallo de recurso puede incluir allow_generate=true; un fallo de
acceso/SQL/cardinalidad requiere corregir su causa.

DOWNLOAD recibe `{context_id, format:"CSV"|"XLSX"}` y devuelve stream con la
identidad de ejecución en headers. No hay URL de artefacto persistente. Los límites
o revocaciones que ocurran después de iniciar el stream lo interrumpen; una descarga
parcial no tiene SUCCESS. generation_status distingue COMPLETE de la generación;
transmission_status COMPLETE sólo se persiste después del último send ASGI exitoso.
No significa que el navegador guardó el archivo. Desconexión termina el hijo y
preserva un error de generación previo.

Execution DTO: `{id, context_id, profile, status, generation_status,
transmission_status, progress_stage, progress_percent, metrics, sources,
output_version_id, output_dataset_id, error_code, error_message, started_at,
finished_at, created_at}`. profile es PREVIEW/DOWNLOAD/DATASET (XLSX utiliza la
ejecución DOWNLOAD). Estados principales QUEUED, RUNNING, SUCCESS, FAILED,
CANCELLED, INTERRUPTED. Porcentajes expresan etapas; filas/bytes observados son
métricas reales, no una estimación de total desconocido.

Definición GET: `{id,name,description,owner_user_id,version,active,revisions}`;
revisions se ordena descendente y cada elemento contiene id/version/draft/query_hash/
created_at. Guardar recibe name/description/draft; nueva revisión recibe
expected_version/draft. Conflicto de nombre o CAS devuelve 409. Una revisión
existente no se sobrescribe. Listados usan items/total/offset/limit, máximo 100.
El listado de definiciones incluye sólo la revisión vigente; el detalle pagina
revisiones con revision_offset/revision_limit (default 50, máximo 100) e incluye
revision_total/revision_offset/revision_limit. Ningún detalle descarga la historia
completa por defecto.
selected_revision contiene la vigente independientemente de revision_offset.
revision_id opcional solicita una revisión exacta en selected_revision sin alterar
la página de revisions. La revisión debe pertenecer a esa definición y organización;
un ID ajeno o inexistente devuelve 404 y nunca sustituye la revisión por la vigente.

## CSV y XLSX

CSV es UTF-8, RFC4180, todos los campos entre comillas y CRLF. Su contrato
`UTF8_RFC4180_QUOTED_NULL_BACKSLASH_N_ESCAPED_PREFIXES_FORMULA_APOSTROPHE_V1`
preserva null y cadena vacía y evita fórmulas tanto en encabezados como celdas:

| Valor original | Valor del campo CSV después de leer RFC4180 |
|---|---|
| null | `\N` |
| cadena vacía | cadena vacía |
| texto que comienza con barra invertida | se añade una barra invertida inicial |
| texto que comienza con apóstrofo | se añade un apóstrofo inicial |
| texto que, tras espacios/tab/CR/LF, comienza por =, +, -, @ | se añade un apóstrofo inicial |
| otros textos y valores escalares | representación exacta/ISO/true/false |

Para decodificar: campo exactamente `\N`→null; campo que empieza por dos barras
invertidas→eliminar una; campo que empieza por apóstrofo→eliminar uno; los demás
se conservan. La interpretación como número/fecha necesita el esquema registrado.
Una planilla que infiera tipos puede transformar ceros iniciales; usar XLSX para
celdas tipadas que conserven identificadores. No se promete preservar el tipo de
una celda si se abre un CSV mediante inferencia automática de Excel.

XLSX produce OOXML/ZIP incremental sin archivos temporales. STRING y DECIMAL son
inlineStr; int con más de 15 dígitos también texto; otros enteros y booleanos usan
sus tipos correspondientes. Fechas/hora son texto ISO. null es ausencia de celda;
cadena vacía tiene una celda inlineStr explícita. Texto parecido a una fórmula
permanece texto, nunca `<f>`. Excel limita 32.767 caracteres por celda y no admite
ciertos controles XML; esos casos fallan con una alternativa CSV visible.

## Generar dataset y recuperación

POST `/datasets` recibe context_id, idempotency_key (8–100), name (1–160),
description y opcionalmente macro_domain_id/domain_id, owner, criticality,
business_owner_id/steward_id/technical_custodian_id e information_classification.
criticality: LOW/MEDIUM/HIGH/CRITICAL. Clasificación: UNKNOWN/PUBLIC/INTERNAL/
CONFIDENTIAL/RESTRICTED. El par de dominio y sus responsables se valida; clasificación
parcial se conserva como parcial. La clasificación efectiva no rebaja las fuentes,
incluido UNKNOWN. Un nombre existente no se sobrescribe.

Se devuelve una ejecución durable para consultar en `/executions/{id}`. Repetir
la misma clave y payload devuelve esa ejecución; reutilizarla con otro payload
falla. No crea doble Job ni dataset ante reintento de HTTP. El worker comparte
admisión global, obtiene lease y prepara partes String/null y perfil íntegro.
StorageProvider publica descriptor multipart y artefactos inmutables. Bajo un
fence de lease vigente confirma Dataset nuevo, versión 1 REPORT_OUTPUT READY,
gobierno, dependencias, ArtifactLink y SUCCESS en una sola transacción de metadatos.
La calidad queda PENDING_VALIDATION y requiere un nuevo Intake propio.

Linaje: REPORT_EXECUTION→DATASET_VERSION con REPORT_SOURCE; →RUN con REPORT_APPROVAL;
→REPORT_REVISION con REPORT_REVISION si guardada; →versión/artefacto con REPORT_OUTPUT;
versión derivada→cada versión fuente con DERIVED_FROM. DatasetSecurityDependency
protege transitivamente descendientes. Bloquear una fuente impide usar la salida
derivada aunque tenga artefactos propios.

Staging exclusivo: `report-staging/<execution>/<attempt>-<owner>`, incluido spill.
Los intentos perdidos no publican. Tras rollback o recuperación se eliminan sólo
candidatos conocidos sin Artifact confirmado. Un backup excluye staging y conserva
metadata, fuentes, artefactos permanentes y linaje. Consultar las comprobaciones
de [ADR0028](../adr/0028-isolated-certification-recovery-upgrade-080.md) para restore.

## Límites y entorno

`GET /limits` muestra lo efectivo; estas cifras son defaults finitos, no una
afirmación de certificación de rendimiento:

| Perfil | Tiempo | Filas de resultado | Bytes | Spill |
|---|---:|---:|---:|---|
| PREVIEW | 60 s | 10 máximo duro | 8 MiB | Prohibido |
| DOWNLOAD CSV | 180 s | 100.000 | 128 MiB | Prohibido |
| DOWNLOAD XLSX | 180 s | 50.000 | 64 MiB | Prohibido |
| DATASET | 1.800 s | 5.000.000 | 2 GiB | Sólo intento, hasta 2 GiB |

Variables comunes: REPORT_THREADS=2, REPORT_MEMORY_MB=512,
REPORT_PROCESS_MEMORY_MB=2048, REPORT_BATCH_ROWS=512, REPORT_BATCH_BYTES=8388608,
REPORT_MAX_JOIN_ROWS=5000000, REPORT_MAX_JOIN_EXPANSION=100, REPORT_CONCURRENCY=2,
REPORT_DATASET_TEMP_BYTES=2147483648. Cada perfil admite
REPORT_<PROFILE>_TIMEOUT_SECONDS/MAX_ROWS/MAX_BYTES, incluido XLSX. PREVIEW nunca
supera diez filas aunque se configure más. Memoria de proceso debe superar la
del motor y lotes no exceder diez mil filas. Concurrencia es global entre API y
workers, con exclusión transaccional, no sólo un semáforo por proceso.

Usar Linux con Landlock ABI>=3, libseccomp2 y runtime DuckDB/SQLGlot del lock.
El contenedor debe tener presupuesto adicional para coordinador/exportador,
cgroup de memoria/CPU/pids finito, cap-drop ALL y no-new-privileges. No habilitar
privileged o SYS_ADMIN para hacer funcionar el ejecutor. Un kernel sin controles
devuelve REPORT_SANDBOX_UNAVAILABLE; un proceso ya multithread antes de aislarse
devuelve REPORT_SANDBOX_THREADS. Ambos casos fallan sin ejecutar una consulta.
El hijo importa `_duckdb` después de instalar las políticas; su primera conexión
recibe threads/memoria explícitos. No inicializa los tipos DB-API del paquete
público, que crean una conexión por defecto sin esos límites. El cargador usa
exclusivamente `sys.base_prefix/lib`, derivado del runtime, sin heredar su valor
del proceso coordinador.
Los límites públicos de CPU/memoria del cgroup propio se descubren desde
`/proc/self/cgroup` antes de instalar Landlock. Se permiten únicamente archivos
regulares de contadores concretos dentro de `/sys/fs/cgroup`, incluidas las rutas
anidadas del proceso en runners hosted; ningún directorio cgroup recibe permiso
de lectura. Rutas relativas, traversal y symlinks fuera de ese filesystem se
rechazan. No concede acceso a procesos vecinos ni amplía los presupuestos.

Métricas `rows`, `bytes`, `elapsed_seconds`, `max_rss_bytes` pertenecen al motor
y canal. RSS máximo es del hijo, no pico agregado; tiempo comienza tras conexión.
`cpu_user_seconds` y `cpu_system_seconds` son deltas del hijo desde ese mismo
punto; no incluyen startup ni CPU del coordinador. `cgroup_memory_current_bytes`
es una lectura al terminar y `cgroup_memory_lifetime_peak_bytes` es el máximo de
vida del contenedor, que puede incluir otras ejecuciones. Un contador no
disponible se omite, no se convierte en cero ni se atribuye a una fase.
DATASET añade parts, canonical_size_bytes y cardinality; el progreso observa
rows_generated/bytes_prepared/parts_prepared/temporary_bytes_observed. Conteo
muestreado de staging no es un pico absoluto de disco. Los límites del contenedor
y evidencia de observación externa completan la medición. DATASET conserva
`query_seconds` (ejecutor, canal y escritura de partes) y `profiling_seconds`
separados, además de `disk_free_bytes_at_start`,
`disk_free_bytes_min_observed` y `temporary_bytes_sampled_max`. Las muestras
durante materialización no son un pico continuo de todas las fases.

## Pruebas y diagnóstico seguro

`test_reports_query_exports.py` prueba AST hostil y semántica CSV/XLSX exacta.
`test_reports_executor.py` requiere Linux y ejecuta el proceso real aislado:
oráculo Python independiente para outer joins, N:M completo, expansión, precisión,
archivos no seleccionados, red, variables y denegación de escritura durante
ejecución. Con strace instalado añade observación de coordinador/exportador/hijo
para PREVIEW, CSV, XLSX y fallo de recursos, sin mutaciones ni archivos resultado.
`test_reports_lifecycle.py` usa Intake y JobQueue reales hasta publicación y nueva
aprobación. Windows marca explícitamente las pruebas del aislador como no aplicables;
el gate Linux y la certificación aislada deben ejecutarlas.
La recuperación incluye caída real `os._exit` durante materialización, profiling
y preparación del descriptor. El spill de profiling y el JSON intermedio del
descriptor pertenecen a `report-staging/<execution>/<attempt>-<owner>`.
`StorageProvider.put_dataset` admite `temporary_parent` opcional, validado dentro
del proveedor antes de promover partes; Reportes pasa su staging existente.
La limpieza elimina únicamente el intento abandonado y conserva otro intento
activo y los artefactos comprometidos. Las llamadas ajenas a Reportes conservan
la ubicación previa cuando omiten ese parámetro.

`scripts/tests/reports_runtime_probe.py` permite observar el runtime confinado en
CI con datos sintéticos, sin revelar stderr o trazas. Reporta salida/señal, errno,
categorías de rutas y hashes; retorna cero para que pytest ejecute todos los gates.
La comparación reproducible `--force-default-cpu-count 192 --public-wrapper-baseline`
usa una copia privada del child para reproducir la antigua importación. Comparar
con `--force-default-cpu-count 192` bajo idénticos presupuestos AS512/1024MiB y
memoria del motor128/256MiB, sin alterar políticas ni código productivo.

`scripts/tests/reports_ephemeral_http.py` observa procesos reales de API/Nginx y
sus hijos durante resolve, PREVIEW y DOWNLOAD CSV/XLSX. Incluye éxito, límite de
recursos y desconexión; compara oráculos completos y el almacenamiento antes y
después. Rechaza intentos de escritura a archivos regulares, aunque se denieguen
o después se eliminen. Las imágenes de diagnóstico tienen strace y conservan
cap-drop ALL/no-new-privileges; no se usan como imágenes de instalación. Las
trazas privadas nunca se publican: sólo casos, códigos, contadores y hashes.
El proxy de `/api/v1/reports/` desactiva buffering de solicitudes/respuestas y
temporales de proxy; el gate comprueba esa configuración exacta.
Los cuatro Jobs de preparación (dos adquisiciones y dos Intake) deben estar
SUCCESS antes de detener sus workers; nunca se cancelan para iniciar la medición.
El probe real `/health/ready` se comprueba una vez antes del baseline, pues escribe
un marcador de disponibilidad por diseño. Durante el intervalo Docker usa el
endpoint real `/health` de sólo lectura. El observador no exime archivos `.ready`
ni permite escrituras de otros procesos dentro del intervalo.
La evidencia pública preserva los intentos fallidos y el resultado de los diez
casos en `docs/development/evidence/0.8.0/reports-ephemeral-http*.json`.

`scripts/tests/reports_postgres_snapshot.py` verifica la resolución conjunta real
contra dos nuevas aprobaciones Intake confirmadas en otra conexión entre la
lectura de la primera y la segunda fuente. Crea y elimina un esquema PostgreSQL
con UUID y un TemporaryDirectory exclusivos, sin tocar tablas o artefactos
existentes. El contexto RR conserva INPUT [1,1] y la resolución posterior elige
INPUT [2,2]. Ejecutar como proceso nuevo con DATABASE_URL de un PostgreSQL desechable:
`uv run python ../scripts/tests/reports_postgres_snapshot.py`, desde backend.
La salida JSON sanitizada acredita aislamiento, ausencia de mezcla y cleanup.

Los logs reales contienen identidades/códigos sanitizados, no filas, credenciales,
SQL o excepciones completas. No usar manifests privados de sesión/CSRF como
evidencia pública. Una vista previa limitada por recursos permite ofrecer DATASET;
jamás se amplían permisos, se quitan bloqueos, se omiten hashes o se desactiva el
aislamiento como recuperación automática.
