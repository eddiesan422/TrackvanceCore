# ADR 0026 — Gobierno controlado y aprobación estricta compartida

Fecha: 5 de octubre de 2026. Estado: aceptada e implementada en el árbol de
trabajo 0.8.0. Base comprobada: `d9b6856`, `feat/local-prototype`.
La aceptación de esta decisión no certifica el volumen, CI final ni promoción.

## Problema y decisión

`Dataset.domain` era texto libre, con default Operaciones; el nombre de una
salida Intake terminada en « · Aprobados » servía para localizar el activo. El
estado técnico SUCCESS y la existencia de una salida no demuestran una calidad
sin rechazos ni advertencias. La navegación no diferenciaba gobierno vigente,
aprobación histórica y habilitación actual. Un reporte multifuente tampoco puede
representarse correctamente creando un Run ligado a una entrada ficticia.

Se conserva el monolito modular, el RBAC persistente, auditoría, JobQueue,
StorageProvider y ArtifactLink. `governance_models.py`, `governance.py` y
`governance_api.py` separan identidades, evaluación y presentación HTTP. La
ejecución de Reportes usa entidades propias, descritas en ADR0027; no altera
el contrato obligatorio de Run.

## Identidades y migración conservadora

`MacroDomain` es único por organización/nombre normalizado; `DataDomain` por
organización/macrodominio/nombre normalizado. La normalización sólo colapsa
espacios y mayúsculas; la etiqueta visible se conserva. Renombrar incrementa
versión mediante CAS y conserva ID. Desactivar mantiene relaciones y evita
nuevas selecciones; una clasificación desactivada deja de habilitar Reportes.
No hay endpoint de borrado de dominios referenciados.

Dataset añade FK opcionales macro_domain_id/domain_id, identidades opcionales
de responsable/gestor/custodio, information_classification UNKNOWN y
governance_version 1. El par dominio/macrodominio se valida dentro de la
organización. Cero selecciones o sólo macrodominio son estados válidos para
crear/versionar. Para cambiar macrodominio conservando un dominio incompatible
se exige elegir otro o limpiarlo explícitamente.

`0017_catalog_reports` parte de 0016 y crea trece tablas específicas de
gobierno/Reportes y las columnas Dataset/Job. Las migraciones 0001–0016 no se
reescriben. Los nuevos campos de datasets históricos quedan NULL/1/UNKNOWN:
no se inventan macrodominios a partir de áreas, no se modifican archivos y no
se crean versiones de datos por clasificar. Los textos domain/owner/criticality
se conservan. La API nueva deja vacío el texto de área cuando no se proporciona
un valor explícito compatible; la UI usa únicamente los dos selectores.

Las salidas nuevas se localizan por
organización/entrada lógica/identidad raíz del contrato, con UNIQUE y FK. El
nombre es presentación y maneja colisiones; cada revisión del mismo contrato
reutiliza su identidad lógica. La FK al contrato es nombrada y use_alter para
resolver el ciclo Dataset/Configuration en PostgreSQL. Configuraciones no tienen
endpoint de borrado; la FK nullable permite SET NULL en el desmontaje de una
base desechable sin borrar versiones o runs.

La herencia histórica sólo se reconstruye en lectura si todas las versiones
del activo demuestran el mismo dataset padre mediante parent_version_id,
source_run_id, Run.output_version_id, Run.dataset_version_id y
Configuration.dataset_id de la organización. Una coincidencia de nombre no
sirve. Relación insuficiente queda sin clasificación comprobable. El dataset
publicado por Reportes tiene gobierno propio; sus antecedentes no lo reclasifican.

## Historia, glosario y documentación

`GovernanceHistory` conserva snapshots y actor por revisión de gobierno. Los
runs nuevos incluyen governance_snapshot en el plan; las ejecuciones viejas
no reciben la clasificación actual retrospectivamente. Un cambio de gobierno
no modifica DatasetVersion, Configuration, decisiones o manifests previos.

`GlossaryTerm` usa identidad estable, definición, actividad y revisión CAS.
`ColumnDocumentation` se vincula a DatasetVersion, schema_hash y nombre de
columna. Una versión nueva o una columna homónima no adopta documentación
automáticamente. `GlossaryAssociation` relaciona el término con un dataset o
con una columna de una versión validada. Quitar una asociación exige permiso y
auditoría; no altera tipos físicos. Identidades inactivas conservan historia y
aparecen con actividad explícita; ser responsable no concede acceso.

## Aprobación estricta y disponibilidad

`strict_approval` es el único evaluador de metadata para Catálogo y Reportes.
Requiere Intake SUCCESS terminado, APPROVED tanto en Run como métricas,
entrada positiva, processed_rows/valid_rows/output_rows iguales a la entrada y
a los conteos de ambas versiones, error_rows/warning_rows/discarded_rows cero,
contabilidad completa por regla, controles habilitados y evaluación positiva.
Compara el multiconjunto de identidad/código/columnas/severidad de controles y
evidencia, incluyendo los shorthands históricos sin rule_id individual.

Los productores Polars y Spark cuentan validation_coverage_rows como la unión
de identidades de filas realmente evaluadas. No suman reglas superpuestas para
deducir filas distintas. Una condición no aplicable conserva skipped_count y
puede coexistir con otras reglas evaluadas; ninguna evaluación positiva o
transformaciones sin controles no bastan. La cobertura no promete que un
contrato definido cubra todas las necesidades de negocio.

Una entrada de 10.000 filas con 100 rechazadas y salida de 9.900 nunca cumple,
aunque acceptance_rate redondee a 100 o exista una salida. Vacíos, advertencias,
metrics ausentes, reglas ausentes y relaciones incompletas producen códigos
funcionales. La evidencia histórica insuficiente requiere nueva validación;
no se fabrican conteos. `StrictApproval` indexa idempotentemente tras preparar
salida y manifest dentro del mismo commit; es un índice, no otra autoridad.

`dataset_eligibility` agrega gobierno completo/activo, disponibilidad de
metadata, permisos vigentes y bloqueos. Se devuelve approved/eligible
y availability por separado. La navegación no lee Parquet ni anuncia integridad física:
verified_bytes=false. Clasificar después puede habilitar una aprobación
suficiente sin repetir Intake.

`verify_strict_approval_artifacts` es exclusivo de ejecución. Toma metadata
confirmada en una transacción de lectura independiente, la desprende y termina
la transacción antes de hashear. Verifica original, canónicos de entrada/salida,
descriptor/todas las partes, tamaños/hashes, footer/conteos/columnas y manifest.
Compara entrada/salida/revisión, config_hash efectivo y almacenado, métricas y
schema_hash de salida. Las ejecuciones nuevas incluyen outputs en manifest;
los manifests históricos no se reescriben. Una aprobación deja de ser utilizable
si bytes o evidencia ya no pasan la comprobación completa.

## Autorización transitiva y restricciones

`DatasetSecurityDependency` conserva las fuentes de seguridad de un derivado.
La alta comprueba organización, duplicados y ciclos. Intake añade su entrada y
las fuentes de referencia que consumieron sus validaciones;
versiones históricas mantienen parent_version_id como otra prueba verificable.
El recorrido iterativo limita a 128 activos y falla cerrado ante referencias
rotas/ciclos. El creador, un rename o un Intake posterior no eliminan dependencias.

`DatasetBlock` tiene motivo, scope REPORT o CONTENT, actividad, versión y
liberación explícita con usuario/fecha/motivo. REPORT impide nuevos usos en
Reportes a través de cualquier descendiente; CONTENT protege lectura/uso de
contenido también en rutas nativas. La metadata autorizada de un bloqueado
permanece visible para entender el motivo. Las aprobaciones históricas no se
borran. Liberar una dependencia es una acción distinta y auditable, nunca un
efecto de clasificar o publicar como Administrator. Si una nueva derivación
consume otra vez una fuente previamente desacoplada, restablece esa dependencia
y registra un evento auditable con su nueva versión.

Los guards se aplican a perfil/muestra, artifacts originales/canónicos y sus
partes, resultados/exports de Run, uso de entrada/referencias y Delivery. El
worker relee autorización del iniciador antes de ejecutar y después del cómputo,
antes de publicar resultados o una versión derivada. Delivery revalida durante
la preparación y antes de persistir STARTED. Descargar una parte
por Artifact.id también recorre DATASET_PART hasta su descriptor; no elude el
guard de la versión. Los scopes anteriores no reclasifican ni bloquean
indiscriminadamente activos históricos ajenos a las nuevas relaciones.

Administrator sigue resolviendo el catálogo completo. Se agregan permisos de
catálogo/gobierno/dominios/glosario/restricciones y cinco acciones de Reportes;
no se añaden grants a roles personalizados. Consultar metadata, descargar y
publicar son permisos separados. La matriz exhaustiva se genera del runtime.

## Navegación y verificación

Catálogo usa Macrodominio → Dominio → tipo, con Pendientes de clasificación y
seis secciones del panel. Las listas Dataset y Run utilizan selección,
conteos, filtros y paginación SQL; la clasificación heredada se resuelve por
CTE de metadata, con tipos de identidad explícitos compatibles con PostgreSQL.
El panel de linaje pagina y cuenta en SQL y aplica permiso del módulo y
propietario de la ejecución de Reportes antes de mostrar vínculos. La clausura
de seguridad usa UNION distinto y cotas, sin
visitar recursivamente filas de datos. Intake usa el índice de aprobaciones;
ReconOps exige CONFORME, Sentinel HEALTHY y Delivery COMMITTED, nunca UNKNOWN.
Reportes referencia definiciones multifuente sin duplicar el dataset publicado.
El catálogo de Reportes selecciona la última revisión con una ventana SQL y
expande sus fuentes JSON en PostgreSQL/SQLite: filtra todas las fuentes por
acceso y al menos una por los filtros de navegación antes de contar y paginar.

La suite `test_governance_catalog.py` usa Intake, manifests y archivos reales en
DB/storage desechables; cubre negativos, integridad, clasificación tardía,
condiciones, metadatos sin lectura física, conflictos, glosario y rutas nativas
de derivados/descendientes. La paridad de migración se prueba en SQLite y
PostgreSQL. Docker integrado, volumen, backup/restore, CI del SHA final y
actualización habitual se informan en evidencias separadas; no se deducen de
estas pruebas unitarias.
