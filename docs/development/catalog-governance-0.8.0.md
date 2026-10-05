# Catálogo y gobierno del dato 0.8.0

Esta guía describe la implementación de gobierno, su API y la ventana Catálogo.
Los resultados ejecutados se consultan en validación/evidencias del ciclo 0.8.0;
la guía no constituye certificación del despliegue habitual o CI final.

## Clasificar y documentar un activo

En carga normal, rápida o desde una conexión SQL, **Macrodominio** y **Dominio**
son opcionales. El segundo selector sólo ofrece dominios activos del primero.
Sin ninguno o con sólo macrodominio el archivo puede adquirirse, versionarse y
validarse; aparece en **Pendientes de clasificación**. Una combinación incompatible
se rechaza y debe corregirse o quedar incompleta explícitamente. **Crear** en el
selector requiere domains:manage; un usuario que sólo carga puede consultar
las entidades sin recibir ese permiso de administración.

El área anterior es información heredada. No se traduce automáticamente a un
macrodominio ni compite como tercer selector. Renombrar/desactivar una entidad
conserva su identidad y asociaciones; desactivar evita nuevas selecciones y
deja de habilitar fuentes para Reportes. Una etiqueta duplicada por espacios o
mayúsculas devuelve DOMAIN_DUPLICATE. Guardar una edición obsoleta devuelve
VERSION_CONFLICT y exige recargar para no sobrescribir el trabajo de otra persona.

Desde **Resumen y gobierno** se edita descripción, clasificación, responsable
de negocio, gestor, custodio técnico, criticidad y clasificación de información.
UNKNOWN no equivale a pública. Elegir una persona no otorga permisos; personas
inactivas y valores anteriores permanecen identificados como historia. La
información descriptiva no implementa masking, retención ni cifrado de filas.

Una salida Intake hereda gobierno vigente de su entrada comprobada y se edita
en esa entrada. Un dataset generado por Reportes tiene clasificación propia:
puede combinar fuentes de distintos dominios, quedar pendiente y clasificarse
posteriormente. Reclasificar un antecedente no cambia esa clasificación propia.
Guardar gobierno no reescribe archivos, versiones, contratos o ejecuciones.

## Localizar e inspeccionar

El árbol carga los nodos por petición: macrodominio, dominio y tipo de recurso.
Los tipos son Datasets, Data Intake, ReconOps, Sentinel, Data Delivery y Reportes.
Hay búsqueda, filtros de dominio/tipo/estado/responsable y paginación en servidor.
Los contadores corresponden al ámbito autorizado. Ningún nodo lee Parquet para
dibujarse. Los enlaces profundos mantienen activo/versión/contrato al recargar.

Datasets contiene los activos autorizados, incluidas generaciones y entradas
pendientes/rechazadas. Intake contiene aprobaciones estrictas; ReconOps contiene
ejecuciones SUCCESS/CONFORME; Sentinel SUCCESS/HEALTHY; Delivery SUCCESS/COMMITTED.
Los otros estados siguen en su módulo/historia y no se ocultan como si no existieran.
Un reporte puede referenciarse desde varios dominios y conserva una sola identidad.

El panel tiene seis secciones cargadas independientemente:

| Sección | Información y acciones |
| --- | --- |
| Resumen y gobierno | Gobierno vigente/heredado, campos editables, aprobación/elegibilidad separadas y restricciones vigentes |
| Columnas y glosario | Esquema registrado, documentación funcional por versión/schema_hash, términos asociados a dataset o columna |
| Versiones | Identidades, números propios, origen, conteos, hashes y artifacts de versiones inmutables |
| Calidad y contratos | Runs reales, contrato/revisión/entrada/salida, decisión y evaluación estricta con motivos |
| Linaje | Relaciones verificables, enlaces/nombres autorizados y dependencias de seguridad; sin correspondencia inventada fila a fila |
| Historial | Snapshots de gobierno con revisión, actor e instante; no atribuye gobierno actual a runs anteriores |

Documentar una columna no cambia su tipo. La documentación se guarda para la
versión seleccionada: una columna que desaparece y otra homónima futura no
reciben la misma descripción automáticamente. Los términos desactivados
conservan sus asociaciones históricas y no admiten asociaciones nuevas.

## Comprender las cuatro evaluaciones

**Clasificación completa** requiere ambos IDs compatibles y activos. **Aprobación
estricta** requiere una ejecución Intake terminada correctamente con APPROVED,
entrada no vacía, controles realmente evaluados, cero errores/advertencias/descartes
y conteos/evidencia completos. **Disponibilidad** de navegación expresa existencia
de metadata canónica READY; la comprobación completa de bytes ocurre en el worker.
**Habilitación para Reportes** agrega clasificación vigente, permisos y ausencia de
bloqueos a la aprobación y disponibilidad.

SUCCESS, un nombre que contenga Aprobados, una salida o acceptance_rate redondeada
a 100 no son equivalentes a aprobación estricta. Por ejemplo, 10.000 entradas,
100 rechazos y 9.900 aceptadas no habilitan Reportes. Contratos con transforms
pero sin controles, entrada vacía, cero evaluación efectiva o métricas antiguas
incompletas producen motivos explícitos. Las condiciones no aplicables conservan
skipped_count; no se suman reglas que evalúan las mismas filas para fabricar
cobertura. La suficiencia funcional de los controles sigue dependiendo del contrato.

Si la aprobación histórica tiene evidencia suficiente, clasificar después puede
habilitarla sin volver a ejecutar Intake. Si la evidencia no es suficiente, se
solicita nueva validación y no se reconstruyen valores que nunca se registraron.
Un dataset generado desde fuentes aprobadas queda **Pendiente de validación de
calidad**. Sólo la salida de un Intake nuevo estricto sobre ese dataset sirve
como nueva fuente de Reportes.

## Restricciones, permisos y procedencia

Crear un bloqueo exige blocks:manage, motivo y scope explícito. REPORT limita
Reportes; CONTENT limita lectura/uso nativos de contenido. Las restricciones se
aplican transitivamente a generaciones y descendientes Intake. La aprobación
histórica se conserva. Liberar exige revisión vigente y un motivo auditable;
crear otro reporte, cambiar dominio/etiqueta o validar de nuevo no las libera.

El acceso usa permisos vigentes y organización de todas las fuentes. Ser dueño
de una definición, responsable o creador del dataset no concede acceso extra.
Preview, artifacts/partes, resultados, exports y uso como entrada comprueban
procedencia. La metadata de un activo bloqueado puede seguir siendo visible
para explicar su estado. El backend repite los controles de botones/acciones.

## Contrato HTTP de gobierno y Catálogo

Base `/api/v1`, cookie de sesión y X-CSRF-Token en mutaciones. Toda colección
devuelve items/total y, cuando admite paginación, offset/limit. Limit es 1..200;
offset es no negativo. Fechas son ISO UTC. IDs no se deducen de etiquetas.

| Método y ruta | Entrada y resultado principal | Permiso de ruta |
| --- | --- | --- |
| GET `/governance/macrodomains` | active/search/offset/limit; entidades id/name/description/active/version | datasets:read |
| POST `/governance/macrodomains` | name, description opcional; 201 entidad | domains:manage |
| PATCH `/governance/macrodomains/{id}` | expected_version, name/description/active opcionales | domains:manage |
| GET `/governance/domains` | macro_domain_id/active/search/offset/limit; añade macro_domain_id | datasets:read |
| POST `/governance/domains` | name, macro_domain_id, description opcional; 201 | domains:manage |
| PATCH `/governance/domains/{id}` | expected_version, name/description/active opcionales | domains:manage |
| PATCH `/datasets/{id}/governance` | expected_version y campos de ficha opcionales; devuelve dataset/governance | governance:write |
| GET `/governance/glossary` | active/search/offset/limit; términos id/name/definition/active/version | glossary:read |
| POST `/governance/glossary` | name, definition; 201 término | glossary:manage |
| PATCH `/governance/glossary/{id}` | expected_version, name/definition/active opcionales | glossary:manage |
| GET `/catalog/tree` | parent_type y parent_id opcionales, offset/limit; nodos id/label/type/count/has_children | catalog:read |
| GET `/catalog/resources` | resource_type/search/status/domain_id/macro_domain_id/responsible/pending/offset/limit | catalog:read y permiso del módulo |
| GET `/catalog/datasets/{id}` | section=summary/columns/versions/quality/lineage/history; version_id/offset/limit | catalog:read; quality agrega intake:read |
| PATCH `/catalog/datasets/{id}/columns` | version_id,column_name,description,term_ids,expected_version; cero crea documentación | governance:write |
| POST `/catalog/datasets/{id}/terms` | term_id y version_id/column_name opcionales; idempotente | glossary:manage |
| DELETE `/catalog/glossary-associations/{id}` | Elimina asociación específica y audita | glossary:manage |
| GET `/catalog/datasets/{id}/blocks` | Bloqueos propios vigentes/liberados | catalog:read |
| POST `/catalog/datasets/{id}/blocks` | scope REPORT/CONTENT, reason; 201 | blocks:manage |
| PATCH `/catalog/blocks/{id}` | expected_version, active=false, reason | blocks:manage |
| PATCH `/catalog/security-dependencies/{id}` | expected_version, active=false, reason; liberación expresa | blocks:manage |

`/catalog/macrodomains` y `/catalog/domains` son aliases de las listas/altas/
ediciones de entidades compartidas. `/catalog/datasets` es alias de resources
con DATASET predeterminado. `parent_type` admite macro_domain/macrodomain,
domain y pending; resource_type es una hoja DATASET/INTAKE/RECON/SENTINEL/DELIVERY/REPORT.

El panel devuelve `{dataset,governance,items,total,offset,limit,section}` y datos
específicos: summary incluye eligibility/blocks; columns incluye version_id y
dataset_terms; lineage incluye security_dependencies y href/name comprobados.
`eligibility={eligible,classification_complete,strict_approval,availability,reasons,governance}`;
`strict_approval.approved` expresa calidad histórica y `availability.verified_bytes`
permanece false en metadata. Los códigos más frecuentes son DOMAIN_INCOMPATIBLE,
DOMAIN_DUPLICATE, VERSION_CONFLICT, GOVERNANCE_INHERITED, ACCOUNTING_INSUFFICIENT,
NO_EFFECTIVE_VALIDATION, DATASET_BLOCKED y APPROVAL_INTEGRITY_FAILED.

Dataset DTO añade macro_domain_id/domain_id/governance_version/governance; el
snapshot muestra IDs/nombres/actividad de clasificación, source_dataset_id,
inherited, classification_complete, personas con actividad y legacy_area/legacy_owner.
POST Dataset y registros SQL aceptan los IDs opcionales. El campo domain textual
se conserva deprecated para clientes anteriores, sin convertirlo en clasificación.

La migración/entidades y frontera física del verificador se describen en
[ADR0026](../adr/0026-controlled-governance-strict-approval.md). Reportes y los
perfiles de ejecución se documentan por separado para distinguir persistencia
deliberada de los resultados efímeros.
