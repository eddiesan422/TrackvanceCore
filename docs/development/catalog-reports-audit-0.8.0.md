# Revisión inicial de Catálogo y Reportes 0.8.0

Fecha: 5 de octubre de 2026. Base local y remota comprobada:
`d9b6856e757a2a1fcab3913209146f3b7b79d70c`, rama `feat/local-prototype`, árbol
inicial limpio. Este documento distingue estado observado de decisiones y de
certificación. No declara finalizada 0.8.0.

## Base observada y reutilización

| Componente | Estado en la base | Reutilización y cambio necesario |
| --- | --- | --- |
| Adquisición | AcquisitionRun/Upload y Job ACQUISITION; lector XLSX ZIP/SAX con índices limitados, diagnósticos y publicación íntegra | Conservar límites, numeración y fence; sustituir área editable por clasificación opcional controlada en las tres rutas |
| Metadata | Dataset/Version inmutables, Configuration con cadena de revisiones, Run ligado a un dataset/configuración | Agregar identidades de gobierno; crear ejecución multifuente propia sin Run ni dataset ficticio |
| Archivos | FileArtifactStore/StorageProvider, multipart comprimido, descriptor/partes/hash, enlaces reales | Reutilizar para DATASET; PREVIEW/DOWNLOAD tienen un canal independiente sin persistencia de cuerpos ni artifacts |
| Calidad | Polars/PySpark, decisiones Intake, evidencia y output_version_id | Agregar contabilidad explícita de cobertura y verificador estricto común; evidencia histórica incompleta no se inventa |
| Organización | Dataset.domain/owner/criticality heredados; selector de áreas C03 | Conservar textos históricos y datos/versiones; clasificación por FK, sin asociación automática de áreas |
| Salidas Intake | Fuente y salida vinculadas por versiones/runs; búsqueda histórica por nombre | Identidad comprobada entrada/contrato, herencia de gobierno por relaciones y procedencia de seguridad |
| Identidad | RBAC dinámico, catálogo de permisos, CSRF, organización, auditoría | Ampliar catálogo/endpoints; Administrator conserva catálogo completo, roles personalizados no reciben grants automáticos |
| Automatización | Scheduler y CHAINING por metadata, leases; outbox y bandeja personal | Conservar C05/C06 y lanes anteriores; nueva lane REPORT utiliza Job y heartbeat existentes |
| SQL | DuckDB reutilizado por lectores; algunos consumidores anteriores permiten spill | Ejecutor independiente tipado con AST permitido y restricciones del sistema operativo; sin spill en perfiles efímeros |
| Delivery | Preflight/PreparedRows, transacción externa, UNKNOWN y reparación | Conservar contratos y workers; Catálogo muestra únicamente confirmaciones COMMITTED verificables |
| Recuperación | Manifest 2/state 7/0016, seis volúmenes, proyecciones state 2–6 | State 8 y proyección exacta state 7; incluir las nuevas entidades y linaje, excluir staging de Reportes |
| Interfaz | React/TypeScript con rutas lazy, estilos/componentes compartidos y permisos | Agregar Catálogo y Reportes integrados; fuentes, cruces, columnas/filtros y contexto congelado |
| Documento | Fuente v1.1, generador reportlab/pypdf, PDF oficial y copia ProductOne/Documentación | Actualizar todas las secciones afectadas, renderizar y revisar completo; conservar antecedentes como históricos |

## Instalación habitual identificada

Docker Desktop estaba apagado y se inició únicamente el motor. Se comprobaron
los contenedores existentes; no se inició, migró, sembró ni restauró la
instalación habitual durante esta revisión.

- Proyecto real: `trackvance-certification`; web configurada en loopback, puerto 3100.
- Nueve contenedores existentes detenidos, todos con `restart: no`.
- Imágenes de aplicación etiquetadas con el SHA de base d9b6856; IDs guardados
  en inventario privado. La etiqueta no prueba por sí sola la migración aplicada.
- Seis volúmenes propios: PostgreSQL, datos canónicos, credenciales/clave de
  conexiones, credenciales/clave de destinos. No se adoptó este proyecto por
  el nombre mencionado en antecedentes; se comprobó en labels y Compose real.
- Compose efectivo anterior incluye el archivo base y overlays privados del
  upgrade C01–C06. Deben conservarse puerto/origen/configuración/secretos y
  políticas durante la promoción final.
- Host con 32 GiB de RAM física; motor Linux con 16 CPU y unos 15 GiB asignados.
  La certificación nueva impone CPU/RAM por servicio y presupuesto conjunto
  de 9 GiB; se ejecutan escenarios de volumen secuencialmente.

El CI de base, workflow
[37233312966](https://github.com/eddiesan422/TrackvanceCore/actions/runs/37233312966),
terminó con 15 jobs SUCCESS. Es evidencia de 0.7.0, no certificación de 0.8.0.
La revisión aplicada, la igualdad de metadata/archivos y el estado operativo
habitual se comprobarán en copia protegida y otra vez antes/después de promover.

La inspección posterior sobre copia fría completa confirmó aplicación **0.7.0**
y Alembic **0016_acquisition_diagnostics**, sin trabajos QUEUED/RUNNING. La copia
incluyó WAL y pasó comparación binaria antes de recuperar PostgreSQL sólo en el
clon. Los helpers montaron la fuente en solo lectura; el clon no tuvo red,
puertos ni workers. El inventario habitual permaneció idéntico. Esta inspección
no sustituye el respaldo fresco y su restore previo a la actualización final.

## Dependencias, riesgos y decisiones

Se conserva el monolito modular FastAPI/React/PostgreSQL. El parser nuevo es
SQLGlot con versión congelada; DuckDB y los adaptadores existentes se mantienen.
La migración `0017_catalog_reports` agrega entidades y columnas sin reescribir
0001–0016, clasificar automáticamente, alterar archivos ni crear versiones de datos.

El kernel Docker probado ofrece Landlock ABI 3. Crear un user namespace fue
rechazado con EPERM usando seccomp estándar; se descartó depender de bubblewrap
con capacidades elevadas. El ejecutor adopta Landlock y libseccomp, entorno
mínimo y límites de proceso/cgroup; falla cerrado si la frontera no está disponible.
Esta decisión requiere pruebas reales de acceso/red y de escrituras durante cada
perfil, además de validar AST. Un proceso Python con acceso general al volumen
no cumple el requisito.

Los riesgos principales son la compatibilidad de backup/proyección, la evidencia
heredada insuficiente, revocaciones durante streaming/publicación, memoria y
expansión de joins, y visibilidad atómica entre archivos y PostgreSQL. Se resuelven
con relaciones verificables, errores funcionales conservadores, selección congelada,
revalidación, staging privado sólo para DATASET y fences del Job existente.

## Condiciones de cierre

Se requiere código integrado, todos los gates locales y Docker aislados,
oráculos completos a 400.000/1.000.000 por fuente, recuperación nativa y desde
0.7.0, revisión completa del PDF y sus copias, todos los jobs obligatorios de
GitHub Actions SUCCESS sobre el SHA final y upgrade seguro de la instalación
habitual. Un PASS anterior, un plan o un workflow parcial no sustituyen estos gates.
