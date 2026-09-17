# Alcance histórico del prototipo 0.1

Este documento conserva el primer corte como antecedente. El estado actual 0.3.0 y sus pendientes están en [arquitectura](../architecture.md), [validación](validation.md), [catálogo](../rules-catalog.md) y [operación](operations.md). Las ampliaciones de esta tabla no representan pendientes vigentes cuando ya fueron resueltas en 0.3.0.

Referencia: Trackvance_Core_Especificacion_Tecnica_v1.1.pdf, septiembre 2026.
Este documento es un control de alcance; no sustituye la especificación.

| Área | Primer corte local | Ampliación posterior |
| --- | --- | --- |
| Centro de control | Métricas persistidas; en 0.2 incorpora filtros por período, dataset, módulo, estado y criticidad, atención priorizada, evolución de salud, datasets afectados y navegación al detalle | Períodos personalizados, vistas guardadas, asignación operativa y objetivos SLA |
| Identidad | Sesión demo explícita y protección CSRF | Administración completa de usuarios, permisos, bloqueos y sesiones |
| Datasets | Carga CSV, XLSX, JSON, Parquet y TXT mediante lectores desacoplados; versiones, artifacts, hash, perfil, muestra, corrección explícita del tipo lógico, identificadores asistidos, áreas configurables, origen visible y selección de hoja/delimitador | Conectores PostgreSQL, SQL Server, object storage y APIs; profiling asíncrono de gran volumen |
| Intake | Required, unique, numeric, positivos, decisión y errores | Mapping, normalizaciones, referencias, fechas, reglas críticas y versionamiento completo |
| ReconOps | Claves, duplicados, faltantes, comparación decimal y evidencia | 1:N, tolerancia porcentual/temporal y políticas configurables de nulls |
| Sentinel | Monitores y checks explicables con historial | Baseline robusto IQR, programación y contratos de métricas |
| Excepciones | Creación desde hallazgo, configuración y run de origen inmutables, validación mediante run posterior del mismo control, resolución técnica, cierres administrativos y timeline | Agrupación de varios hallazgos, equipos, asignación por permisos y resolución automática |
| Evidencia | Manifest JSON y export CSV de resultados | Bundles, retención y verificación ampliada de integridad |
| Trabajos | API y worker en procesos separados, estado persistido | Leases, recuperación, cancelación endurecida y concurrencia multiworker |
| Motores | Polars en el worker estándar | DuckDB, planner por presupuesto y perfil opcional PySpark |
| Operación | Arranque Windows, Compose y datos persistentes | Backup/restore, upgrades y runbooks completos |

No se debe publicar este prototipo en Internet con el acceso demo habilitado.
La siguiente revisión debe priorizar el comportamiento del flujo integrado y
las reglas del negocio antes de ampliar los motores de procesamiento.
