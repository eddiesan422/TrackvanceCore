# Alcance funcional local 0.6.1

Se conserva el monolito FastAPI/React, PostgreSQL interno, artifacts locales,
workers DEFAULT/DELIVERY, Conexiones PostgreSQL/SQL Server y DatasetVersions
inmutables. Esta versión evoluciona el producto existente sobre 0.6.0.

| Área | Implementado | Límites / preparado |
| --- | --- | --- |
| Datasets y fuentes | CSV/XLSX/JSON/Parquet/TXT, esquema, perfiles, versiones, fuentes PostgreSQL/SQL Server | Sin nuevos Source; carga inicial síncrona acotada |
| Intake/ReconOps/Sentinel | Reglas, transforms, referencias, conciliación, programación Sentinel e historia compatible | Sin notificaciones externas de ejecuciones |
| Excepciones | Asignación, SLA, adjuntos, validación, reapertura y política automática opcional | Sin escalamiento corporativo |
| RBAC | Roles persistentes, catálogo, dependencias, Administrator protegido, propagación inmediata backend | Sin permisos arbitrarios; gestión de usuarios/roles no delegable |
| Usuarios | Username/email, temporal visible una vez, primer acceso, regeneración, baja lógica y sesiones revocables | Nombres/apellidos heredados no inventados; identidad reservada tras baja |
| SSO | Microsoft common personal/corporativo y Google Gmail/Workspace; identidad estable | Deshabilitado por defecto; sin auto-provisioning ni group-role/domain; proveedores externos no probados en este ciclo |
| Notificaciones | Lectura y persistencia histórica 0.6.0 | Sin SMTP operativo, sin pestaña ni env estándar; notificaciones funcionales y canal futuros |
| Delivery | CREATE/APPEND/OVERWRITE/UPSERT, UNKNOWN, evidencia/revisión, audit policy física, fechaIngesta/usuario | Sin SHIST/SCD, scheduler, replay automático ni nuevos Sink |
| Operación | Sin migración nueva; 0001..0012 intactas, backup state5 y proyecciones legacy, Docker manual | .env fuera de backup; sin cloud, Kubernetes, Helm o Terraform |

Los límites por defecto siguen en 10 MiB, 100.000 filas y 100 columnas. Los
benchmarks smoke no certifican volúmenes mayores. PySpark, Redis/Celery,
object storage, gestores de secretos cloud, masking, retención avanzada,
gobierno ampliado y observabilidad empresarial continúan en backlog.

Los contratos, Runs y evidencia histórica permanecen inmutables. Audit columns
representa última ingesta Trackvance; no transforma el dataset de origen ni
reconstruye la historia del target. Una confirmación UNKNOWN no autoriza replay.

La cobertura ejecutada y todos los NOT_RUN/FAIL/SKIP se documentan en
[validación](validation.md), separada de esta descripción funcional. Los capítulos
de [identidad](identity-060.md) y [Delivery audit](delivery-audit.md) y los ADRs
0016..0019 detallan modelo, API, UI, seguridad y decisiones.
