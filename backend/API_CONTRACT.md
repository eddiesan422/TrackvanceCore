# Contrato prototipo local 0.3.0

El esquema ejecutable versionado está en [openapi.json](openapi.json). Se
genera desde la aplicación y documenta sesión cookie, CSRF, MIME XLSX,
declaraciones de reglas y errores. Mantiene base `/api/v1` para compatibilidad.

Base `/api/v1`. Todas las listas son `{items: [...], total: number}`. IDs string opacos. Fechas ISO UTC. Todos los endpoints salvo `/health` y `/auth/demo|login` requieren cookie sesión. Todas las mutaciones autenticadas requieren `X-CSRF-Token` devuelto al iniciar sesión. El proxy Vite preserva cookie; usar `credentials: 'include'`.

## Identidad y estado

- `GET /health` → `{status:'ok',version:'0.3.0',mode:'local-prototype',demo_enabled:true}`.
- `GET /health/ready` → 200 con DB/storage/migrations listos o 503; también `/health/ready` para Compose.
- `POST /auth/demo` cuerpo `{}` → sesión demo explícita (no password): `{user:{id,name,email,role,permissions:[]},organization:{id,name},csrf_token,demo_mode:true}`; cookie HttpOnly `trackvance_session`.
- `POST /auth/login` `{email,password}` → mismo.
- `GET /me` → mismo.
- `POST /auth/logout` → `{ok:true}`.
- `GET /dashboard?period=7d|30d|90d|all&dataset_id=&module=intake|recon|sentinel&status=ATTENTION|HEALTHY|IN_PROGRESS|TECHNICAL_FAILURE&criticality=CRITICAL|HIGH|MEDIUM|LOW` → cockpit operativo limitado a la organización autenticada. `period` vale `30d` por defecto; todos los demás filtros son opcionales. Devuelve `{applied_filters,filter_options,period,stats:{datasets,total_rows,runs,open_exceptions,health_score,controls_failed,affected_datasets},variations,attention,attention_total,health_history,datasets_attention,recent_runs,module_status,activity,volume_history,organization_name,prototype:true}`. `attention` prioriza excepciones, hallazgos y ejecuciones por severidad/criticidad e incluye la ruta de acción. `health_history` desglosa salud general, Intake, ReconOps y Sentinel; `recent_runs` añade dataset, registros procesados, hallazgos, duración, estado operativo y salud cuando existen. `health_score` pondera por unidades evaluadas. `SUCCESS` conserva su significado técnico y `operational_status` expresa por separado si el resultado de negocio está sano o requiere atención. Las variaciones son `{previous,delta}` frente al período anterior o `null` cuando no existe una comparación válida.
- `GET /system/engines` → `{items:[{id,name,version,available,status,description}],worker:{status,last_seen},limits:{max_upload_mb,max_rows},mode:'local-prototype'}`.

## Datasets

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
- `GET /dataset-versions/{id}/profile` → Version más `{sample:row[]}`.

## Configuraciones y ejecuciones

`Configuration = {id,name,module,version,dataset_id,dataset_name,target_dataset_id,target_dataset_name,owner,description,status,config,created_at,latest_run:Run|null}`.

`Run = {id,run_id,job_id,module,name,status,decision,config_id,dataset_version_id,target_version_id,dataset_name,created_at,started_at,finished_at,progress_percent,progress_stage,metrics,execution_plan,error,output_version_id}`.

- `GET /intake/contracts`, `GET /recon/controls`, `GET /monitors` → lista Configuration.
- `POST /intake/contracts` `{name,dataset_id,owner?,description?,config:{required_columns:['pedido_id'],unique_columns:['pedido_id'],numeric_columns:['valor'],positive_columns:['valor'],max_error_rate:0.05}}` → Configuration 201. Las columnas opcionales de config default a [] y tasa 0.
- `POST /recon/controls` `{name,dataset_id,target_dataset_id,owner?,description?,config:{key_columns:['pedido_id'],amount_column:'valor',tolerance:'0.01'}}` → Configuration 201. El shorthand conserva tolerancia Decimal; los nuevos snapshots tienen normalización explícita default NONE. Preferir comparison_rules para las capacidades nuevas.
- `POST /monitors` `{name,dataset_id,owner?,description?,config:{required_columns:['pedido_id'],null_columns:['pedido_id'],max_null_rate:0.05,max_volume_change_pct:15,max_age_hours:48}}` → Configuration 201.
- `GET /runs?module=intake|recon|sentinel` → lista Run.
- `POST /intake/runs` `{contract_id,dataset_version_id}` → Run 202.
- `POST /recon/runs` `{control_id,source_version_id,target_version_id}` → Run 202.
- `POST /monitors/{id}/runs` `{dataset_version_id?:string}` → Run 202 (última versión si omitida).
- `GET /runs/{id}` → Run más `{findings:Finding[]}`. Poll hasta SUCCESS/FAILED/CANCELLED. SUCCESS significa procesamiento terminado; calidad es decision.
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
- `GET /exceptions?state=&module=&severity=` → lista Exception.
- `GET /exceptions/{id}` → Exception más finding.
- `POST /exceptions/{id}/validate` `{version:number,validation_run_id?:string}` → Exception. Evalúa una ejecución posterior `SUCCESS` del mismo `configuration_id`; si se omite `validation_run_id`, usa la candidata elegible más reciente. Data Intake exige que desaparezca el fallo de la misma regla/columna, ReconOps que el control quede conforme o desaparezca la clasificación asociada y Sentinel que el monitor termine `HEALTHY`. La validación registra ejecución, fecha y evidencia sin modificar el run original. Sin run posterior devuelve 409 `VALIDATION_RUN_REQUIRED`; un run indicado que ya no es el más reciente devuelve 409 `VALIDATION_RUN_OUTDATED`. Si el problema persiste, conserva evidencia `FAILED`, incrementa la versión y mantiene `PENDING_VALIDATION`.
- Al completarse una ejecución, el worker reevalúa las excepciones `PENDING_VALIDATION` de esa misma configuración y actualiza su evidencia. Una validación positiva habilita la acción humana `RESOLVED`; no cambia por sí sola el estado final del caso.
- `PATCH /exceptions/{id}` `{version:number,state?:'OPEN'|'INVESTIGATING'|'PENDING_VALIDATION'|'RESOLVED'|'DISCARDED'|'ACCEPTED'|'NOT_APPLICABLE',owner?:string,root_cause?:string,resolution?:string,administrative_reason?:string,comment?:string}` → Exception. Version obsoleta 409. `RESOLVED` solo se acepta desde `PENDING_VALIDATION`, reevalúa el último run posterior y exige causa raíz y resolución; sin evidencia positiva devuelve 422 `TECHNICAL_VALIDATION_REQUIRED`. `DISCARDED`, `ACCEPTED` y `NOT_APPLICABLE` exigen `administrative_reason` (422 `ADMINISTRATIVE_REASON_REQUIRED` si falta), no equivalen a una resolución técnica y no establecen evidencia de validación. Los estados históricos `WAITING_EXTERNAL` y `FALSE_POSITIVE` siguen siendo legibles.
- `GET /audit-events` → lista Audit `{id,event_type,actor,subject_type,subject_id,message,created_at,metadata}`.
- `GET /rules` → lista `{id,code,name,type,module,description,severity}` (catálogo real reglas soportadas).
- `GET /users` → lista User (solo lectura en prototipo).

Errores: `{error:{code,message,details,request_id}}`. Validación 422, sesión 401, CSRF/permisos 403, inexistente 404, conflicto 409.

## Ejecución

`uv sync --directory backend` crea `backend/.venv`. Desde backend:

```
uv run python -m trackvance.seed
uv run uvicorn trackvance.api:app --host 127.0.0.1 --port 8000
uv run python -m trackvance.worker
```

Variables: `DATABASE_URL` (SQLite por defecto o PostgreSQL psycopg), `TRACKVANCE_STORAGE_DIR` (alias `TRACKVANCE_STORAGE_ROOT` admitido), `DEMO_SEED_ENABLED=true`, `TRACKVANCE_WEB_ORIGIN=http://localhost:3000`, `MAX_UPLOAD_BYTES=10485760`, `TRACKVANCE_MAX_ROWS=100000`. API startup aplica Alembic, backfill de artifacts y demo idempotente. Worker espera schema inicializado. Dockerfile en backend, context raíz, copia backend y demo. La sesión demo solo disponible si seed habilitado.

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
