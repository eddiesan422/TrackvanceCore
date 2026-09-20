# ADR 0006 — Puertos de almacenamiento, fuentes, ejecución y cola

Fecha: 2026-09-16. Estado: aplicado.

Actualización 2026-09-19: PostgreSQL y SQL Server ya implementan esta frontera.
La configuración, secretos, snapshots y separación de Data Delivery se deciden en
[ADR 0007](0007-external-connections.md). Las menciones a esos dos adaptadores como
futuros describen el estado histórico al aceptar este ADR.

## Contexto

El prototipo ya separaba los lectores de archivos y compilaba reglas portables,
pero varios servicios conocían directamente el directorio local, el worker
invocaba una función concreta y la creación de ejecuciones construía el registro
de cola. Esos acoplamientos no impiden operar localmente, pero harían necesario
modificar servicios de Data Intake, ReconOps y Sentinel al adoptar object
storage, Celery o un motor distribuido.

## Decisión

Mantener el monolito modular y declarar cuatro fronteras estructurales:

- `StorageProvider` es el almacenamiento interno, inmutable y con ámbito de
  organización. Publica bytes, materializa un artefacto para el motor y asigna
  un staging temporal sin exponer el layout del volumen a los servicios.
  `FileArtifactStore` es el adaptador local respaldado por el volumen
  persistente. Conservamos `artifact_store` como alias concreto para el
  backfill de rutas históricas.
- `DatasetSource` adquiere datos y produce `DatasetReadResult`.
  `LocalFileDatasetSource` delega CSV, XLSX, JSON, Parquet y TXT al registro de
  `DatasetReader`. PostgreSQL y SQL Server ya implementan el mismo puerto; S3,
  Azure Blob o API pueden entregar el modelo común sin cambiar el motor de calidad.
- `ExecutionEngine` ejecuta un `Run` persistido completo. El adaptador
  `LocalExecutionEngine` usa la implementación actual Polars/Python. Este puerto
  es distinto de `portable_engine.ProcessingEngine`, que compila y evalúa la
  semántica de reglas dentro de un motor.
- `JobQueue` publica una ejecución. `DatabaseJobQueue` conserva la cola local,
  sus estados y los leases existentes. Un adaptador Redis/Celery podrá reemplazar
  la publicación y el consumo sin cambiar la API ni la creación de `Run`.

Los servicios reciben puertos opcionales sólo en los puntos necesarios para
pruebas y composición. Los defaults siguen siendo locales. Los identificadores,
tablas, paths históricos, hashes, manifests y estados de jobs no cambian.

## Límites actuales

Se instalan adaptadores de archivo, PostgreSQL y SQL Server; el almacenamiento y
la ejecución siguen siendo locales. El planner puede identificar una carga que
requiere PySpark y mantiene `ENGINE_UNAVAILABLE` mientras dicho adaptador no esté
instalado; no simula procesamiento distribuido. Redis/Celery y object storage se
mantienen como adapters futuros y no son dependencias del prototipo.

Esta es una frontera incremental, no una implementación cloud plug-and-play.
El esquema conserva locators históricos en campos llamados `*_path`, y los
puertos de ejecución y cola aún reciben la sesión SQLAlchemy y modelos ORM. Un
adaptador S3/Azure Blob deberá materializar objetos en un cache local acotado,
persistir su locator y migrar las referencias locales existentes. Un adaptador
Redis/Celery deberá definir el límite transaccional entre `Run` y el mensaje.
Esos cambios pertenecen a infraestructura; la semántica declarativa y los
módulos Intake, ReconOps y Sentinel no necesitan reescribirse.

`ExecutionPlanner`, el heartbeat y la inicialización SQLite pueden consultar
disco local porque pertenecen a la infraestructura del runtime local. La lógica
de módulos ya no construye rutas desde `STORAGE_DIR`: publica y materializa por
el puerto. Las funciones de registro directo y paths concretos permanecen en el
adaptador local exclusivamente para compatibilidad y backfill.

## Consecuencias

Docker Compose continúa operando con PostgreSQL y volumen local persistente.
La evolución de infraestructura requiere nuevos adaptadores y una migración de
locators/entrega de jobs, mientras las reglas, configuraciones, trazabilidad,
auditoría y evidencia conservan sus contratos funcionales. Las pruebas verifican
los adapters locales y la sustitución por doubles de cola, fuente y ejecución.
