# Alcance funcional local 0.7.0

La baseline comprobada es 0.6.1, commit `6fac26b3648cb4a4b50c094ef12c1e103bc97ddd`.
La 0.7.0 conserva el monolito FastAPI/React, PostgreSQL, identidad, contratos,
linaje, evidencia histórica y los adaptadores SQL existentes. Añade adquisición
asíncrona, un segundo motor operativo y automatización persistente.

| Área | Implementado | Límites efectivos |
| --- | --- | --- |
| Adquisición | Recepción acotada, inspección, registro202, progreso, cancelación, recuperación por lease y publicación atómica | Defaults: recepción 1 GiB, población 5 millones, bytes observados 2 GiB, 100 columnas; celda 64 KiB y lote 5000 filas/8 MiB. XLSX/JSON no lineal 10 MiB/100.000 filas |
| Fuentes SQL | Snapshots completos PostgreSQL/SQL Server por cursores y batches; schema, perfil global y hash | Posición `SNAPSHOT_ROW`; sin escritura de negocio desde DatasetSource ni SQL libre |
| Almacenamiento | Parquet multipart con descriptor y partes verificadas; providers y artifacts históricos compatibles | Hashes, orden, tipo, esquema y conteos se verifican; sin versión parcial |
| Intake/ReconOps/Sentinel | Polars y PySpark real, AUTO/explícito, plan persistente, reglas y resultados exactos | Spark 4.0.3/Java17; local[K] y Standalone. Lote 2048 filas/8 MiB; fila 64 KiB; grupo Recon 10.000 filas/16 MiB. Exceso rechaza el trabajo completo |
| Excepciones | Asignación, SLA, adjuntos, validación, reapertura y política automática opcional | Sin escalamiento corporativo |
| RBAC e identidad | Roles persistentes, 42 permisos, Administrator protegido, usuarios locales y SSO Microsoft/Google | SSO externo requiere configuración; sin auto-provisioning ni sincronización de grupos. SMTP permanece retirado |
| Delivery | CREATE/APPEND/OVERWRITE/UPSERT, mensajes coherentes, preflight persistente, spool sellado, transacción única, auditoría e incertidumbre explícita | Sin SHIST/SCD, 2PC, SQL libre ni replay UNKNOWN. Lotes 1000 filas/8 MiB y población 5 millones. Más de 100.000 filas exige preflight asíncrono |
| Automatización | ONCE/INTERVAL/DAILY/WEEKLY, zona IANA, revisiones, ocurrencias, identidad responsable y encadenamiento exacto Intake→Delivery | Coalescing, exclusión de targets y dedupe persistentes. UNKNOWN bloquea hasta decisión auditada; sin reejecución implícita |
| Notificaciones | Outbox PostgreSQL y consumidores independientes; bandeja personal, filtros y lectura persistente | Se aplican permisos actuales a lista, contador y detalle; sin correo ni bandeja global de administradores |
| Operación | Nueve servicios Docker, tres lanes, scheduler, dos consumidores; migraciones 0013..0015 y 42 tablas; backup state6 | 0001..0012 permanecen intactas. `.env` se conserva por separado; sin cloud/Kubernetes/Helm/Terraform |

Cambiar parámetros exige reiniciar o recrear los procesos afectados. Cada trabajo
registra sus límites y plan; aumentar la cota de recepción no aumenta por sí solo
la memoria ni habilita volúmenes superiores a los certificados. La muestra de
presentación del perfil se limita a veinte filas y 8 MiB; el perfil almacenado
siempre describe toda la población publicada.

Los resultados actuales, recursos medidos y requisitos aún pendientes se registran
en [validación](validation.md). El antecedente 0.6.1 conserva su informe separado
en [validación histórica](validation-0.6.1.md). Los smoke anteriores no certifican
la capacidad de 0.7.0. Consulta también [parámetros](parameters-0.7.0.md),
[Spark](spark-volume-0.7.0.md), [operación](operations.md) y los ADR0020..0024.
