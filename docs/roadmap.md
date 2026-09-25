# Roadmap oficial del prototipo local

La evolución funcional local precede a la productización. Este orden sustituye la secuencia anterior que adelantaba almacenamiento remoto, Redis/Celery o Kubernetes.

| Punto | Alcance | Estado certificado vigente |
| --- | --- | --- |
| 1 | Gestión de conexiones y fuentes externas | Implementado y recertificado |
| 2 | PostgreSQL y SQL Server como DatasetSource | Implementado y recertificado con ambos motores reales |
| 3 | Data Intake configurable y reglas avanzadas | Implementado; unitarias, integración y E2E aprobadas |
| 4 | ReconOps configurable, transformaciones y agregaciones | Implementado; unitarias, integración y E2E aprobadas |
| 5 | Sentinel programado, histórico y alertas internas | Implementado; unitarias, integración y E2E aprobadas |
| 6 | Excepciones con validación, SLA, adjuntos y política automática | Implementado; unitarias, integración y E2E aprobadas |
| 7 | Administración local de usuarios, roles y permisos | Implementado; unitarias, integración y E2E aprobadas |
| 8 | Reset, backup/restore integral y diagnóstico | Implementado; reset protegido y backup/restore destructivo aislado aprobados |
| 9 | Benchmarks reproducibles y límites medidos | Framework general con antecedente 0.4.0 de 106.194.531 bytes / 50.000 filas, no recertificado a ese volumen en 0.5.1; nuevo benchmark Delivery de 8.917.809 bytes / 20.000 filas, ocho casos PASS sin elevar límites |
| 10 | Data Delivery controlado a PostgreSQL/SQL Server | Hardening operacional 0.5.1 implementado; código 8927ea0 certificado con ocho jobs SUCCESS, límites explícitos en validación |
| 11 | Productización | Fuera del ciclo; objetivo futuro |

El cierre de cada punto exige pruebas satisfactorias, documentadas en `development/validation.md`. Las capacidades presentes en código aún pendientes de certificación no se anuncian como certificadas. Los informes de ciclos anteriores se conservan como evidencia histórica.

## Ciclo 0.5.1: fortalecer el punto 10 existente

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
local. S3/Blob/REST y otros destinos, orquestación programada, RBAC específico de
Delivery, secretos administrados, Redis/Celery, object storage, OIDC/SSO,
observabilidad distribuida y despliegue Kubernetes permanecen como evolución de
producto. La certificación de cada revisión se publica separada de este estado funcional.

Kubernetes, Redis/Celery productivo, S3/Azure Blob como almacenamiento interno, OIDC/SSO, Vault/Key Vault productivo, Terraform, Helm, PySpark operativo y observabilidad distribuida permanecen como objetivos de producto. No deben presentarse como implementados ni añadirse para completar este roadmap funcional local.
