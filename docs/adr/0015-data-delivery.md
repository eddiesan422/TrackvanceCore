# ADR 0015: Data Delivery controlado hacia bases SQL

- Estado: implementado y certificado localmente; CI del commit se registra por separado.
- Fecha: 2026-09-23.
- Sustituye únicamente la afirmación histórica de ADR 0007 que describía Data
  Delivery como futuro. No cambia la frontera de lectura de `DatasetSource`.

## Contexto

Trackvance ya conserva datasets como `DatasetVersion` inmutables y puede adquirir
snapshots desde archivos, PostgreSQL y SQL Server. Publicar esos datos hacia un
sistema externo tiene riesgos distintos de la lectura: permisos de escritura,
compatibilidad del target, transacciones remotas, confirmación ambigua, secretos
de destino y evidencia de qué revisión se entregó. Reutilizar `DatasetSource` o el
worker general mezclaría responsabilidades y expondría credenciales innecesarias.

## Decisión

Se incorpora `DataSink` como puerto de salida separado. Los adaptadores locales
`PostgreSQLDataSink` y `SQLServerDataSink` descubren metadata, validan permisos y
escriben una `DatasetVersion` canónica. Data Delivery no vuelve a adquirir la
fuente, no ejecuta SQL arbitrario y no aplica transforms de negocio: el mapping
declarado selecciona/ordena/renombra columnas y conserva el tipo lógico de la
DatasetVersion. La conversión de STRING a DECIMAL/DATE/TIMESTAMP/BOOLEAN, o entre
otras familias lógicas, se rechaza; el sink sólo materializa la representación
técnica del mismo tipo lógico.

Un destino tiene identidad estable y revisiones inmutables. La metadata contiene
host, puerto, base, usuario, opciones permitidas y una referencia opaca; nunca la
contraseña. Una configuración publicada fija `dataset_version_id`,
`destination_id`, `destination_version_id`, target, mapping, estrategia y claves.
Editar un destino no cambia configuraciones ni runs históricos.

El flujo exige preview y preflight antes de publicar. La ejecución repite el
preflight para detectar drift, permisos o configuración obsoleta inmediatamente
antes de abrir la transacción remota. Se soportan:

- `CREATE_AND_LOAD`, únicamente con `CREATE_TABLE`; puede crear el schema cuando
  se solicitó y falla si el schema o la tabla aparecen en una carrera.
- `APPEND`, que inserta en una tabla existente compatible.
- `OVERWRITE`, que ejecuta `DELETE` e inserción dentro de la misma transacción;
  no es un drop/recreate ni conserva filas no incluidas.
- `UPSERT`, que exige claves explícitas del mapping, sin nulls ni duplicados en
  la fuente y respaldadas por PK o restricción unique compatible en el destino.

Los tipos de mapping son `STRING`, `INT64`, `DECIMAL`, `DATE`, `TIMESTAMP` y
`BOOLEAN`; el tipo debe coincidir con la columna fuente, `DECIMAL` conserva
precisión/escala y `STRING` puede fijar longitud.
El preflight sólo admite `DECIMAL` contra tipos nativos exactos y comprueba tanto
escala como capacidad de dígitos enteros; `real`/`float` binarios se rechazan.
Los identificadores se validan y citan por dialecto. No se aceptan expresiones,
fragmentos SQL ni nombres fuera del contrato tipado.

`STRING` sólo admite almacenamiento Unicode variable que conserve el contrato:
`text`/`varchar`/`character varying` en PostgreSQL y `nvarchar` en SQL Server.
La longitud se mide en unidades UTF-16; NUL, surrogates no emparejados, familias
fixed/non-Unicode y tipos desconocidos se rechazan. Texto suplementario en SQL
Server exige collation `_SC` o `_UTF8`; las tablas creadas usan
`Latin1_General_100_CI_AS_SC`. `TIMESTAMP` exige offset ISO explícito, conserva
offset/instante y hasta seis microsegundos; los targets creados son
`TIMESTAMPTZ(6)` y `DATETIMEOFFSET(6)`. Un `datetimeoffset(7)` fuente debe mapearse
a `STRING` si se requiere conservar el séptimo dígito, nunca redondearse.

## Ejecución, idempotencia y estado UNKNOWN

`DatabaseJobQueue` persiste las lanes `DEFAULT` y `DELIVERY`. El mismo ejecutable
`python -m trackvance.worker` selecciona lane mediante
`TRACKVANCE_WORKER_LANE`. El worker `DEFAULT` procesa Intake, ReconOps y Sentinel;
además despacha el scheduler local. `delivery-worker` consume sólo Delivery y no
ejecuta el scheduler. Cada lane tiene heartbeat independiente:
`worker-heartbeat-default.json` y `worker-heartbeat-delivery.json`.

`POST /delivery/runs` usa `Idempotency-Key` para no crear dos runs por la misma
solicitud. Cada contacto remoto genera un `DeliveryAttempt` con clave estable y
estado `STARTED`, `COMMITTED`, `FAILED` o `UNKNOWN`:

- `COMMITTED` significa que el adaptador recibió confirmación del commit remoto.
- `FAILED` significa que se comprobó un fallo y se ejecutó rollback o no se llegó
  a un punto ambiguo.
- `UNKNOWN` significa que Trackvance perdió la confirmación después de intentar
  el commit, perdió el lease/worker con un intento iniciado, o recuperó un
  `STARTED` sin prueba concluyente. No equivale a `FAILED` ni a `COMMITTED`.

La preparación local —materialización, selección tipada, identificadores,
conversión de valores, tamaño serializado y recuperación de la credencial— se
congela como `PreparedDelivery` antes de persistir `STARTED`. Un fallo local queda
`FAILED_PRECONDITION` sin crear intento. Rechazos explícitos del motor, incluidos
constraints, fallo de serialización transaccional o deadlock con rollback demostrado, son
`FAILED`; sólo una confirmación realmente indeterminada produce `UNKNOWN`.

Un intento `UNKNOWN` no se reintenta automáticamente: repetir APPEND/OVERWRITE o
un UPSERT sin conocer el resultado podría duplicar o destruir datos. La revisión
operativa debe verificar el destino y decidir un nuevo run explícito. Un intento
ya `COMMITTED` no vuelve a ejecutar la transacción. El receipt se publica sólo
para commit confirmado; el estado remoto confirmado se persiste antes de crear
la evidencia local para impedir que un fallo del ArtifactStore repita la entrega.
En ese caso el Run conserva `COMMITTED` y marca evidencia `PENDING_REPAIR`; la
ausencia temporal de receipt no autoriza otra escritura.

El worker toma ownership mediante claim condicional antes de interpretar un
estado durable y respeta el orden global de locks Run→Job usado por cancelación.
Antes de `STARTED`, una cancelación impide abrir el intento; después de contacto
remoto, un resultado conocido `COMMITTED`/`FAILED` prevalece sobre una cancelación
tardía. Lease perdido o `STARTED` huérfano sin prueba queda `UNKNOWN`, sin replay.

La autoridad de ejecución no depende sólo del preflight. Para tablas existentes,
el adaptador adquiere un lock transaccional aunque la DatasetVersion tenga cero
filas. PostgreSQL vuelve a comprobar nombre, columnas y carácter no deferrable de
la constraint UPSERT después de tomar el lock; el staging temporal usa tipos y
collations nativos. `OVERWRITE` rechaza RLS activa. SQL Server bloquea el target,
revalida catálogos, clona tipos/collations de claves y rechaza todo índice unique
`IGNORE_DUP_KEY`. `OVERWRITE` requiere `SELECT` y `VIEW DEFINITION`, falla cerrado
si no puede ver policies y rechaza FILTER security policies activas. Estas
restricciones evitan commits exitosos con filas omitidas u ocultas.
PostgreSQL UPSERT exige `INSERT+UPDATE+SELECT` y `TEMPORARY`; SQL Server exige
`INSERT+UPDATE+SELECT`, evita `MERGE`, usa `SELECT @@ROWCOUNT` en el mismo batch y
aborta si una clave toca más de una fila. Su tabla `#temp` depende de la política
de `tempdb` del servidor.

## Evidencia y trazabilidad

Un commit confirmado produce un artifact `DELIVERY_RECEIPT` y amplía el manifest
schema 2 con la sección `delivery`. La evidencia registra IDs y hashes, revisión
de destino, target, estrategia, métricas y referencia remota, pero no secretos ni
filas completas. `ArtifactLink` y linaje conectan Run, DatasetVersion,
DestinationVersion, DeliveryAttempt, manifest y receipt. La auditoría conserva
publicación, encolado, inicio, commit, fallo o ambigüedad con actores estables.

## Secretos y topología

Los secretos de destino usan un `SecretStore` independiente:

- `TRACKVANCE_DESTINATION_SECRETS_DIR` / volumen `delivery_credentials` contiene
  payloads cifrados.
- `TRACKVANCE_DESTINATION_SECRET_KEY_FILE` / volumen `delivery_keys` contiene la
  clave maestra separada.

La API monta artifacts, secretos de conexiones de origen y secretos de destino
porque administra y descubre ambos. El worker `DEFAULT` monta sólo
`trackvance_data`. El `delivery-worker` monta `trackvance_data`,
`delivery_credentials` y `delivery_keys`, pero nunca `connection_credentials` ni
`connection_keys`. Esta separación reduce exposición; no convierte Docker
volumes en un gestor de secretos productivo ni aporta rotación automática.

Backup/restore trata PostgreSQL, artifacts y los cuatro volúmenes de secretos
como un conjunto coordinado. Falta una clave o un payload referenciado invalida
el backup. El manifest no imprime secretos, pero el archivo de backup contiene
material capaz de descifrarlos y debe mantenerse fuera de Git y de artifacts CI.
Reset, doctor y verificación de almacenamiento conocen ambas lanes, heartbeats,
tablas, referencias y almacenes.

Backup Docker usa staging privado, creación exclusiva, `fsync`, read-only y
verificación de tamaño/hash de cada copia antes de restaurar o inspeccionar. El
modo SQLite copia a destino nuevo y verifica antes de abrir/mutar la base. Ambos
cierran carreras TOCTOU.
Restore acepta el formato 0.4.1 manifest 1/state 2 en migración 0007: exige sus 21
tablas, migra a 0008, deja las tres tablas Delivery vacías y backfillea jobs
históricos a lane `DEFAULT`. State 2 no contenía hash estructural del catálogo;
esa propiedad histórica no se inventa y se compensa con contrato/proyección exactos.

## API, UI y autorización

La API vive bajo `/api/v1/delivery` y ofrece CRUD/test de destinos,
schemas/tablas/metadata, preview, preflight, configuraciones/versiones, runs,
intentos y receipt. La SPA `/delivery` expone administración de destinos, builder
guiado, ejecución y detalle con estado `UNKNOWN` explícito.

0.5.0 reutiliza provisionalmente permisos existentes:

- destinos: `connections:read`, `connections:manage` y `connections:use`;
- preview/preflight y publicación: `configurations:write`;
- ejecutar/consultar: `runs:execute` y `runs:read`;
- receipt: `artifacts:download`.

No se afirma un RBAC granular de Delivery. Permisos como administrar destinos de
salida, publicar mappings, ejecutar determinadas estrategias o targets y aprobar
reintentos tras `UNKNOWN` requieren una evolución compatible de política y UI.
Todos los recursos continúan acotados por organización.

## Límites y consecuencias

- Sólo PostgreSQL y SQL Server son sinks. S3, Azure Blob, REST, warehouses y
  mensajería no están implementados.
- No hay scheduler de Delivery, ejecución masiva, aprobación de cuatro ojos,
  transformación funcional, DDL arbitrario ni rollback entre Trackvance y el
  destino como una transacción distribuida.
- Preflight ofrece diagnóstico anticipado; la transacción vuelve a resolver el
  target y revalida bajo lock existencia, árbitro UPSERT y políticas capaces de
  ocultar u omitir filas. Otros cambios externos quedan bajo gobierno del destino.
- La transacción es atómica dentro del motor remoto según sus garantías; metadata
  Trackvance y destino no comparten 2PC. `UNKNOWN` preserva esa incertidumbre.
- Las cuentas de destino deben usar privilegio mínimo según la estrategia y la
  red/TLS deben administrarse en el entorno. Data Delivery es escritura real.
- La implementación no constituye garantía de alta disponibilidad, throughput o
  volumen productivo. Los resultados medidos no se extrapolan a cargas no ejecutadas.
- Triggers/rules del target pueden transformar o suprimir DML. `rows_written`
  representa filas intentadas por Trackvance, no una lectura post-trigger exacta.
  `PENDING_REPAIR` requiere intervención; no hay reconciliador automático.

## Verificación

Unitarios y pruebas de API cubren validación, quoting, tipos, estrategias,
preflight, evidencia, autorización, lanes y recuperación. El runner aislado
`scripts/tests/delivery_cycle.py` levanta PostgreSQL/SQL Server de destino con
fixtures propios y puede invocar `frontend/tests-e2e/delivery.spec.ts`. El job
`delivery-e2e` no usa la instalación principal. El resultado de publicación se
registra en `docs/development/validation.md`; local y GitHub Actions se informan
por separado y nunca se infieren uno del otro.
