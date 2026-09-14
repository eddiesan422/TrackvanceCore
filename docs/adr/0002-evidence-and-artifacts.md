# ADR 0002 — Identidad, Parquet y evidencia inmutable

Fecha: 2026-09-13. Estado: aplicado en 0.2.0. Complementa ADR 0001.

## Problema y decisión

Un CSV aceptado por Intake aparecía como archivo original y la evidencia
guardaba sólo nombres visibles. Se conserva el monolito modular y se separa
la identidad del artifact de la fuente de una DatasetVersion.

`Artifact` persiste ID estable, organización, kind, nombre, ruta local,
SHA-256, tamaño y media type. `FileArtifactStore` comprueba confinamiento de
rutas e integridad; publica archivos completos sin reemplazar bytes previos.
`ArtifactLink` conserva relaciones tipadas e idempotentes. Cada intento del
worker tiene ruta propia; un worker sin lease no puede promover evidencia.

- `ORIGINAL_UPLOAD`: bytes recibidos del usuario.
- `CANONICAL_PARQUET`: representación canónica de un upload.
- `INTAKE_ACCEPTED`: salida aceptada de Intake, también Parquet.
- `INTAKE_ERRORS`, `RECON_RESULTS`, `SENTINEL_PROFILE`, `RUN_MANIFEST`:
  evidencia derivada de las ejecuciones.
- `EXPORT_XLSX`: informe generado a partir de un run completado.
- `GENERATED_DEMO`: fuente sintética identificada, distinta de un upload.

Las nuevas versiones Intake tienen `source_type=INTAKE_OUTPUT`,
`original_artifact_id=null`, `canonical_artifact_id` al Parquet aceptado,
`parent_version_id` al input y `source_run_id` al run. Los links `DERIVED_FROM`,
`INTAKE_ACCEPTED_FROM` y `RUN_OUTPUT` permiten navegar el origen. Una versión
interna usa registros desde 1; un CSV original usa líneas físicas iniciales
de cada registro, incluso con celdas multilínea. Se registra la política en
las métricas/manifiesto y se adapta el encabezado del reporte.

## Manifest schema 2

`initiated_by` es `{type: USER|WORKER|SYSTEM, id, display_name}`. El nombre es
presentación; el ID es identidad. Cada artifact resultante incluye
`artifact_id`, `kind`, `name`, `sha256`, `size_bytes`, `media_type`.

El manifest además contiene run, módulo, fechas UTC, versión de aplicación,
plan/versión de motor, ambos inputs cuando corresponda, hashes originales y
canónicos, IDs/versiones/schema hashes, configuración efectiva/config hash,
config hash almacenado, métricas y output DatasetVersion. La normalización
de claves está dentro de la configuración efectiva y del plan.

`configuration_hash` usa JSON UTF-8 con claves ordenadas y separadores
canónicos. Cambiar una normalización declarada cambia el hash. El manifest
se registra en ArtifactStore después de escribirse; no se incluye su propio
hash dentro de sus bytes. Los Excel posteriores se enlazan con `EXPORT_OF`
y no se añaden modificando manifests históricos.

## Compatibilidad y migración

Sólo se crea `0002_evidence_v2`; `0001_initial` no se modifica. Añade las
tablas de artifacts/linaje/métricas y campos de identidad/versionado, sin
reescribir los snapshots de negocio existentes. La adopción de una SQLite
sin Alembic valida la estructura original antes de marcar v1 y actualizar;
una estructura desconocida se rechaza.

El backfill registra archivos existentes y crea links cuando tiene evidencia,
sin cambiar sus bytes ni recalcular perfiles v1. Si un archivo falta, se
reporta; no se inventa un hash. Un actor histórico se resuelve contra usuarios
de su organización cuando es inequívoco; de otro modo obtiene identidad
legacy estable, marcada como tal, sin fingir una atribución personal.

`manifests.adapt_manifest` concentra la lectura compatible. v1 se adapta
únicamente en memoria para los consumidores actuales; descarga de evidencia
devuelve el JSON histórico intacto. Metadatos faltantes quedan explícitos
como incompletos. Versiones desconocidas se rechazan. No hay condicionales
de versión dispersos en motores/exportadores.

La identidad del actor de auditoría tiene tipo, ID, nombre opcional,
`request_id`, `run_id`, sujeto, evento, fecha y metadata permitida. Los eventos
`EXPORT_DOWNLOADED`, `ARTIFACT_DOWNLOADED`, `EVIDENCE_DOWNLOADED` mantienen
autorización por organización y permisos del rol. Descargar no cambia la
decisión de negocio ni el estado técnico del run.

## Verificación

Pruebas de upgrade/downgrade/paridad ORM en SQLite y PostgreSQL, preservación
de filas, snapshots del adaptador v1, manifest v2, hashes, confinamiento,
idempotencia, linaje, retry, corrupción, auditoría y XLSX. La comprobación
local de preservación está en `.codex-local/correction-baseline/`.
