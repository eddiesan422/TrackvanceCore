# Alcance funcional local 0.5.0

La línea oficial está en [roadmap](../roadmap.md). Los puntos 1 y 2 se conservan:
Conexiones PostgreSQL/SQL Server, SecretStore y snapshots inmutables son la base de
la evolución. Los puntos 3–10 maduran el producto local; la productización del
punto 11 no forma parte de este ciclo. Este documento describe capacidades;
[validación](validation.md) registra qué comprobaciones se ejecutaron y su resultado.

| Área | IMPLEMENTADO en código local | PREPARADO / OBJETIVO fuera de este ciclo |
| --- | --- | --- |
| Datasets | CSV/XLSX/JSON/Parquet/TXT, esquema acotado, overrides, identificadores, áreas, filtros, versiones y lineage | Adquisición/profiling asíncronos de gran volumen |
| Conexiones | PostgreSQL/SQL Server de lectura, schemas/tablas/vistas, preview, configuración versionada, secretos cifrados y refresh de snapshots | S3, Blob, APIs, Oracle, MySQL, Snowflake y Databricks |
| Intake | Required/not_null, unique simple/compuesta, numeric/positive/type, range/length, allowed_values, regex/date, comparación entre columnas, condiciones y referencias a DatasetVersion | Scripting arbitrario excluido; condiciones compuestas disponibles por API, el editor visual crea condiciones simples |
| ReconOps | 1:1, claves compuestas, múltiples comparaciones exactas/numéricas/temporales, nulls, transforms y 1:N/N:1 SUM/COUNT | N:M y fuzzy matching no implementados |
| Sentinel | Programación por intervalo en worker, métricas compatibles, tendencias, schema drift, nulls/volumen/frescura/distinct/uniqueness y median/IQR | Entrega externa de alertas mediante NotificationDelivery; sin Email/Teams/Slack/Webhook |
| Data Delivery | Destinos versionados PostgreSQL/SQL Server, mapping tipado, preview, preflight, configuraciones inmutables, lane/worker separado, transacción remota, intentos, `UNKNOWN`, receipt, manifest y linaje | Sin S3/Blob/REST; sin transformaciones de negocio; sin scheduler; sin reintento ciego de confirmación incierta; RBAC granular pendiente |
| Excepciones | Asignación, prioridad, SLA, objetivo, comentarios, adjuntos, filtros, reapertura, cierre administrativo y resolución técnica humana/automática opcional | Agrupación multi-finding y políticas corporativas de escalamiento |
| Identidad | Cinco roles base, usuarios locales, actividad, permisos efectivos, reset Argon2, revocación de sesiones, protección del último administrador y auditoría | OIDC/SSO y directorio multi-organización |
| Centro de Control | Salud real, filtros globales, fallos, datasets afectados, tendencias y navegación a acción/detalle | Vistas guardadas y acuerdos operativos corporativos |
| Evidencia | Manifest schema2 compatible con v1, actores estables, métricas por regla, referencias, linaje y Excel estructurado | Retención y almacenamiento remoto productivo |
| Ejecución | Polars/Python, compiladores de reglas Polars/DuckDB, preflight, jobs SQL, leases, heartbeat, idempotencia y recuperación | PySpark operativo, Redis/Celery productivo y distribución horizontal |
| Operación y volumen | Reset por plan confirmado, backup Docker/SQLite, restore integral, diagnóstico/integridad y benchmark aislado medido | Alta disponibilidad, certificación de tamaños no probados y garantías productivas |

Una versión o ejecución histórica no cambia por corregir una fuente, publicar
una configuración, resolver una excepción o cambiar un usuario. SUCCESS es
estado técnico; REJECTED, WITH_FINDINGS y ALERT son resultados de negocio.

El scheduler evalúa la última DatasetVersion ya registrada; no adquiere datos
externos ni opera con Docker apagado. Las resoluciones automáticas están apagadas
por defecto y necesitan la misma comprobación técnica posterior que el cierre
humano. Una regla no evaluada no certifica una corrección.

La carga conserva límites por defecto de 10 MiB, 100.000 filas y 100 columnas;
los overrides exclusivos del benchmark no cambian esos valores. El máximo
realmente probado se documenta con bytes/filas/recursos y no se extrapola a otras
formas de datos o capacidades del equipo.
El baseline variado de 100 MiB nominales (106.194.531 bytes, 50.000 filas, cuatro
columnas) pasó con overrides aislados; 500 MiB y 1/2/5 GiB permanecen sin ejecutar
por presupuesto. La restauración aislada verificó metadata, artifacts, secretos,
lineage y una conexión PostgreSQL restaurada. Consulta los detalles y límites
en [operación](operations.md) y [volumen](volume-benchmark.md).

StorageProvider, DatasetSource, DataSink, ExecutionEngine y JobQueue son los
puertos vigentes. Data Delivery/DataSink es una responsabilidad de salida separada
de DatasetSource y de los exports de usuario. Publica únicamente una
DatasetVersion registrada hacia PostgreSQL o SQL Server con configuración y
revisión de destino inmutables; no incorpora transforms funcionales. Los permisos
actuales reutilizan temporalmente capacidades existentes y el RBAC granular de
Delivery permanece pendiente. No se incorporan Kubernetes, Terraform, Helm,
OIDC, S3/Blob internos, Vault productivo, Redis/Celery ni PySpark operativo.

La implementación 0.5.0 tiene certificación local integrada cerrada. Conteos,
duraciones, límites y el estado remoto del commit de publicación se registran en
[validación](validation.md) sin heredar resultados de versiones anteriores.

El primer corte 0.1 y la certificación 0.3 se conservan en Git y documentos
históricos; sus pendientes resueltos no se presentan como pendientes de 0.5.
