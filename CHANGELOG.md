# Changelog

## 0.5.0 — 2026-09-23 — Data Delivery controlado

- Incorpora **Data Delivery** como módulo de salida separado de `DatasetSource`,
  con destinos versionados PostgreSQL y SQL Server, credenciales cifradas fuera
  de metadata y un puerto `DataSink` específico de escritura.
- Añade preview de mapping, preflight obligatorio al publicar y antes de ejecutar,
  configuraciones inmutables, idempotencia de creación de runs, `DeliveryAttempt`
  y receipt/manifest enlazados al Run, DatasetVersion y revisión exacta del destino.
- Implementa targets `EXISTING_TABLE` y `CREATE_TABLE`, tipos lógicos `STRING`,
  `INT64`, `DECIMAL`, `DATE`, `TIMESTAMP` y `BOOLEAN`, y estrategias
  `CREATE_AND_LOAD`, `APPEND`, `OVERWRITE` y `UPSERT`. `UPSERT` exige claves
  explícitas, no nulas, únicas en la fuente y respaldadas por PK/unique en destino.
- Introduce la lane persistida `DELIVERY`, un `delivery-worker` independiente y
  heartbeats separados. El worker `DEFAULT` no monta secretos de origen ni de
  destino; el delivery-worker monta únicamente artifacts y secretos de destino;
  la API monta ambos almacenes para descubrimiento y administración.
- Distingue `STARTED`, `COMMITTED`, `FAILED` y `UNKNOWN` en intentos remotos.
  Una confirmación de commit perdida queda `UNKNOWN`, no se presenta como fallo ni
  éxito y no se reintenta a ciegas.
- Prepara identificadores, tipos y payload antes de `STARTED`; toma el lease antes
  de reconciliar y conserva el orden de locks Run→Job frente a cancelación.
  Un resultado remoto conocido prevalece sobre cancelación tardía. PostgreSQL
  revalida la constraint UPSERT nombrada y rechaza RLS en `OVERWRITE`; SQL Server
  rechaza `IGNORE_DUP_KEY` y, para `OVERWRITE`, falla cerrado ante FILTER security
  policies invisibles o activas.
- Endurece exactitud: strings Unicode variables con longitud UTF-16/collation
  compatible, timestamps con offset y precisión máxima 6, y decimales sin float.
  El mapping conserva el tipo lógico de la DatasetVersion y rechaza conversiones
  funcionales entre STRING, número, fecha, timestamp y booleano.
- Alinea adquisición y Delivery: sólo `timestamptz` y `datetimeoffset(0..6)` se
  etiquetan `TIMESTAMP`; temporales SQL sin zona y `datetimeoffset(7)` se preservan
  como `STRING`, sin inventar zona ni crear snapshots imposibles de publicar.
- Añade la experiencia `/delivery`: administración y prueba de destinos, builder
  guiado, mapping, metadata de tabla, preview/preflight, publicación, ejecución,
  intentos, estado `UNKNOWN` y descarga de receipt/manifiesto.
- La migración aditiva `0008_data_delivery` crea
  `delivery_destinations`, `delivery_destination_versions` y `delivery_attempts`,
  y agrega `jobs.lane`, sin reescribir configuraciones o ejecuciones históricas.
- Backup, restore, reset, doctor y verificación de almacenamiento incluyen
  `delivery_credentials`, `delivery_keys`, las tres tablas nuevas, lanes,
  heartbeats y linaje de entrega. El conjunto de backup contiene material sensible.
- Certifica restore real desde la baseline exacta 0.4.1/0007: 21 tablas y estado
  legacy idénticos, jobs históricos en lane DEFAULT y tablas Delivery vacías tras 0008.
- Cierra carreras TOCTOU de backup mediante copia privada, apertura exclusiva,
  `fsync` y reverificación de tamaño/hash antes de consumir cada componente.
- CI declara un job aislado `delivery-e2e` con PostgreSQL y SQL Server reales; el
  runner no reutiliza ni destruye la instalación principal.
- Reutiliza temporalmente permisos existentes de conexiones, configuraciones,
  ejecuciones y artifacts. Permisos Delivery granulares y roles específicos
  permanecen pendientes.
- **Certificación local 0.5.0:** 869 pruebas Python y 128 Vitest aprobadas;
  Ruff, Mypy, ESLint, TypeScript y build PASS; Playwright 20 aprobadas/5 opt-in
  omitidas; smoke 84/84, Conexiones 62/62, Delivery real 92/92 y recovery PASS.
  GitHub Actions: [siete jobs SUCCESS](https://github.com/eddiesan422/TrackvanceCore/actions/runs/35875426350)
  sobre `57c0319ae2f0ef27ae63eced8e354258efc0854d`, sin reintentos.

## 0.4.1 — 2026-09-21 — configuración guiada por esquema

- Simplifica la carga y el versionado de datasets: elimina la entrada redundante
  de identificadores por nombre y conserva el selector basado en el esquema
  inspeccionado, incluida la opción **Todos**.
- Convierte **Responsable** en un catálogo reutilizable para Data Intake,
  ReconOps y Sentinel. El usuario puede elegir un valor existente o agregar uno
  nuevo al crear la configuración, sin cambiar el contrato persistido `owner`.
- Presenta las transformaciones declarativas con nombres y ayudas funcionales,
  ejemplo acorde con sus parámetros, orden explícito y una vista previa
  **Antes / Después** sobre hasta ocho valores reales. La muestra es informativa;
  no modifica la DatasetVersion ni cambia la semántica del backend.
- Explica las claves simples y compuestas de ReconOps, traduce las opciones de
  normalización a lenguaje de negocio y muestra su efecto sobre muestras reales
  de origen y destino. Los valores persistidos `trim`, `case`,
  `unicode_normalization`, `NONE`, `UPPER`, `LOWER`, `NFC` y `NFKC` permanecen
  compatibles.
- Sustituye la escritura manual de columnas requeridas y columnas observadas por
  nulos de Sentinel por selectores múltiples del esquema real del dataset.
- Al cerrar sesión limpia la caché de la sesión y devuelve a la pantalla inicial
  de Trackvance.
- Esta versión es una corrección de experiencia de usuario: no añade migraciones,
  no modifica la ejecución de reglas y no reescribe configuraciones históricas.
- Certifica 533 pruebas backend/scripts, 119 pruebas frontend en 13 archivos,
  24 escenarios Playwright distintos y seis de seis jobs de GitHub Actions.

## 0.4.0 — 2026-09-19 — madurez funcional local

- Adopta el roadmap funcional hasta el punto 9; productización y Data Delivery
  permanecen fuera de este ciclo. Conserva Conexiones PostgreSQL/SQL Server,
  cinco formatos de archivo y el monolito modular con sus puertos existentes.
- Amplía Intake con unicidad compuesta, longitud, comparación entre columnas,
  condiciones declarativas y referencias a DatasetVersions inmutables. El editor
  usa columnas reales; resultados y Excel conservan identidad de regla, filas
  evaluadas/fallidas/excluidas, severidad, evidencia y linaje de referencias.
- Completa Recon con transforms explícitos por fuente, políticas de nulos,
  múltiples comparaciones y agregaciones SUM/COUNT por columna en 1:N/N:1.
  Rechaza agregaciones ambiguas y conserva las categorías históricas.
- Añade programación local de Sentinel, revisiones inmutables, ocurrencias
  transaccionales, agrupación de atrasos, control de solapamiento, histórico
  separado por método/versión, evolución y alertas internas navegables.
- Madura excepciones con asignación estable, prioridad, SLA, fecha objetivo,
  vencimientos, comentarios, adjuntos con StorageProvider, ASSIGNED/REOPENED y
  política automática deshabilitada por defecto. RESOLVED exige validación
  posterior; una regla sin filas evaluadas no demuestra una corrección.
- Añade administración local de usuarios, roles y permisos, activación,
  contraseñas Argon2, revocación de sesiones y protección del último administrador.
- Incorpora reset con plan verificable y confirmación explícita, backup Docker
  PostgreSQL/artifacts/credenciales/claves, restore fresco, hashes y diagnóstico
  integral. Las pruebas destructivas operan exclusivamente en proyectos aislados.
- Añade un framework de volumen con datos sintéticos reales, medición de recursos,
  límites de seguridad y distinción entre volumen medido y no ejecutado.
- Crea migraciones aditivas `0006_local_identity_exceptions` y
  `0007_monitor_scheduling`, además de `0005_external_connections` del ciclo de
  Conexiones. No reescribe migraciones, snapshots, configs o runs históricos.
- Amplía pruebas unitarias, integración, interfaz y ciclos Docker/Playwright;
  agrega backup/restore a CI y el flujo avanzado de fuentes a la certificación.
  Resultados y límites reales se publican en `docs/development/validation.md`.
- Certifica 533 pruebas backend/scripts, 109 de frontend, 24 escenarios distintos
  de navegador y seis jobs de GitHub Actions. El backup usa streaming privado
  compatible con Windows/Linux; no depende de permisos de escritura en un bind.
- Sincroniza las ediciones consecutivas del E2E de excepciones con la revisión
  visible guardada; evita adelantarse al render al consultar únicamente la API,
  sin modificar la funcionalidad ni introducir reintentos.
- Mide un fixture variado de 106.194.531 bytes con ambos motores y los tres
  módulos. Los tiers superiores quedan sin ejecutar por preflight; los límites
  por defecto no se elevan ni se presenta PySpark como disponible.

## 0.3.0 - evolución Conexiones — 2026-09-19

- Añade el módulo Conexiones con creación, prueba, edición, deshabilitación y
  baja lógica de fuentes PostgreSQL y SQL Server.
- Implementa `PostgreSQLDatasetSource` y `SQLServerDatasetSource` detrás del
  puerto común; descubre schemas, tablas/vistas y tipos, y ofrece preview acotado.
- Registra selecciones como datasets con snapshots Parquet inmutables, refresh,
  configuración versionada y linaje completo hacia fuente y objeto originales.
- Añade `SecretStore` y un proveedor local con cifrado autenticado, clave en un
  volumen separado, referencias opacas, aislamiento por organización y errores
  sanitizados. Las respuestas, auditoría, logs y metadata excluyen credenciales.
- Incorpora la migración aditiva `0005_external_connections`, RBAC y auditoría
  de Conexiones, sin modificar configuraciones ni runs históricos.
- Certifica ambos motores reales con usuarios SELECT, errores de acceso,
  permisos, tablas/vistas, tipos, caída/reconexión, Intake, Excel y Playwright.
- Declara Data Delivery como puerto futuro separado; esta evolución no escribe
  en fuentes externas ni añade una dependencia cloud.

## 0.3.0 — 2026-09-16

- Documenta Docker Compose con PostgreSQL y volumen persistente como instalación
  local principal; SQLite continúa como opción de desarrollo directo.
- Introduce los puertos `StorageProvider`, `DatasetSource`, `ExecutionEngine` y
  `JobQueue`, con adaptadores locales para archivos, lectores, Polars/Python y
  cola transaccional en base de datos. Conserva los contratos y datos históricos.
- Los servicios publican y materializan artifacts mediante el proveedor de
  almacenamiento, sin construir rutas desde el directorio del volumen.
- Distingue arquitectura implementada, fronteras preparadas y capacidades de
  producto futuras, sin añadir servicios cloud, Redis/Celery ni PySpark al
  entorno local. Amplía la documentación de arquitectura y evolución.
- La carga de datasets admite CSV, Excel XLSX, JSON tabular/JSON Lines,
  Parquet y TXT/TSV delimitado mediante un registro extensible de lectores.
- Detecta el formato, inspecciona una muestra acotada, permite elegir hoja de
  Excel o delimitador TXT y normaliza todas las fuentes antes del motor.
- Conserva en cada DatasetVersion el lector, opciones efectivas, esquema
  nativo y semántica de numeración mediante Alembic
  `0003_dataset_ingestion_metadata`.
- Data Intake obtiene el esquema de la última versión desde metadata y ofrece
  selectores múltiples para reglas de columnas.
- El esquema puede actualizarse explícitamente leyendo solo el footer del
  Parquet canónico, sin recorrer las filas del dataset.
- La inspección previa permite corregir tipos lógicos y seleccionar o escribir
  identificadores en la carga inicial y en cada versión nueva.
- El formulario permite agregar áreas de negocio y el listado identifica el
  origen de la última versión como Manual, Data Intake, Demo u otro conector.
- Los selectores de columnas de Data Intake incluyen una acción “Todos”; para
  valores positivos se limita a los campos numéricos elegibles.
- La carga reconoce nombres de dataset existentes y agrega el archivo como una
  nueva versión; la API devuelve `DATASET_NAME_EXISTS` a clientes directos.
- La tabla de datasets permite ordenar Área, Registros, Versiones, Estado y
  Última actualización en sentido ascendente o descendente.
- El Centro de Control incorpora filtros globales por período, dataset, módulo,
  estado y criticidad, con indicadores y variaciones frente al período anterior.
- Añade atención priorizada con acciones directas, evolución temporal de salud,
  datasets afectados, resúmenes operativos por módulo y ejecuciones enriquecidas.
- Las excepciones conservan la configuración y el run de origen y solo pueden
  resolverse después de una ejecución posterior conforme del mismo snapshot.
- Añade `PENDING_VALIDATION`, evidencia técnica y enlace al run que confirmó la
  corrección; los cierres `DISCARDED`, `ACCEPTED` y `NOT_APPLICABLE` requieren
  un motivo y permanecen separados de `RESOLVED`.
- Añade Alembic `0004_exception_validation` y conserva la lectura de estados
  históricos sin reescribir casos ni ejecuciones anteriores.

## 0.2.0 — 2026-09-14

- Corrige profiling observado, fechas ISO e identificadores con ceros iniciales.
- Publica outputs Intake en Parquet con artifacts y linaje inmutable.
- Actualiza manifests a schema 2 y auditoría con actores estructurados.
- Añade normalización explícita y reglas ampliadas de Intake, ReconOps y Sentinel.
- Sustituye la exportación principal CSV por informes Excel estructurados y seguros.
- Añade Alembic `0002_evidence_v2`, PostgreSQL/Docker, respaldo y pruebas integrales.
- Conserva lectura de manifests v1, configuraciones legacy y export CSV deprecated.

Los límites pendientes de PySpark, gran volumen, privacidad avanzada y backup
automatizado de PostgreSQL se documentan en `docs/development/validation.md`.
