# Contrato Trackvance Core local 0.7.0

El esquema ejecutable se genera desde la aplicación y mantiene la base `/api/v1`
para compatibilidad. [openapi.json](openapi.json) se regenera y revisa como paso
documental separado. El snapshot 0.7.0 contiene 117 paths y corresponde a las rutas
instaladas. Regenerar con `uv run python ../scripts/export_contracts.py` desde
backend; no conecta a la DB. El contrato documenta cookie, CSRF, MIME, DTOs y errores.

Base `/api/v1`. Las listas usan `{items: [...], total: number}`, IDs string opacos
y fechas ISO UTC. Health/ready, auth/demo|login, auth/providers y los start/callback
Microsoft/Google son públicos. Los demás requieren cookie de sesión. El primer
acceso limita la sesión a /me, logout y change-password. Los aliases absolutos
`/health` y `/health/ready` también son públicos para diagnóstico y Compose. Las
mutaciones autenticadas exigen `X-CSRF-Token` devuelto al iniciar sesión. El proxy
Vite preserva la cookie; usar `credentials: 'include'`.

## Adquisición asíncrona 0.7.0

Las rutas legacy conservan sus respuestas 201 y límites pequeños. La UI normal
usa recepción 201 → inspección/opciones → adquisición 202.

| Método/ruta | Entrada y resultado |
| --- | --- |
| POST `/datasets/uploads/stage?filename=` | Cuerpo `application/octet-stream`; 201 con upload propio e id/filename/source_format/size_bytes/sha256/expires_at. Staging privado, bytes observados y hash; todavía no existe DatasetVersion. |
| GET `/datasets/uploads/{id}/inspect` | Opciones de lectura e inspección limitada con columnas/opciones/formato; no es el perfil completo. |
| POST `/datasets/{id}/acquisitions` | `{upload_id,reader_options,column_overrides}`; 202 con AcquisitionRun durable. `Idempotency-Key` es opcional: el mismo cuerpo devuelve la misma identidad y un cuerpo distinto produce 409. |
| POST `/connections/{id}/acquisitions` | `{name,schema_name,object_name,column_overrides}`; 202, con conexión/revisión/selección congeladas. |
| POST `/datasets/{id}/acquisitions/refresh` | Nueva adquisición SQL sobre la revisión vigente congelada al registrar; conserva versiones anteriores. |
| GET `/acquisitions?dataset_id=&status=&offset=&limit=` | Colección paginada del ámbito autorizado; estados QUEUED/RUNNING/SUCCESS/FAILED/CANCELLED. |
| GET `/acquisitions/{id}` | `stage`, `processed_rows`, `processed_bytes`, totales nullable, fechas, attempts, cancel_requested, output_version_id y error sanitizado. |
| POST `/acquisitions/{id}/cancel` | Cancela QUEUED o solicita cancelación cooperativa RUNNING; no publica una versión parcial. |

CSV/TXT/TSV, JSONL/NDJSON y Parquet se leen por lotes. JSON no lineal y XLSX
conservan límites efectivos de 10 MiB/100.000 filas. El cliente distingue
transferencia y procesamiento; no inventa porcentajes ni promete continuar una
transferencia incompleta después de cerrar el navegador. Nginx transmite el stage
binario sin buffering y la API mantiene autoridad sobre el límite efectivo.

## Ejecución, conjuntos y resultado completo 0.7.0

Los POST de Intake/Recon/Sentinel aceptan `requested_engine:'AUTO'|'POLARS'|'PYSPARK'`.
Omitirlo conserva AUTO y la idempotencia legacy. El preview del plan y la ejecución
incluyen origen, destino y referencias deduplicadas, con tamaño físico verificado
de las partes. Run.execution_plan schema 3 conserva motor/versión/master,
parámetros/presupuesto, reason/rejection y selección explícita; un fallo no cambia
silenciosamente de motor. `/system/engines` informa Java/PySpark reales,
modo/master/parameters/budget y heartbeats de tres lanes y tres procesos ligeros.
Un runtime instalado no certifica que el master Standalone remoto sea alcanzable;
la creación de una aplicación Spark comprueba esa conectividad.

estimated_working_set_bytes incluye los inputs y estimated_polars_evidence_bytes:
una estimación conservadora de filas × reglas habilitadas en Intake y detalles
por comparación en Recon, incluidos valores, parámetros y condiciones. No se
estima ese fanout usando la muestra. AUTO puede elegir Spark aunque el archivo
comprimido sea pequeño; POLARS explícito por encima del presupuesto queda en
FAILED_PRECONDITION. budget_is_estimate sigue distinguiendo la estimación de
los límites de memoria realmente impuestos al proceso.

El lote portable Recon Spark también reserva evidencia según cardinalidad real
del grupo y número de comparaciones. Una clave indivisible cuya evidencia
prevista excede la cota por grupo produce RESOURCE_RECON_GROUP_LIMIT antes del
kernel, sin cortar resultados; una agregación conserva su lineage completo.

`application/vnd.trackvance.parquet-set+json` representa un descriptor schema 1
PARQUET_DATASET, con esquema físico y partes ordenadas identificadas por ordinal,
artifact_id, path, sha256, size_bytes y row_count, además de totales. Cuando existe
un original conservado, DatasetVersion.sha256/size_bytes identifican ese original;
de lo contrario identifican el artefacto canónico, incluido su descriptor.
La metadata de ingestión conserva bytes canónicos/observados de la población para
planner/UI. Descargar el conjunto entrega un ZIP_STORED completo con el descriptor
exacto y las partes verificadas. Los archivos únicos legacy conservan sus bytes,
hashes y descarga anteriores.

GET `/runs/{id}/results` es paginado. GET `/runs/{id}/export.csv` recorre todas las
filas en disco, protege fórmulas, registra EXPORT_CSV/hash/EXPORT_OF y entrega un
FileResponse completo. Excel conserva límites de 100.000 filas, 500.000 celdas y
16 MiB de valores, además de las cotas del generador. Un 422 EXPORT_LIMIT_EXCEEDED
incorpora `complete_download` con el CSV completo, sin truncar. Los resultados y
el manifest tienen identidades propias.

## Preflight durable y publicación 0.7.0

POST `/delivery/validations` recibe DeliveryDraft y devuelve 202 con una Run
DELIVERY_PREFLIGHT. GET lista/detalle exige delivery:read y propiedad personal,
también para Administrator; POST cancel exige delivery:configure. El DTO expone
id/status/stage/dataset_id/source_version_id, fechas, error, cancel_requested,
draft_hash/draft/result. Configuration VALIDATION_PRIVATE no aparece entre las
configuraciones publicadas. El preflight no crea DeliveryAttempt ni ejecuta DDL/DML;
un SUCCESS técnico puede contener un resultado FAIL.

POST configuración/revisión admite `?validation_run_id=<id>`. Para cargas mayores
que SYNCHRONOUS_ROWS exige un resultado personal SUCCESS+PASS, con draft, hash y
origen exactos. Cambiar el draft invalida su reutilización. POST `/delivery/preflight`
conserva el recorrido legacy pequeño y devuelve 412 PREFLIGHT_ASYNC_REQUIRED para
cargas grandes. La ejecución repite permisos y drift y verifica la preparación
sellada, vinculada a fuente/configuración/destino/esquema/conteo, antes de STARTED.

## Automatización, ocurrencias y target 0.7.0

GET `/delivery/automations`, GET `/{id}` y GET `/{id}/occurrences` exigen
delivery:read. POST create y POST `/{id}/versions` exigen delivery:schedule con
dependencias transitivas. POST `/{id}/dispatch` exige delivery:execute y permite
una repetición visible y explícita. Settings incluyen mode
ONCE/INTERVAL/DAILY/WEEKLY/CHAINED, timezone, starts_at, source_policy
FIXED_VERSION/LATEST_REGISTERED/INTAKE_OUTPUT, allow_empty y allow_warnings,
además de campos específicos de horario, dataset e Intake. Las versiones fijan
Configuration y el User responsable. La ocurrencia conserva revisión,
trigger_key/origin/planned/dispatched/sourceRun/datasetVersion/Run/status/reason
y coalesced_intervals. La última versión debe estar ya registrada; no refresca SQL.

CHAINED usa output_version_id del Intake concreto con SUCCESS+APPROVED por defecto.
Los warnings requieren habilitación explícita; REJECTED y la salida vacía no
disparan una entrega por defecto. UNIQUE automation+trigger, idempotencia de la
solicitud y no repetición de entrada son garantías independientes. TargetGuard
coordina entregas manuales y automatizadas por fingerprint reconocido, con límites
para aliases/proxies/sinónimos; no identifica físicamente todos los targets.
UNKNOWN bloquea nuevas entregas hasta una revisión concluyente y decisión operativa.

POST `/delivery/runs/{id}/resume-target` recibe `{review_id,note}` y requiere
`delivery:review_unknown`. Registra decisión, actor y auditoría; conserva el UNKNOWN
histórico, no crea otra Run ni ejecuta DDL/DML. PENDING_REPAIR no autoriza reenvío.

## Bandeja personal y contratos históricos 0.7.0

GET `/notifications/inbox?module=&origin=&unread=&offset=&limit=` devuelve items
con id/module/origin/status/decision/description, resource_type/resource_id,
detail_url/created_at/read_at. GET `/notifications/unread-count` devuelve el conteo.
POST `/notifications/inbox/{id}/read` y POST `/notifications/inbox/read-all`
persisten la lectura. Todas estas rutas exigen notifications:read y el permiso
vigente del módulo; el ámbito usuario/organización también aplica al contador y
la lectura. Administrator no consulta una bandeja ajena. Un proceso manual
notifica al iniciador; scheduled/chain notifica al User responsable real.
SYSTEM no es destinatario. Los reintentos deduplican por evento y destinatario.

DELIVERY_PREFLIGHT enlaza `/delivery/validation/{id}` y muestra PASS/FAIL.
Intake SUCCESS+REJECTED se describe como rechazado. Delivery COMMITTED+PENDING_REPAIR
se diferencia de UNKNOWN. Los avisos no incluyen filas, valores de negocio,
secretos ni errores crudos del driver. Los endpoints deprecated `status`/`deliveries`
conservan HISTORICAL_ONLY para notification_deliveries de 0.6.0. No hay SMTP/Mailpit
ni envío de credenciales.

## Identidad y estado

- `GET /health` → `{status:'ok',version:'0.7.0',mode:'local-prototype',demo_enabled:true,demo_access_enabled:true,demo_seed_enabled:true}`; `demo_enabled` se conserva por compatibilidad y refleja `DEMO_ACCESS_ENABLED`.
- `GET /health/ready` → 200 con DB/storage/migrations listos o 503; alias absoluto `/health/ready` para Compose.
- `POST /auth/demo` cuerpo `{}` → sesión demo explícita (no password): `AuthenticationResponse={user:UserResponse,organization:{id,name},csrf_token,demo_mode:true}`; cookie HttpOnly `trackvance_session`. Requiere `DEMO_ACCESS_ENABLED=true`; si está deshabilitado devuelve 404 `DEMO_DISABLED`, con independencia de que existan datos demo.
- `POST /auth/login` `{username,password}` o `{email,password}` → AuthenticationResponse. El campo username acepta usuario o correo; se envía exactamente un identificador. `demo_mode` es true sólo para la cuenta demo.
- `GET /me` → AuthenticationResponse con UserResponse y permisos vigentes.
- `POST /auth/logout` → `{ok:true}`.
- `GET /dashboard?period=7d|30d|90d|all&dataset_id=&module=intake|recon|sentinel|DELIVERY&status=ATTENTION|HEALTHY|IN_PROGRESS|TECHNICAL_FAILURE&criticality=CRITICAL|HIGH|MEDIUM|LOW` → cockpit operativo limitado a la organización autenticada. `period` vale `30d` por defecto; todos los demás filtros son opcionales. Devuelve `{applied_filters,filter_options,period,stats:{datasets,total_rows,runs,open_exceptions,health_score,controls_failed,affected_datasets},variations,attention,attention_total,health_history,datasets_attention,recent_runs,module_status,activity,volume_history,organization_name,prototype:true}`. `attention` prioriza excepciones, hallazgos y ejecuciones por severidad/criticidad e incluye la ruta de acción. `recent_runs` puede incluir Delivery y sus métricas; `SUCCESS` conserva su significado técnico y `operational_status` expresa por separado si el resultado está sano o requiere atención. Las variaciones son `{previous,delta}` frente al período anterior o `null` cuando no existe una comparación válida.
- `GET /system/engines` → `{items:[{id,name,version,available,status,description}],worker:{status,last_seen,lane},workers:{DEFAULT:{...},DELIVERY:{...},ACQUISITION:{...}},components:{scheduler:{status},'events-chaining':{status},'events-notifications':{status}},limits:{max_upload_mb,max_rows,acquisition:{...},delivery:{...}},mode:'local-prototype'}`. `worker` conserva el heartbeat DEFAULT por compatibilidad.

## Datasets y rutas síncronas legacy

`Dataset = {id,name,description,domain,owner,criticality,status,created_at,version_count,row_count,column_count,latest_version_id,origin,origin_label,origin_source_type,updated_at}`. El origen se deriva de la última versión inmutable: `UPLOAD` se presenta como `MANUAL` / “Manual”, `INTAKE_OUTPUT` como `DATA_INTAKE` / “Data Intake” y los datasets sin versiones como `UNKNOWN` / “Sin versiones”. Tipos futuros de conectores conservan un código y una etiqueta legible.

`Version = {id,dataset_id,version,filename,source_type,sha256,schema_hash,size_bytes,row_count,column_count,profile_status,created_at,schema:[{name,logical_type,native_type?,nullable}],profile:{row_count,column_count,columns:[{name,logical_type,null_count,null_rate,distinct_count}]},ingestion_metadata:{reader:{key,version},source_format,format_label,reader_options,row_numbering,native_schema}}`.

- `GET /datasets` → lista Dataset.
- `POST /datasets` `{name,description?:'',domain?:'Operaciones',owner?:'Equipo de datos',criticality?:'HIGH'}` → Dataset 201. Un nombre ya usado en la organización devuelve 409 `DATASET_NAME_EXISTS` con el ID existente para orientar la carga de una versión nueva.
- `GET /datasets/{id}` → Dataset más `{versions:Version[]}` (recientes primero).
- `GET /datasets/{id}/schema` → esquema de la última versión desde el perfil
  persistido, con `{version_id,version,schema_hash,scan_mode,scanned_rows:0,
  columns:[{name,logical_type,native_type,nullable,semantic_tag,numeric}]}`.
  `?refresh=true` vuelve a consultar el footer del Parquet canónico y conserva
  los tipos lógicos inferidos; no recorre las filas del dataset.
- `POST /datasets/uploads/inspect` multipart `file` y `reader_options` JSON
  opcional → detección previa `{format,format_label,sheets,selected_sheet,
  detected_delimiter,columns,row_count?,sampled_rows,supported_formats}`. Usa
  como máximo 100 filas para la inspección y el esquema embebido de Parquet.
- `POST /datasets/{id}/versions/upload` multipart `file`, `column_overrides`
  JSON opcional y `reader_options` JSON opcional → Version 201. Admite CSV,
  XLSX, JSON/JSONL/NDJSON, Parquet/PQ y TXT/TSV delimitado, hasta 10 MiB,
  100.000 filas y 100 columnas. `reader_options` acepta `sheet_name` para
  Excel y `delimiter` para TXT; las opciones efectivas quedan en la versión.
  `column_overrides` asocia cada nombre con `logical_type` (`STRING`,
  `DECIMAL`, `INT64`, `DATE`, `TIMESTAMP` o `BOOLEAN`) y, opcionalmente,
  `semantic_tag: "IDENTIFIER"`. El override se valida contra el archivo y se
  persiste en el esquema inmutable de esa versión.
- `GET /dataset-versions/{id}/profile` → Version más `{sample:row[],sampled_rows,sample_bytes,sample_limited,sample_byte_limit,delivery_preflight_synchronous_rows}`. La muestra tiene como máximo 20 filas y 8 MiB de JSON UTF-8 observado; `sample_limited` indica que una fila excedería el límite de bytes. El perfil persistido sigue describiendo la población completa, aunque la muestra sea menor o vacía.

## Conexiones externas (solo lectura)

Las operaciones respetan organización, sesión y CSRF. `connections:read` permite
leer metadata; `connections:manage`, administrar conexiones; `connections:use`,
probar una conexión guardada, explorar datos y crear/refrescar snapshots.
Administrador y responsables de datos administran; Analista puede usar conexiones.
Los roles de consulta no pueden adquirir datos desde fuentes externas.

`Connection = {id,name,source_type,enabled,version,host,port,database,username,
options,connection_version_id,config_hash,last_test_status,last_test_at,
last_test_message,created_at,updated_at}`. Nunca incluye contraseña o referencia
al almacén de secretos. `source_type` es `POSTGRESQL` o `SQLSERVER` e inmutable.

- `GET /connections` y `GET /connections/{id}`: colección y detalle.
- `POST /connections/test`: `{name,source_type,host,port,database,username,
  password?,options?,connection_id?}`. Prueba borrador sin guardarlo; al editar,
  `connection_id` permite reutilizar la contraseña omitida solo si se conserva
  el destino, usuario y modo TLS de la versión vigente. Devuelve
  `{status:SUCCESS,message,tested_at}` o error sanitizado 422.
- `POST /connections`: mismos campos de conexión, sin `connection_id`; prueba
  en backend antes de guardar, crea configuración v1 y devuelve Connection 201.
- `PATCH /connections/{id}`: `{version,...campos modificados,enabled?}`; `version`
  es la revisión esperada. Password vacío/omitido reutiliza el secreto solo si no
  cambia el destino, usuario ni modo TLS. Crea una
  configuración nueva; revisión obsoleta devuelve 409. Deshabilitar no exige que
  la fuente esté disponible. No se permite modificar `source_type`.
- `DELETE /connections/{id}?version=<revisión>`: baja lógica; conserva configuraciones y snapshots. Versión obligatoria; si ya cambió, devuelve 409 `VERSION_CONFLICT`.
- `POST /connections/{id}/test`: prueba y persiste último estado SUCCESS/FAILED.
- `GET /connections/{id}/schemas`: `{items:[schema],total}` desde metadata.
- `GET /connections/{id}/objects?schema_name=`: `{items:[{name,kind}],total}`,
  `kind` TABLE/VIEW y solo objetos accesibles.
- `GET /connections/{id}/preview?schema_name=&object_name=&limit=20`:
  `{columns:[{name,native_type,logical_type,nullable,numeric,...}],rows,
  sampled_rows}`. Máximo 100 filas; valores observados texto/null. No expresa
  el total de filas de la tabla. Registra CONNECTION_PREVIEWED.
- `POST /connections/{id}/datasets`: `{name,domain?,description?,schema_name,
  object_name,column_overrides?}` → `{dataset,version}` 201. Crea binding y snapshot
  Parquet completo acotado, sin original_artifact_id inventado.
- `POST /datasets/{id}/refresh-source`: cuerpo vacío → Version 201; usa la
  configuración vigente y conserva las versiones previas.

`options` solo admite `connect_timeout` (1..15), `query_timeout` (1..60) y
`sslmode` (PostgreSQL: disable/require/verify-ca/verify-full) o `encryption`
(SQL Server: off/require). Defaults: 5s, 30s y require. No acepta SQL ni DSN.

Cambiar `host`, `port`, `database`, `username`, `sslmode` o `encryption` exige
proporcionar una contraseña explícita en la prueba de borrador o edición. En caso
contrario devuelve 422 `PASSWORD_REQUIRED_FOR_ENDPOINT_CHANGE` antes de abrir una
conexión externa. Cambiar nombre o timeouts permite reutilizar el secreto guardado.
Crear una conexión nueva siempre requiere contraseña; nunca se devuelve el secreto
ni se permite obtenerlo desde el formulario.

Las pruebas de borrador y guardadas registran `CONNECTION_TESTED` con actor estable y resultado, sin credenciales ni filas de negocio.

Dataset añade `source_binding` con conexión/schema/objeto y `connection_state=ACTIVE|DISABLED|DELETED`. La UI bloquea la adquisición cuando la fuente no está activa; la evidencia histórica permanece legible.
Version conserva `source_type` del motor y `ingestion_metadata.source` con
connection_id, connection_version_id, connection_version, config_hash, schema_name,
object_name, object_kind y captured_at. Run y evidencia heredan este linaje mediante
la versión del input. `row_numbering=SNAPSHOT_ROW` identifica registros del snapshot.
El tipo lógico `TIMESTAMP` exige offset y hasta seis dígitos fraccionales:
PostgreSQL `timestamptz` y SQL Server `datetimeoffset(0..6)` lo conservan;
`timestamp without time zone`, `datetime`, `datetime2`, `smalldatetime` y
`datetimeoffset(7)` se adquieren como `STRING` para no inventar una zona horaria
ni perder precisión.
Errores de fuente usan códigos SOURCE_AUTH_FAILED, SOURCE_PERMISSION_DENIED,
SOURCE_TIMEOUT, SOURCE_UNAVAILABLE, SOURCE_OBJECT_UNAVAILABLE o SOURCE_SIZE_LIMIT;
los mensajes no incluyen errores originales del driver ni credenciales.

## Data Delivery (escritura controlada)

Todas las rutas usan el prefijo `/delivery`. Los destinos pertenecen a la
organización autenticada y sólo admiten `POSTGRESQL` o `SQLSERVER`. La contraseña
se guarda en el almacén cifrado de destino y jamás aparece en respuesta, auditoría,
manifest o receipt.

`Destination = {id,name,sink_type,enabled,version,host,port,database,username,
options,destination_version_id,config_hash,last_test_status,last_test_at,
last_test_message,created_at,updated_at}`.

`options` admite `connect_timeout` 1..15, `query_timeout` 1..300 y
`sslmode=disable|require|verify-ca|verify-full` para PostgreSQL o
`encryption=off|require` para SQL Server. Defaults: 5 s, 60 s y `require`.

- `GET /delivery/destinations` y `GET /delivery/destinations/{id}` → colección y
  detalle. Requieren `destinations:read`.
- `POST /delivery/destinations/test` prueba un borrador
  `{name,sink_type,host,port,database,username,password?,options?,destination_id?}`
  sin persistirlo. `destination_id` permite reutilizar el secreto vigente sólo
  cuando el endpoint/usuario/modo de transporte sigue siendo compatible.
- `POST /delivery/destinations` con
  `{name,sink_type,host,port,database,username,password,options?}` → Destination
  201 y revisión v1. `PATCH /delivery/destinations/{id}` usa `version` esperado,
  crea revisión inmutable y rechaza conflictos.
  `DELETE /delivery/destinations/{id}?version=<revisión>` hace baja lógica.
- `POST /delivery/destinations/{id}/test` prueba la revisión guardada.
- `GET /delivery/destinations/{id}/schemas`,
  `/tables?schema_name=` y
  `/table-metadata?schema_name=&table_name=` descubren únicamente objetos
  accesibles. Metadata incluye columnas, tipos nativos/lógicos, nullability,
  defaults/identity/generated y restricciones relevantes. Requiere
  `destinations:use`; no acepta SQL libre.

Un borrador de entrega es:

```json
{
  "schema_version": 1,
  "dataset_version_id": "...",
  "destination_id": "...",
  "destination_version_id": "...",
  "target": {
    "mode": "EXISTING_TABLE",
    "schema_name": "public",
    "table_name": "orders",
    "create_schema": false
  },
  "columns": [
    {
      "source_name": "order_id",
      "target_name": "order_id",
      "target_type": "STRING",
      "ordinal": 0,
      "nullable": false,
      "length": 64
    }
  ],
  "write_strategy": "UPSERT",
  "upsert_keys": ["order_id"]
}
```

`target.mode` es `EXISTING_TABLE|CREATE_TABLE`; tipos lógicos:
`STRING|INT64|DECIMAL|DATE|TIMESTAMP|BOOLEAN`; estrategias:
`CREATE_AND_LOAD|APPEND|OVERWRITE|UPSERT`. `CREATE_AND_LOAD` exige tabla nueva;
las otras estrategias exigen tabla existente. `UPSERT` requiere claves explícitas
en el mapping, sin nulls/duplicados en la fuente y respaldadas por PK/unique en
el target. `target_type` debe coincidir con el `logical_type` inmutable de la
columna en la DatasetVersion; Delivery no convierte STRING a número, fecha,
timestamp o booleano ni cambia entre familias lógicas. `DECIMAL` admite
`precision`/`scale`; `STRING`, `length`.
Para tablas existentes, `DECIMAL` sólo acepta familias exactas `numeric`/`decimal`
y `money` con escala inspeccionable; `real`/`float` se rechazan. El preflight
comprueba escala y capacidad de dígitos enteros, no sólo precisión total.
`STRING` sólo acepta `text`/`varchar` en PostgreSQL y `nvarchar` en SQL Server;
la longitud se mide en unidades UTF-16 y texto suplementario exige collation
`_SC`/`_UTF8`. `TIMESTAMP` requiere offset explícito y precisión máxima 6; las
tablas nuevas usan `timestamptz(6)`/`datetimeoffset(6)`. Tipos nativos ambiguos,
fixed/non-Unicode, NUL, surrogates no emparejados y pérdida de precisión se
rechazan antes de crear `STARTED`.

La ejecución prepara todo el payload localmente y luego bloquea una tabla
existente aun con cero filas. PostgreSQL revalida la constraint UPSERT nombrada y
rechaza `OVERWRITE` con RLS activa. SQL Server exige `SELECT` para targets
existentes y rechaza `IGNORE_DUP_KEY`; `OVERWRITE` exige además `VIEW DEFINITION`
y rechaza FILTER security policies.
Estas son precondiciones de ejecución además de checks de preflight.

- `POST /delivery/preview?limit=8` con el borrador → muestra acotada de filas de
  origen y destino después del mapping; no escribe.
- `POST /delivery/preflight` con el borrador → `{status:'PASS',checks,source,
  destination,target,system_audit}` o error sanitizado. Valida artifact/hash, versión exacta
  del destino, mapping/tipos, target, restricciones, permisos y claves UPSERT.
- `GET /delivery/configurations` → configuraciones Delivery publicadas.
- `POST /delivery/configurations` añade `name`, `owner` y `description` al
  borrador → snapshot 201. Ejecuta preflight antes de publicar.
- `POST /delivery/configurations/{id}/versions` publica un sucesor inmutable del
  snapshot indicado y vuelve a ejecutar preflight.
- `GET /delivery/runs` → runs Delivery.
- `POST /delivery/runs` `{configuration_id,dataset_version_id}` con
  `Idempotency-Key` → Run 202 en lane `DELIVERY`. Repite preflight en el worker
  inmediatamente antes de escribir.
- `GET /delivery/runs/{id}/attempts` → `{items:[DeliveryAttempt],total}`.
- `GET /delivery/runs/{id}/receipt` → JSON descargable sólo tras un commit
  confirmado y con `delivery:read` además de `artifacts:download`.

`DeliveryAttempt` conserva `{id,run_id,destination_version_id,attempt_number,
idempotency_key,status,target_locator,rows_attempted,rows_written,rows_inserted,
rows_updated,bytes_sent,remote_reference,error_code,error_message,started_at,
finished_at}`. Sus estados son `STARTED|COMMITTED|FAILED|UNKNOWN`. `UNKNOWN`
indica que Trackvance no puede confirmar el commit remoto; no equivale a fallo ni
éxito y no se reintenta automáticamente. El operador debe verificar el destino y
decidir una ejecución explícita. Receipt y manifest no contienen secretos ni filas
completas y enlazan Run, DatasetVersion, DestinationVersion y DeliveryAttempt.

RBAC 0.6.0 separa destinations:read/use/manage de delivery:read/configure/execute/overwrite/alter_target/review_unknown/repair_evidence. Receipt añade artifacts:download. La matriz exhaustiva y los controles adicionales por módulo/recurso están en [permission-matrix.md](../docs/development/permission-matrix.md).

## Configuraciones y ejecuciones

`Configuration = {id,name,module,version,dataset_id,dataset_name,target_dataset_id,target_dataset_name,owner,description,status,config,created_at,latest_run:Run|null}`.

`Run = {id,run_id,job_id,module,name,status,decision,config_id,dataset_version_id,target_version_id,dataset_name,created_at,started_at,finished_at,progress_percent,progress_stage,metrics,execution_plan,error,output_version_id}`. `module` también puede ser `DELIVERY`; su estado puede quedar `UNKNOWN` cuando no existe confirmación concluyente del destino.

- `GET /intake/contracts`, `GET /recon/controls`, `GET /monitors` → lista Configuration.
- `POST /intake/contracts` `{name,dataset_id,owner?,description?,config:{required_columns:['pedido_id'],unique_columns:['pedido_id'],numeric_columns:['valor'],positive_columns:['valor'],max_error_rate:0.05}}` → Configuration 201. Las columnas opcionales de config default a [] y tasa 0.
- `POST /recon/controls` `{name,dataset_id,target_dataset_id,owner?,description?,config:{key_columns:['pedido_id'],amount_column:'valor',tolerance:'0.01'}}` → Configuration 201. El shorthand conserva tolerancia Decimal; los nuevos snapshots tienen normalización explícita default NONE. Preferir comparison_rules para las capacidades nuevas.
- `POST /monitors` `{name,dataset_id,owner?,description?,config:{required_columns:['pedido_id'],null_columns:['pedido_id'],max_null_rate:0.05,max_volume_change_pct:15,max_age_hours:48}}` → Configuration 201.
- `GET /runs?module=intake|recon|sentinel|DELIVERY` → lista Run.
- `POST /intake/runs` `{contract_id,dataset_version_id}` → Run 202.
- `POST /recon/runs` `{control_id,source_version_id,target_version_id}` → Run 202.
- `POST /monitors/{id}/runs` `{dataset_version_id?:string}` → Run 202 (última versión si omitida).
- `GET /runs/{id}` → Run más `{findings:Finding[]}`. Para Delivery, el target permanece en `execution_plan` y los intentos se consultan en `/delivery/runs/{id}/attempts`. Poll hasta `SUCCESS|FAILED|UNKNOWN|CANCELLED|FAILED_PRECONDITION`. SUCCESS significa procesamiento terminado; calidad o resultado remoto vive en `decision`.
- `POST /runs/{id}/cancel` `{}` → Run.
- `GET /runs/{id}/results?classification=&offset=0&limit=50` → lista row. Alias `/intake/runs/{id}/errors`, `/recon/runs/{id}/results`.
- `GET /runs/{id}/evidence` → descarga JSON manifest (v1 histórico intacto o v2 nuevo); `GET /runs/{id}/export.xlsx` → informe Excel estructurado. `export.csv` es deprecated y se conserva por compatibilidad de clientes históricos.
- `GET /monitors/{id}/metrics` → lista `{run_id,observed_at,row_count,null_rate,health_score,status}`.

Intake metrics: `{total_rows,valid_rows,error_rows,warning_rows,error_count,acceptance_rate,rules:[{code,column,failed_count,status}],decision}`. Errores rows `{original_row_number,rule_code,column,received_value,severity,message,classification:'ERROR'}`.

Recon metrics: `{total_rows,source_rows,target_rows,matched,mismatched,source_only,target_only,duplicate_source,duplicate_target,invalid,match_rate,counts:{MATCH:n,VALUE_MISMATCH:n,...}}`. Result row `{key,classification,source_value,target_value,difference,tolerance,message,source_row,target_row}`. Valores moneda strings; ausencias null.

Sentinel metrics: `{row_count,null_rate,health_score,failed_checks,total_checks,checks:[{code,name,status,actual,expected,message}]}`. Results misma lista checks con classification PASS/FAIL.

## Excepciones y auditoría

`Finding = {id,run_id,title,code,severity,details,created_at,exception_id}`.

`Exception = {id,display_id,finding_id,run_id,origin_run_id,origin_run,configuration_id,configuration_name,configuration_version,validation_run_id,validation_run,validated_at,validation_evidence,technical_validation:{status,eligible,validated,can_resolve,reason,candidate_run_id,validation_run_id,validated_at,evidence},title,module,severity,state,owner,root_cause,resolution,administrative_reason,version,created_at,updated_at,events:[{timestamp,actor,from_state,to_state,event_type,comment,validation_run_id?}]}`. `origin_run` y `validation_run` son resúmenes navegables; el run de origen permanece inmutable.

- `GET /findings` → lista Finding.
- `POST /findings/{id}/exceptions` `{}` → Exception 201 (idempotente por finding).
- `GET /exceptions?state=&module=&severity=&priority=&assigned_user_id=&overdue=&q=` → lista Exception filtrada dentro de la organización.
- `GET /exceptions/{id}` → Exception más finding.
- `POST /exceptions/{id}/validate` `{version:number,validation_run_id?:string}` → Exception. Evalúa una ejecución posterior `SUCCESS` del mismo `configuration_id`; si se omite `validation_run_id`, usa la candidata elegible más reciente. Data Intake exige que desaparezca el fallo de la misma regla/columna, ReconOps que el control quede conforme o desaparezca la clasificación asociada y Sentinel que el monitor termine `HEALTHY`. La validación registra ejecución, fecha y evidencia sin modificar el run original. Sin run posterior devuelve 409 `VALIDATION_RUN_REQUIRED`; un run indicado que ya no es el más reciente devuelve 409 `VALIDATION_RUN_OUTDATED`. Si el problema persiste, conserva evidencia `FAILED`, incrementa la versión y mantiene `PENDING_VALIDATION`.
- Al completarse una ejecución, el worker reevalúa las excepciones `PENDING_VALIDATION` de esa misma configuración y actualiza su evidencia. Una validación positiva habilita la acción humana `RESOLVED`. Si el caso tiene `auto_resolve_enabled=true`, la política lo resuelve con actor SYSTEM y run confirmatorio; por defecto está deshabilitada.
- `PATCH /exceptions/{id}` `{version:number,state?:'OPEN'|'ASSIGNED'|'INVESTIGATING'|'PENDING_VALIDATION'|'RESOLVED'|'DISCARDED'|'ACCEPTED'|'NOT_APPLICABLE'|'REOPENED',assigned_user_id?:string|null,priority?:string,sla_hours?:number|null,due_at?:datetime|null,auto_resolve_enabled?:boolean,root_cause?:string,resolution?:string,administrative_reason?:string,comment?:string}` → Exception. Versión obsoleta 409. `RESOLVED` solo se acepta desde `PENDING_VALIDATION`, reevalúa el último run posterior y exige causa raíz y resolución; sin evidencia positiva devuelve 422 `TECHNICAL_VALIDATION_REQUIRED`. `DISCARDED`, `ACCEPTED` y `NOT_APPLICABLE` exigen un `administrative_reason` explícito nuevo (422 `ADMINISTRATIVE_REASON_REQUIRED` si falta), no equivalen a una resolución técnica y no establecen evidencia de validación. Los estados históricos `WAITING_EXTERNAL` y `FALSE_POSITIVE` siguen siendo legibles. `owner` permanece como campo de compatibilidad; las nuevas asignaciones utilizan un usuario estable.
- `GET /audit-events` → lista Audit `{id,event_type,actor,subject_type,subject_id,message,created_at,metadata}`.
- `GET /rules` → lista `{id,code,name,type,module,description,severity}` (catálogo real reglas soportadas).
- `GET /users` → lista de usuarios locales, permisos efectivos, estado y versión; nunca hashes ni sesiones. Administración descrita abajo.

Errores: `{error:{code,message,details,request_id}}`. Validación 422, sesión 401, CSRF/permisos 403, inexistente 404, conflicto 409.

## Ejecución

`uv sync --directory backend` crea `backend/.venv`. Desde backend:

```
uv run python -m trackvance.seed
uv run uvicorn trackvance.api:app --host 127.0.0.1 --port 8000
# Lane general (default)
uv run python -m trackvance.worker
# Lane exclusiva de escritura remota
TRACKVANCE_WORKER_LANE=DELIVERY uv run python -m trackvance.worker
```

Variables: `DATABASE_URL` (SQLite por defecto o PostgreSQL psycopg),
`TRACKVANCE_STORAGE_DIR` (alias `TRACKVANCE_STORAGE_ROOT` admitido),
`TRACKVANCE_WORKER_LANE=DEFAULT|DELIVERY|ACQUISITION`,
`TRACKVANCE_DESTINATION_SECRETS_DIR`,
`TRACKVANCE_DESTINATION_SECRET_KEY_FILE`, `DEMO_ACCESS_ENABLED=true`,
`DEMO_SEED_ENABLED=true`, `TRACKVANCE_WEB_ORIGIN=http://localhost:3000`,
`MAX_UPLOAD_BYTES=10485760`, `TRACKVANCE_MAX_ROWS=100000`. Las variables
`TRACKVANCE_SECRETS_DIR` y `TRACKVANCE_SECRET_KEY_FILE`
continúan aislando credenciales de entrada. API startup aplica Alembic y backfill
de artifacts. `DEMO_ACCESS_ENABLED` controla exclusivamente la sesión demo;
`DEMO_SEED_ENABLED`, el seed idempotente. Deshabilitar el seed no elimina datos
persistidos. Los workers esperan el schema inicializado.

Compose ejecuta `worker` DEFAULT, `delivery-worker` DELIVERY y
`acquisition-worker` ACQUISITION, además de dispatcher y dos consumidores. El primero no monta secretos; el segundo
monta artifacts y secretos de destino, pero no secretos de conexiones de origen.
La API monta ambos tipos para administrar y explorar conexiones/destinos. Los
heartbeats se guardan por lane; Sentinel/Delivery se despachan en `scheduler`,
independiente de `DEFAULT`. Adquisición sólo monta secretos fuente; los consumidores
y scheduler no montan secretos de negocio.

## Adiciones y semántica 0.5.0

La migración `0008_data_delivery` agrega las tablas de destinos, revisiones e
intentos, más `jobs.lane`. Las configuraciones históricas no se modifican. Data
Delivery fija una DatasetVersion y DestinationVersion, ejecuta preflight dos veces,
usa una transacción remota por intento y publica receipt/manifest/linaje sólo
después de confirmación. `UNKNOWN` conserva la ambigüedad de commit y bloquea el
reintento automático. Ver [ADR 0015](../docs/adr/0015-data-delivery.md).

Backup/restore y verificación incorporan `delivery_credentials` y `delivery_keys`.
Los almacenes cifrados y sus claves son material sensible aunque las respuestas y
manifests no lo muestren. Desde 0.6.0 el control de acceso usa permisos específicos de Delivery y destinos; no se autoriza con las etiquetas históricas de User.

## Adiciones y semántica 0.3.0

`DatasetVersion` añade `original_artifact_id`, `canonical_artifact_id`,
`source_run_id`, `parent_version_id`, `is_derived`, `has_original_upload`,
`artifacts` y `lineage`. `source_type=INTAKE_OUTPUT` tiene Parquet aceptado,
sin upload original. Profiling incluye method/version, semantic_tag,
distinct_rate y uniqueness_ratio. Multipart upload admite `column_overrides`
JSON string. El sample se lee después de verificar integridad del canonical.

Las configuraciones admiten `schema_version:2`, reglas declarativas Intake y
Sentinel y transforms ordenados. Recon admite `key_normalization`,
`comparison_rules` múltiples y `aggregation`. Ver parámetros y ejemplos en
[catálogo](../docs/rules-catalog.md). Las respuestas añaden metadata derivada
de semántica; al republicar se elimina esta metadata antes de validar inputs.

- `POST /intake/contracts/{id}/versions`, `/recon/controls/{id}/versions`,
  `/monitors/{id}/versions` con `{config,description?}` → 201 snapshot nuevo,
  ID nuevo, version+1 y previous_version_id. Una publicación repetida sobre
  un padre con sucesor produce 409. No modifica el padre ni runs previos.
- POST de runs acepta header `Idempotency-Key` hasta 128 caracteres. Mismo
  endpoint/org/key/payload devuelve mismo run; payload distinto produce 409
  `IDEMPOTENCY_CONFLICT`.
- `POST /execution-plans/preview` con
  `{configuration_id,dataset_version_id,target_version_id?}` → plan local,
  estimaciones, presupuesto, engine/reason/rejection y normalización.
- `GET /runs/{id}/execution-plan` → snapshot del plan.
- `GET /runs/{id}/diagnostics` → estado, plan, normalización, schema de
  configuración, fechas y error técnico.
- `GET /artifacts/{id}/download` → archivo verificado de la organización;
  permiso artifacts:download, auditoría ARTIFACT_DOWNLOADED.
- `GET /runs/{id}/export.xlsx` → SUCCESS requerido; permiso exports:download.
  MIME `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
  Content-Disposition `trackvance_<module>_<run>.xlsx`, header X-Artifact-ID.
  Audita EXPORT_DOWNLOADED y registra EXPORT_XLSX/EXPORT_OF. 409 si resultados
  pendientes/corruptos; 422 EXPORT_LIMIT_EXCEEDED si excede límites de Excel.

Run initiated_by es `{type,id,display_name}`; metrics añade
source_row_numbering/target_row_numbering para distinguir PHYSICAL_LINE de
RECORD_NUMBER. Errores conservan original_row_number por compatibilidad,
con el significado declarado por la política. Recon añade comparisons y
listas de líneas en agregaciones. Las reglas Sentinel preservan método y
versión de métrica; history sólo incluye ejecuciones SUCCESS compatibles.

Audit añade actor_type/actor_id/actor_legacy/request_id/run_id; `actor` sigue
siendo el nombre visible. Los eventos de excepciones también tienen identidad
estable. Las colecciones se limitan a la organización y las acciones se
validan por permiso del rol. Más detalles en
[ADR de evidencia](../docs/adr/0002-evidence-and-artifacts.md).


## Endurecimiento Data Delivery 0.5.1

Se mantienen los endpoints existentes y se añaden dos paths (tres operaciones):

- `POST /delivery/runs/{run_id}/repair-evidence`, sesión + CSRF + `runs:execute`.
  Sin body obligatorio. Devuelve 200 `{run_id,status:'REPAIRED'|'ALREADY_VALID',
  receipt_artifact_id,manifest_artifact_id}`. Sólo Run DELIVERY SUCCESS/COMMITTED
  con un único intento COMMITTED verificable. Valida scope, configuración y hashes,
  DatasetVersion/canónico, revisión destino y métricas; nunca accede a secretos,
  DataSink o conexión/transacción remota. Reutiliza IDs/bytes/enlaces válidos.
  409 `DELIVERY_REPAIR_NOT_ALLOWED`, `DELIVERY_EVIDENCE_UNVERIFIABLE` o
  `DELIVERY_EVIDENCE_REPAIR_FAILED` ante estado o evidencia no reparables; 404 para
  recurso ajeno/inexistente. No transforma UNKNOWN/FAILED en éxito.
- `GET /delivery/runs/{run_id}/reviews`, `runs:read`: `{items:[DeliveryReview],total}`.
- `POST /delivery/runs/{run_id}/reviews`, sesión + CSRF + `runs:execute`:
  `{delivery_attempt_id,outcome,note,verified_at?}` → `DeliveryReview`, 201.
  outcome es `REMOTE_COMMIT_OBSERVED`, `REMOTE_NOT_COMMITTED_OBSERVED` o
  `INCONCLUSIVE`; nota no vacía, máximo 4000 caracteres. verified_at acepta ISO
  con zona, no epoch numérico, no futuro ni anterior al inicio del intento.
  Omitirlo usa el instante del servidor. 409 `DELIVERY_REVIEW_NOT_ALLOWED` si
  Run/attempt no conservan UNKNOWN; 422 fecha/cuerpo inválidos y 404 scope ajeno.

`DeliveryReview = {id,run_id,delivery_attempt_id,reviewer_id,reviewer_name,outcome,
note,verified_at,created_at}`. Organization es un scope interno, no un campo
seleccionable por el cliente. El revisor procede de la sesión y se conserva su
nombre al crear la observación. Historial append-only, sin PATCH/DELETE. Registrar
una revisión nunca cambia el intento, no crea Jobs ni Runs ni invoca DataSink.

La tabla `delivery_reviews` se incorpora sólo mediante `0009_delivery_reviews`:
FKs a Run/Attempt/User, índices y CHECK de los tres outcomes. No se modifica
ninguna migración anterior ni hay nuevos permisos granulares.

### Métricas y compatibilidad de evidencia

`rows_attempted` cuenta filas fuente preparadas. `rows_written` cuenta filas
fuente enviadas en una operación confirmada, **no** población física remota ni
efectos de triggers. `rows_inserted`/`rows_updated` son acciones reportadas con
certeza por el adaptador o `null`; UI muestra N/D. Cero siempre significa cero
conocido. `bytes_sent` mide representación UTF-8 preparada, no tráfico de red.

UPSERT PostgreSQL 18 usa RETURNING OLD/NEW documentado dentro de la misma
transacción, sin pre-SELECT ni xmax. PostgreSQL 16/17 conserva null para desglose
no fiable; una entrada vacía permite cero y un mapping sólo-claves usa DO NOTHING
con inserciones conocidas y cero actualizaciones. Los guards
ON CONFLICT/constraints/locks siguen vigentes. No se requiere actualizar el
servidor metadata PostgreSQL 16.

Runs nuevos persistirán `preflight_seconds` y `write_seconds`, medidos con reloj
monotónico en el worker. El segundo incluye deliver_prepared/commit remoto,
excluye persistencia de resultado y publicación de evidencia. Receipt/manifest
reutilizan esos valores; los históricos ausentes no reciben ceros inventados.
`metric_semantics` (version1) es metadata aditiva en nueva evidencia. Receipt
sigue schema1 y manifest schema2; lectores aceptan 0.5.0 sin el bloque. Reparar
no reescribe artifacts históricos válidos, incluso si carecen de estos campos.

### Linaje y auditoría canónicos

| source_type | relation | target_type |
| --- | --- | --- |
| DATASET_VERSION | DELIVERY_INPUT | RUN |
| RUN | DELIVERED_TO | DELIVERY_DESTINATION_VERSION |
| RUN | DELIVERY_RECEIPT | ARTIFACT |
| ARTIFACT | EVIDENCE_OF | DELIVERY_ATTEMPT |
| RUN | RUN_OUTPUT | ARTIFACT |

`DELIVERY_DESTINATION_VERSION` no es una relation. No se renombra ningún enlace
histórico. Reparación emite `DELIVERY_EVIDENCE_REPAIR_STARTED`,
`DELIVERY_EVIDENCE_REPAIRED` o `DELIVERY_EVIDENCE_REPAIR_FAILED`; revisión emite
`DELIVERY_UNKNOWN_REVIEWED`. Audit incluye referencias/resultado/fecha sanitizados,
nunca nota libre, credenciales ni filas de negocio. La nota sí vive en el recurso
de revisión, con su autorización normal de lectura de Run.

## Ajustes de interfaz 0.4.1

Esta revisión no añade endpoints ni modifica DTOs persistidos. La interfaz conserva los valores internos de transformaciones y normalización de claves, pero presenta etiquetas funcionales, ejemplos y vistas previas construidas con la muestra acotada de `GET /dataset-versions/{version_id}/profile`. El catálogo de responsables continúa enviando `owner: string`; Sentinel continúa publicando listas de columnas en su configuración declarativa. El cierre de sesión mantiene `POST /auth/logout` y, al completarse, el cliente vuelve a la pantalla de acceso.

## Evolución funcional 0.4.0

### Reglas avanzadas

Intake añade `compound_unique`, `length`, `column_compare` y `reference`; las reglas por registro admiten `when` como condición declarativa acotada. Nuevas publicaciones asignan `rule_id` estable; los lectores históricos no inventan IDs ni alteran fingerprints anteriores. `rule_id`, `evaluated_count`, `failed_count`, `skipped_count` y severidad acompañan el resultado. Una regla que no evaluó filas no demuestra una corrección técnica. El catálogo completo y los límites de la DSL están en `docs/rules-catalog.md` y ADR 0008.

Las referencias fijan una DatasetVersion accesible por organización, columnas y políticas. Se validan al publicar y ejecutar; su tamaño participa en preflight, el manifiesto conserva hashes y `RUN_REFERENCE` incorpora linaje. El motor recibe snapshots normalizados y no consulta fuentes externas.

Recon añade `source_transforms`, `target_transforms` y `aggregations` SUM/COUNT por columna, con configuración explícita del lado agrupado. Una comparación sobre una columna no agregada del lado agrupado se rechaza: no se toma silenciosamente la primera fila. Se conserva el alias histórico `aggregation`. `metrics.comparisons` y el detalle de cada resultado identifican todas las comparaciones, tolerancias y fallos. Ver ADR 0009.

### Sentinel programado

- `GET /monitors/{id}/schedule`: programación actual o `null`; monitor inexistente o de otra organización devuelve 404.
- `POST /monitors/{id}/schedule`: `{interval_seconds:60..2678400,enabled:true,starts_at?:ISO-con-zona,expected_version?:number}`. Crear omite expected_version; editar requiere la revisión vigente. Devuelve programación con ID, revisión, fecha próxima y políticas. Conflicto 409, fecha inválida 422. Requiere `sentinel:schedule`; al habilitar comprueba también `sentinel:execute`, además de CSRF.
- `GET /monitors/{id}/occurrences?limit=50`: hasta 200 intervalos recientes, con revisión, Configuration, DatasetVersion, Run, fecha prevista/despacho/inicio/finalización, estado técnico, decisión de negocio, métricas y reason code.
- `GET /monitors/{id}/series?include_versions=true&limit=500`: hasta 2000 muestras del linaje del monitor. Agrupa por clave, dimensiones, método y versión de definición. Cada punto incluye ejecución, configuración, DatasetVersion, fecha, `value` numérico para visualización y `exact_value` textual para conservar precisión. No mezcla series incompatibles.
- `GET /monitors/{id}/alerts?limit=50`: hasta 200 Findings históricos del monitor, con enlaces al run y excepción si existe.
- `/metrics` conserva la respuesta anterior para lectores existentes.

El scheduler usa `LATEST_REGISTERED_SNAPSHOT`, `COALESCE_LATEST` y `SKIP_WHILE_ACTIVE`; guarda revisiones inmutables y ocurrencias transaccionales. Un slot ya registrado no se repite tras editar una programación. Un monitor inválido genera precondición fallida y no bloquea trabajos independientes. El worker no recibe credenciales ni refresca fuentes externas. El manifiesto incluye `processing.schedule`. Ver ADR 0010.

### Excepciones operativas

`Exception` amplía su DTO con `assigned_user_id`, `priority`, `sla_hours`, `due_at`, `overdue`, `reopened_at`, `auto_resolve_enabled` y `attachments`. `owner` se mantiene para lectura histórica; cuando hay asignación estable, el backend deriva la etiqueta del usuario.

- `GET /exceptions`: añade filtros `priority`, `assigned_user_id`, `overdue` y `search` a los existentes.
- `GET /exceptions/assignees`: usuarios activos de la organización con permiso para gestionar excepciones; no exige administración de usuarios.
- `PATCH /exceptions/{id}`: añade los campos operativos anteriores. Estados nuevos `ASSIGNED` y `REOPENED`. Cada escritura usa CAS por `version`. Asignación exige usuario activo válido; SLA de 1 a 8760 horas se cuenta desde creación/reapertura, salvo fecha objetivo explícita.
- `POST /exceptions/{id}/comments`: `{version,comment}` no vacío; añade evento sin alterar resultados históricos.
- `POST /exceptions/{id}/attachments`: multipart `version`, `file`, `description`; máximo 10 MiB y formatos permitidos. StorageProvider conserva identidad SHA-256, actor y vínculo al caso. No admite rutas del cliente.
- `GET /exceptions/{id}/attachments/{attachment_id}/download`: exige lectura del caso, organización y permiso de descarga; verifica hash y registra auditoría.

Flujo `OPEN → ASSIGNED → INVESTIGATING → PENDING_VALIDATION → RESOLVED`; también se permite pasar de OPEN a INVESTIGATING. Resolver exige causa/corrección y validación posterior vigente del mismo control. Reabrir exige comentario y una nueva ejecución creada después de la reapertura. Cierres DISCARDED/ACCEPTED/NOT_APPLICABLE exigen un motivo explícito nuevo en cada petición y nunca equivalen a resolución técnica. Cambiar la política automática o cerrar requiere `exceptions:close`. La política automática está deshabilitada por defecto y conserva SYSTEM, evidencia, run confirmatorio y timeline. Ver ADR 0011.

### Usuarios, roles y autenticación 0.6.1

Los DTO exactos `UserResponse`, `RoleResponse`, `ExternalIdentityResponse` y
`NotificationResponse` están en OpenAPI. Nunca contienen hash, contraseña,
token OAuth ni secreto SMTP/client. `UserResponse` añade first_name/last_name,
username, role_id/role_version, active/deleted, must_change_password,
temporary_password_expires_at, password_changed_at, last_login_at y external_identities.
No incluye credential_delivery ni permite recuperar una contraseña temporal.
Los nombres personales legacy pueden ser null; name histórico se conserva.

| Método/ruta | Entrada y resultado |
| --- | --- |
| GET /roles/permissions | Catálogo controlado con code, group, label, dependencies, delegable. |
| GET /roles y /users/roles | Colección Role; search opcional filtra por nombre; alias histórico devuelve roles persistidos. |
| GET /roles/{id} | Role con user_count de cuentas no eliminadas y protected. |
| POST /roles | name, description?, permissions[]?, active?; permissions omitido equivale a []; 201 Role. |
| PATCH /roles/{id} | version, name?, description?, permissions[]?, active?; Role. |
| DELETE /roles/{id} | Cuerpo {version}; baja lógica; rechaza asociados no eliminados y Administrator. |
| GET /users?active=&include_deleted=&search= | Colección User; filtros opcionales, include_deleted=false predeterminado. |
| GET /users/{id} | User del scope con estados de credenciales/identidades. |
| POST /users | first_name,last_name,username,email,role_id,active? (true por defecto); 201 UserCredentialIssueResponse. Sin password de entrada. |
| PATCH /users/{id} | version, first_name?,last_name?,username?,email?,role_id?,active?. |
| DELETE /users/{id} | Cuerpo {version}; baja lógica; reserva identificadores y revoca sesiones. |
| POST /users/{id}/regenerate-credentials | {version}; 200 UserCredentialIssueResponse; nueva temporal de 24 horas y revocación de todas las sesiones. |
| POST /users/{id}/resend-credentials y /reset-password | Aliases deprecated de regenerate-credentials, mismo {version} y envelope; no envían correo ni aceptan password elegida por admin. |
| DELETE /users/{id}/external-identities/{identity_id} | Sin cuerpo; desvincula identidad del usuario del mismo scope, audita y revoca sesiones. |
| POST /auth/login | {username,password} o {email,password}; exactamente un identificador, username acepta usuario o correo normalizado. |
| POST /auth/first-login/change-password | {new_password}; confirmación se valida en UI; devuelve envelope /me con cookie/CSRF nuevos. |
| GET /auth/providers | {items:[{id,name,start_url}],total,statuses:{microsoft,google},local_enabled:true}; items contiene sólo habilitados/configurados; estados ENABLED/NOT_CONFIGURED/DISABLED, sin client IDs/secrets. |
| GET /auth/sso/{provider}/start | Redirect a autorización, state/nonce/PKCE y cookie binder. Sólo microsoft/google. |
| GET /auth/sso/{provider}/callback | code/state o error; consume intento único, valida token y redirige a UI o /login?sso_error. |
| GET /notifications/status | Deprecated, histórico: enabled=false, configured=false, provider=NONE, security=NONE, from_address y from_name vacíos, availability=HISTORICAL_ONLY. Ninguna variable activa SMTP. |
| GET /notifications/deliveries | Deprecated: hasta 200 metadatos históricos de la organización, sin cuerpos. Alta/regeneración no añaden intentos. |

Las modificaciones de User/Role administran version/CAS; desvincular identidad externa es una eliminación idempotente por identidad bajo lock, y un vínculo inexistente devuelve404. Dependencias incompletas, códigos
desconocidos o permisos no delegables se rechazan. Administrator protegido
resuelve todo el catálogo y no puede perder identidad/actividad/permisos.
Un rol con usuarios no eliminados no puede desactivarse ni borrarse; se preserva
el último administrador activo y se protege la eliminación propia.

`UserCredentialIssueResponse` es exclusivo de las respuestas de alta y
regeneración (incluidos los aliases deprecated):

```json
{
  "user": "UserResponse sin secretos",
  "temporary_credentials": {
    "username": "username del usuario",
    "temporary_password": "valor efímero generado, nunca persistido",
    "expires_at": "fecha ISO UTC de expiración",
    "must_change_password": true
  }
}
```

Los valores anteriores describen el contrato y no son credenciales válidas.
El alta genera 32 caracteres URL-safe con CSPRNG y sólo persiste Argon2.
La temporal vence a las 24 horas; la respuesta lleva `Cache-Control: no-store`
y `Pragma: no-cache`. No hay recuperación por GET, reconsulta, auditoría o historial.
Los errores inesperados durante la emisión devuelven `CREDENTIAL_ISSUE_FAILED`
con mensaje fijo, sin registrar el texto de excepciones de hashing o persistencia.
El cambio de primer acceso aplica la misma protección y devuelve
`PASSWORD_CHANGE_FAILED` ante fallos inesperados al escribir la nueva credencial.
El frontend la conserva únicamente mientras muestra el modal, ofrece copia
explícita y descarta el secreto al cerrar o desmontar. No usa caché de consultas,
Storage del navegador, cookies, URL ni notificaciones para conservarlo.

Regenerar invalida la contraseña anterior, obliga al cambio y revoca todas las
sesiones. Exige users:manage, CSRF, organización y versión vigente; un CAS obsoleto
responde 409 sin emitir una credencial nueva. No envía email ni crea registros de
notificación, aunque queden variables SMTP de una instalación antigua. Primer
login local y SSO con must_change_password restringe acceso hasta definir una
contraseña de 12–1024 caracteres diferente a la temporal. Role/email/username/
actividad y desvinculación también revocan sesiones. Editar grants no requiere
logout: el backend relee Role y la UI refresca /me.

SSO no auto-provisiona ni confía en roles externos. El primer vínculo exige email
con autoridad verificada y usuario activo preprovisionado; accesos posteriores
usan provider/issuer/subject. No se guardan access/id/refresh tokens. Ver
[identidad](../docs/development/identity-060.md), [SSO](../docs/development/sso-setup.md)
y el [antecedente SMTP deshabilitado](../docs/development/smtp-setup.md).

### Columnas de auditoría y target policy 0.6.0

Delivery config añade `audit_columns_enabled?:boolean=false`. Sin policy, ambos
valores mantienen el comportamiento sin auditoría. Omitir el campo conserva el
hash histórico; enviarlo explícitamente incluye el booleano en el nuevo snapshot.
true activa conjuntamente
fechaIngesta/usuario; mapping no puede usar nombres reservados. El preflight
verifica tipos, columnas existentes, permisos DDL y policy del target físico.

`GET /delivery/destinations/{id}/target-policy?schema_name=&table_name=&destination_version_id=`
requiere schema_name/table_name (1..128 caracteres); destination_version_id es
opcional y, ausente, usa la versión vigente del destino. Devuelve
`{audit_columns_required:boolean,policy_id:string|null,target_fingerprint:string,
materialized_at:string|null}`. Consulta sólo metadata local y exige delivery:read.
No puede desactivarse mediante otra configuración o versión de credenciales.

DeliveryAttempt persiste `system_audit` (objeto vacío histórico); DTO, receipt y
manifest omiten el campo cuando está vacío para conservar el contrato histórico.
Cuando hay auditoría incluye metadata de policy, columnas, timestamp UTC del
intento y username interno snapshot. Receipt/manifest
reutilizan esa metadata sin modificar DatasetVersion ni filas origen. CREATE
crea columnas NOT NULL; ALTER sobre tabla existente conserva filas anteriores
NULL y DML nuevo recibe ambos valores. APPEND/OVERWRITE/UPSERT INSERT/UPDATE
comparten timestamp por intento; ALTER+DML se confirman en una transacción remota.

Preflight devuelve `system_audit={enabled:false}` sin auditoría; cuando está activa
agrega columns, missing_columns, policy_id y materialized_at. Los tipos creados son
TIMESTAMPTZ(6)/VARCHAR(128) en PostgreSQL y DATETIMEOFFSET(6)/NVARCHAR(128) en SQL Server.
Faltantes tras materialización provocan AUDIT_COLUMNS_DRIFT; tipos/nombres no
adoptables, AUDIT_COLUMNS_INCOMPATIBLE; desactivación exigida por policy,
AUDIT_COLUMNS_REQUIRED. UNKNOWN no se reintenta automáticamente; la policy sigue
requerida aunque no haya confirmación local. Ver [Delivery audit](../docs/development/delivery-audit.md)
para fingerprint, permisos, evidencia, recuperación y límites.
