# Changelog

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
