# Roadmap oficial del prototipo local

La evolución funcional local precede a la productización. Este orden sustituye la secuencia anterior que adelantaba almacenamiento remoto, Redis/Celery o Kubernetes.

| Punto | Alcance | Situación al abrir el ciclo 0.4.0 |
| --- | --- | --- |
| 1 | Gestión de conexiones y fuentes externas | Implementado; conservar y recertificar |
| 2 | PostgreSQL y SQL Server como DatasetSource | Implementado con motores reales; conservar y recertificar |
| 3 | Data Intake configurable y reglas avanzadas | Ampliación funcional y certificación en curso |
| 4 | ReconOps configurable, transformaciones y agregaciones | Ampliación funcional y certificación en curso |
| 5 | Sentinel programado, histórico y alertas internas | Ampliación funcional y certificación en curso |
| 6 | Excepciones con validación, SLA, adjuntos y política automática | Ampliación funcional y certificación en curso |
| 7 | Administración local de usuarios, roles y permisos | Ampliación funcional y certificación en curso |
| 8 | Reset, backup/restore integral y diagnóstico | Implementación y certificación aislada en curso |
| 9 | Benchmarks reproducibles y límites medidos | Framework y medición real en curso; no asumir capacidad por volumen |
| 10 | Productización | Fuera del ciclo; objetivo futuro |

El cierre de cada punto exige pruebas satisfactorias, documentadas en `development/validation.md`. Las capacidades presentes en código aún pendientes de certificación no se anuncian como certificadas. Los informes de ciclos anteriores se conservan como evidencia histórica.

## Principios

Monolito modular; PostgreSQL interno para metadata; StorageProvider para artifacts; DatasetSource para entradas; ExecutionEngine y JobQueue como fronteras de procesamiento. Mantener CSV, XLSX, JSON, Parquet y TXT junto con PostgreSQL/SQL Server. Configuraciones, DatasetVersions, snapshots y runs permanecen inmutables. Las migraciones Alembic son aditivas y no reescriben migraciones aplicadas.

RBAC backend, organización, CSRF, SQL parametrizado, límites de carga, secretos cifrados fuera de logs/respuestas, integridad SHA-256, protección de rutas e inyección de fórmulas, auditoría estable y concurrencia optimista forman parte del funcionamiento. Un error técnico no equivale a un hallazgo de negocio; una resolución técnica exige evidencia posterior válida.

Los entornos Docker destructivos de prueba deben usar proyectos, bases y volúmenes aislados. Nunca borrar datos reales para certificar un cambio. Cada benchmark informa el volumen realmente ejecutado, recursos, resultados y motivo de detención. Un volumen no ejecutado por recursos o límites no es un resultado medido.

## Preparado y objetivo futuro

Los puertos permiten añadir fuentes y adaptadores sin acoplar los módulos funcionales a proveedores. Data Delivery/DataSink será una responsabilidad independiente para salidas; no se implementa aquí.

Kubernetes, Redis/Celery productivo, S3/Azure Blob como almacenamiento interno, OIDC/SSO, Vault/Key Vault productivo, Terraform, Helm, PySpark operativo y observabilidad distribuida permanecen como objetivos de producto. No deben presentarse como implementados ni añadirse para completar este roadmap funcional local.
