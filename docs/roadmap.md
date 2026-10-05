# Roadmap oficial del prototipo local

La evolución funcional local precede a la productización. Este orden sustituye la secuencia anterior que adelantaba almacenamiento remoto, Redis/Celery o Kubernetes.

| Punto | Alcance | Estado y evidencia por ciclo |
| --- | --- | --- |
| 1 | Gestión de conexiones y fuentes externas | Implementado y recertificado |
| 2 | PostgreSQL y SQL Server como DatasetSource | Implementado y recertificado con ambos motores reales |
| 3 | Data Intake configurable y reglas avanzadas | Implementado; unitarias, integración y E2E aprobadas |
| 4 | ReconOps configurable, transformaciones y agregaciones | Implementado; unitarias, integración y E2E aprobadas |
| 5 | Sentinel programado, histórico y alertas internas | Implementado; unitarias, integración y E2E aprobadas |
| 6 | Excepciones con validación, SLA, adjuntos y política automática | Implementado; unitarias, integración y E2E aprobadas |
| 7 | Administración local de usuarios, roles y permisos | Implementado; unitarias, integración y E2E aprobadas |
| 8 | Reset, backup/restore integral y diagnóstico | Implementado; reset protegido y backup/restore destructivo aislado aprobados |
| 9 | Benchmarks reproducibles y límites medidos | Antecedentes: general 0.4.0 de 106.194.531 bytes / 50.000 filas y Delivery 0.5.1 de 8.917.809 bytes / 20.000 filas. En 0.6.0 se ejecutan smoke acotados; no recertifican esos volúmenes mayores ni elevan límites. |
| 10 | Data Delivery controlado a PostgreSQL/SQL Server | Hardening 0.5.1 conservado; 0.6.0 añade auditoría permanente, snapshot de username, drift y permisos propios. SQL real y navegador local aprobados; el HEAD final requiere su propio CI. |
| 11 | Catálogo de gobierno, clasificación, glosario y restricciones | Implementación 0.8.0; certificación propia en validation.md |
| 12 | Reportes guiados/SQL y datasets derivados | Implementación 0.8.0; perfiles separados, autorización transitiva y certificación propia |
| 13 | Productización | Fuera del ciclo; objetivo futuro |

El cierre de cada punto exige pruebas satisfactorias, documentadas en `development/validation.md`. Las capacidades presentes en código aún pendientes de certificación no se anuncian como certificadas. Los informes de ciclos anteriores se conservan como evidencia histórica.

## Ciclo 0.7.0: volumen y automatización

Implementa adquisición asíncrona, StorageProvider multipart, PySpark local y
Standalone opcional, preflight persistido y preparación Delivery por lotes,
automatización/scheduling/Intake→Delivery, outbox y bandeja personal. La evidencia
propia comprende1M y progresión100/500/1024MiB, SQL real, recuperación y CI final.
Los gates pendientes/fallidos mantienen su estado; no se heredan cifrasanteriores.
No añade Redis/Celery, Kubernetes, nuevos Source/Sink, gobierno, masking, canales
externos ni notificaciones de asignación. El upgrade real es último gate y sólo
ocurre después de pruebas/documentos/CI aprobados.

### Correcciones C01–C06 — 4 de octubre de 2026

Este ciclo conserva el alcance funcional de 0.7.0. Corrige la adquisición normal
de XLSX mediante lectura incremental y límites propios, diagnósticos seguros y
límites visibles, el selector compartido de áreas, la edición de zona horaria,
el despacho por metadata y el setter personal de notificaciones no leídas.
No abre puntos adicionales del roadmap. La migración aditiva 0016 sólo incorpora
los dos campos opcionales del diagnóstico de adquisición, sin reescribir historia.

La implementación y la certificación se distinguen en
[validación C01–C06](development/validation.md). El cierre exige XLSX inline y
shared de 400000/1000000 filas con verificación completa, navegador y Spark/SQL
reales, PostgreSQL con cuatro programaciones sobre dos datasets grandes y un
worker ocupado, recuperación, todas las regresiones, PDF completo revisado,
todos los jobs del SHA final y upgrade controlado de la instalación existente.
Hasta completar esos gates, las capacidades implementadas no constituyen una
certificación cerrada.

## Antecedente 0.6.1: simplificar la entrega de credenciales

El administrador recibe una temporal nueva una sola vez en pantalla y decide cómo
comunicarla externamente. Se retira SMTP del onboarding, de Configuración y de las
variables estándar. Se conservan email como identidad, primer acceso obligatorio,
24 horas de vencimiento, RBAC y SSO opcional deshabilitado por defecto.
No cambia el schema ni la funcionalidad de Data Delivery. La validación se repite
contra mocks OIDC y SQL real; las pruebas externas no forman parte de este ciclo.

Las notificaciones funcionales regresan al backlog: RUN_COMPLETED, RUN_FAILED,
DELIVERY_FAILED, DELIVERY_UNKNOWN, SENTINEL_ALERT y EXCEPTION_ASSIGNED son posibles
eventos futuros. No se decide todavía SMTP, Teams, Slack o Webhook como tecnología;
el canal dependerá del contexto del cliente. La metadata 0.6.0 queda histórica.

## Antecedente 0.6.0: identidad dinámica y auditoría técnica

RBAC administrable, login username/email, credenciales temporales por SMTP,
primer acceso, Microsoft personal/corporativo y Google Gmail/Workspace mediante
OIDC, y auditoría fechaIngesta/usuario en Delivery son alcance de esta versión.
La evidencia de aquel ciclo se conserva en
[validation-0.6.0.md](development/validation-0.6.0.md); no se heredan resultados.
SHIST/SCD, vigencias y tablas históricas paralelas quedan excluidos. DatasetVersion
sigue siendo el versionado inmutable interno. SSO no auto-provisiona ni mapea
grupos/roles externos. En aquel ciclo SMTP sólo entregaba credenciales USER; 0.6.1 revierte esa decisión.

Gobierno ampliado, dominios/grupos administrables, notificaciones de ejecución,
scheduling Delivery, nuevos Source/Sink, masking, retención avanzada, secretos
cloud, Redis/Celery, PySpark, Kubernetes/Helm/Terraform y observabilidad
empresarial permanecen en backlog. No son requisitos para cerrar 0.6.0.

## Antecedente 0.5.1: fortalecer el punto 10 existente

El ciclo no abre un nuevo módulo ni adelanta productización. Cierra ocho áreas:
reparación local de evidencia, revisión operacional de UNKNOWN, semántica de
métricas, conteos UPSERT PostgreSQL fiables, regresiones temporales, nomenclatura
canónica de linaje, benchmark Delivery propio y carga diferida de rutas frontend.
La persistencia nueva se limita a `delivery_reviews` en `0009_delivery_reviews`:
la revisión externa es consultable y no sobrescribe la confirmación histórica.

La aceptación exige demostrar que reparar no repite la escritura, que revisar
UNKNOWN no crea replay y que `null` no se convierte en cero. También exige
recuperación de nuevas revisiones, compatibilidad con 0.4.x/0.5.0, pruebas con
ambos motores y documentación ampliada del sistema completo. Las cifras actuales
están en la evidencia 0.5.1; no se heredan las de ciclos anteriores. El benchmark
Delivery no se mezcla con el benchmark histórico de módulos de calidad.

El frontend tiene medición antes/después y pruebas propias; la reducción del
bundle inicial no certifica por sí sola latencia de usuario ni volumen de datos.
La especificación conserva el nombre técnico v1.1 y distingue IMPLEMENTADO,
PREPARADO y OBJETIVO. No se cierra 0.5.1 hasta terminar la validación integrada y CI.

## Principios

Monolito modular; PostgreSQL interno para metadata; StorageProvider para artifacts;
DatasetSource para entradas; DataSink para salidas; ExecutionEngine y JobQueue como
fronteras de procesamiento. Mantener CSV, XLSX, JSON, Parquet y TXT junto con
PostgreSQL/SQL Server. Configuraciones, DatasetVersions, snapshots y runs
permanecen inmutables. Las migraciones Alembic son aditivas y no reescriben
migraciones aplicadas.

RBAC backend, organización, CSRF, SQL parametrizado, límites de carga, secretos cifrados fuera de logs/respuestas, integridad SHA-256, protección de rutas e inyección de fórmulas, auditoría estable y concurrencia optimista forman parte del funcionamiento. Un error técnico no equivale a un hallazgo de negocio; una resolución técnica exige evidencia posterior válida.

Los entornos Docker destructivos de prueba deben usar proyectos, bases y volúmenes aislados. Nunca borrar datos reales para certificar un cambio. Cada benchmark informa el volumen realmente ejecutado, recursos, resultados y motivo de detención. Un volumen no ejecutado por recursos o límites no es un resultado medido.

## Preparado y objetivo futuro

Los puertos permiten añadir fuentes y adaptadores sin acoplar los módulos
funcionales a proveedores. Data Delivery implementa la primera frontera de salida
mediante `DataSink`, exclusivamente para PostgreSQL y SQL Server en el prototipo
local. S3/Blob/REST y otros destinos, secretos administrados, Redis/Celery, object storage,
observabilidad distribuida y despliegue Kubernetes permanecen como evolución de
producto. La certificación de cada revisión se publica separada de este estado funcional.

Kubernetes, Redis/Celery productivo, S3/Azure Blob como almacenamiento interno, Vault/Key Vault productivo, Terraform, Helm y observabilidad distribuida permanecen como objetivos de producto. PySpark operativo y automatización/bandeja interna se implementan en0.7.0; su certificación exige evidencia propia. No deben presentarse como implementados ni añadirse para completar este roadmap funcional local.
