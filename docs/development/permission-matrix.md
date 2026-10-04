# Matriz de permisos HTTP y catálogo 0.7.0

Generada por `scripts/export_contracts.py` desde la autoridad del runtime. Base `/api/v1`; organización, propietario, CSRF, primer acceso, estado y autorización de estrategia se aplican además del permiso de ruta.

El catálogo contiene 42 códigos; la matriz tiene 130 entradas protegidas.

Administrator resuelve el catálogo completo, conserva protección y no puede consultar bandejas/preflights personales ajenos. Los defaults ampliados de organizaciones nuevas no reescriben roles personalizados existentes.

## Catálogo y dependencias

| Código | Grupo | Dependencias directas | Delegable |
|---|---|---|---|
| `artifacts:download` | Evidencia / exports | — | Sí |
| `audit:read` | Auditoría | — | Sí |
| `connections:manage` | Conexiones | connections:read | Sí |
| `connections:read` | Conexiones | — | Sí |
| `connections:use` | Conexiones | connections:read | Sí |
| `datasets:read` | Datasets | — | Sí |
| `datasets:write` | Datasets | datasets:read | Sí |
| `delivery:alter_target` | Data Delivery | delivery:execute, delivery:read | Sí |
| `delivery:configure` | Data Delivery | datasets:read, delivery:read, destinations:read, destinations:use | Sí |
| `delivery:execute` | Data Delivery | datasets:read, delivery:read | Sí |
| `delivery:overwrite` | Data Delivery | delivery:execute, delivery:read | Sí |
| `delivery:read` | Data Delivery | — | Sí |
| `delivery:repair_evidence` | Data Delivery | delivery:execute, delivery:read | Sí |
| `delivery:review_unknown` | Data Delivery | delivery:read | Sí |
| `delivery:schedule` | Data Delivery | delivery:configure, delivery:execute, delivery:read | Sí |
| `destinations:manage` | Destinos | destinations:read | Sí |
| `destinations:read` | Destinos | — | Sí |
| `destinations:use` | Destinos | destinations:read | Sí |
| `exceptions:close` | Excepciones | exceptions:read, exceptions:write | Sí |
| `exceptions:read` | Excepciones | — | Sí |
| `exceptions:write` | Excepciones | exceptions:read | Sí |
| `exports:download` | Evidencia / exports | — | Sí |
| `intake:configure` | Data Intake | datasets:read, intake:read | Sí |
| `intake:execute` | Data Intake | datasets:read, intake:read | Sí |
| `intake:read` | Data Intake | — | Sí |
| `notifications:manage` | Notificaciones | notifications:read | Sí |
| `notifications:read` | Notificaciones | — | Sí |
| `recon:configure` | ReconOps | datasets:read, recon:read | Sí |
| `recon:execute` | ReconOps | datasets:read, recon:read | Sí |
| `recon:read` | ReconOps | — | Sí |
| `roles:manage` | Roles | roles:read | No |
| `roles:read` | Roles | — | Sí |
| `rules:read` | Reglas | — | Sí |
| `runs:execute` | Ejecuciones | runs:read | Sí |
| `runs:read` | Ejecuciones | — | Sí |
| `sentinel:configure` | Sentinel | datasets:read, sentinel:read | Sí |
| `sentinel:execute` | Sentinel | datasets:read, sentinel:read | Sí |
| `sentinel:read` | Sentinel | — | Sí |
| `sentinel:schedule` | Sentinel | sentinel:execute, sentinel:read | Sí |
| `system:read` | Sistema | — | Sí |
| `users:manage` | Usuarios | users:read | No |
| `users:read` | Usuarios | — | Sí |

## Rutas protegidas

| Método | Ruta | Permiso |
|---|---|---|
| DELETE | `/connections/{id}` | `connections:manage` |
| DELETE | `/delivery/destinations/{id}` | `destinations:manage` |
| DELETE | `/roles/{id}` | `roles:manage` |
| DELETE | `/users/{id}` | `users:manage` |
| DELETE | `/users/{id}/external-identities/{identity_id}` | `users:manage` |
| GET | `/acquisitions` | `datasets:read` |
| GET | `/acquisitions/limits` | `datasets:read` |
| GET | `/acquisitions/{id}` | `datasets:read` |
| GET | `/artifacts/{id}/download` | `artifacts:download` |
| GET | `/audit-events` | `audit:read` |
| GET | `/connections` | `connections:read` |
| GET | `/connections/{id}` | `connections:read` |
| GET | `/connections/{id}/objects` | `connections:use` |
| GET | `/connections/{id}/preview` | `connections:use` |
| GET | `/connections/{id}/schemas` | `connections:use` |
| GET | `/dashboard` | `runs:read` |
| GET | `/dataset-versions/{id}/profile` | `datasets:read` |
| GET | `/datasets` | `datasets:read` |
| GET | `/datasets/uploads/{id}/inspect` | `datasets:write` |
| GET | `/datasets/{id}` | `datasets:read` |
| GET | `/datasets/{id}/schema` | `datasets:read` |
| GET | `/delivery/automations` | `delivery:read` |
| GET | `/delivery/automations/{id}` | `delivery:read` |
| GET | `/delivery/automations/{id}/occurrences` | `delivery:read` |
| GET | `/delivery/configurations` | `delivery:read` |
| GET | `/delivery/destinations` | `destinations:read` |
| GET | `/delivery/destinations/{id}` | `destinations:read` |
| GET | `/delivery/destinations/{id}/schemas` | `destinations:use` |
| GET | `/delivery/destinations/{id}/table-metadata` | `destinations:use` |
| GET | `/delivery/destinations/{id}/tables` | `destinations:use` |
| GET | `/delivery/destinations/{id}/target-policy` | `delivery:read` |
| GET | `/delivery/runs` | `delivery:read` |
| GET | `/delivery/runs/{id}/attempts` | `delivery:read` |
| GET | `/delivery/runs/{id}/receipt` | `artifacts:download` |
| GET | `/delivery/runs/{id}/reviews` | `delivery:read` |
| GET | `/delivery/validations` | `delivery:read` |
| GET | `/delivery/validations/{id}` | `delivery:read` |
| GET | `/exceptions` | `exceptions:read` |
| GET | `/exceptions/assignees` | `exceptions:read` |
| GET | `/exceptions/{id}` | `exceptions:read` |
| GET | `/exceptions/{id}/attachments/{attachment_id}/download` | `artifacts:download` |
| GET | `/findings` | `runs:read` |
| GET | `/intake/contracts` | `intake:read` |
| GET | `/intake/runs/{id}/errors` | `intake:read` |
| GET | `/monitors` | `sentinel:read` |
| GET | `/monitors/{id}/alerts` | `sentinel:read` |
| GET | `/monitors/{id}/metrics` | `sentinel:read` |
| GET | `/monitors/{id}/occurrences` | `sentinel:read` |
| GET | `/monitors/{id}/schedule` | `sentinel:read` |
| GET | `/monitors/{id}/series` | `sentinel:read` |
| GET | `/notifications/deliveries` | `notifications:read` |
| GET | `/notifications/inbox` | `notifications:read` |
| GET | `/notifications/status` | `notifications:read` |
| GET | `/notifications/unread-count` | `notifications:read` |
| GET | `/recon/controls` | `recon:read` |
| GET | `/recon/runs/{id}/results` | `recon:read` |
| GET | `/roles` | `roles:read` |
| GET | `/roles/permissions` | `roles:read` |
| GET | `/roles/{id}` | `roles:read` |
| GET | `/rules` | `rules:read` |
| GET | `/runs` | `runs:read` |
| GET | `/runs/{id}` | `runs:read` |
| GET | `/runs/{id}/diagnostics` | `runs:read` |
| GET | `/runs/{id}/evidence` | `artifacts:download` |
| GET | `/runs/{id}/execution-plan` | `runs:read` |
| GET | `/runs/{id}/export.csv` | `exports:download` |
| GET | `/runs/{id}/export.xlsx` | `exports:download` |
| GET | `/runs/{id}/results` | `runs:read` |
| GET | `/system/engines` | `system:read` |
| GET | `/users` | `users:read` |
| GET | `/users/roles` | `users:read` |
| GET | `/users/{id}` | `users:read` |
| PATCH | `/connections/{id}` | `connections:manage` |
| PATCH | `/delivery/destinations/{id}` | `destinations:manage` |
| PATCH | `/exceptions/{id}` | `exceptions:write` |
| PATCH | `/roles/{id}` | `roles:manage` |
| PATCH | `/users/{id}` | `users:manage` |
| POST | `/acquisitions/{id}/cancel` | `datasets:write` |
| POST | `/connections` | `connections:manage` |
| POST | `/connections/test` | `connections:manage` |
| POST | `/connections/{id}/acquisitions` | `connections:use` |
| POST | `/connections/{id}/datasets` | `connections:use` |
| POST | `/connections/{id}/test` | `connections:use` |
| POST | `/datasets` | `datasets:write` |
| POST | `/datasets/uploads/inspect` | `datasets:write` |
| POST | `/datasets/uploads/stage` | `datasets:write` |
| POST | `/datasets/{id}/acquisitions` | `datasets:write` |
| POST | `/datasets/{id}/acquisitions/refresh` | `connections:use` |
| POST | `/datasets/{id}/refresh-source` | `connections:use` |
| POST | `/datasets/{id}/versions/upload` | `datasets:write` |
| POST | `/delivery/automations` | `delivery:schedule` |
| POST | `/delivery/automations/{id}/dispatch` | `delivery:execute` |
| POST | `/delivery/automations/{id}/versions` | `delivery:schedule` |
| POST | `/delivery/configurations` | `delivery:configure` |
| POST | `/delivery/configurations/{id}/versions` | `delivery:configure` |
| POST | `/delivery/destinations` | `destinations:manage` |
| POST | `/delivery/destinations/test` | `destinations:manage` |
| POST | `/delivery/destinations/{id}/test` | `destinations:use` |
| POST | `/delivery/preflight` | `delivery:configure` |
| POST | `/delivery/preview` | `delivery:configure` |
| POST | `/delivery/runs` | `delivery:execute` |
| POST | `/delivery/runs/{id}/repair-evidence` | `delivery:repair_evidence` |
| POST | `/delivery/runs/{id}/resume-target` | `delivery:review_unknown` |
| POST | `/delivery/runs/{id}/reviews` | `delivery:review_unknown` |
| POST | `/delivery/validations` | `delivery:configure` |
| POST | `/delivery/validations/{id}/cancel` | `delivery:configure` |
| POST | `/exceptions/{id}/attachments` | `exceptions:write` |
| POST | `/exceptions/{id}/comments` | `exceptions:write` |
| POST | `/exceptions/{id}/validate` | `exceptions:write` |
| POST | `/execution-plans/preview` | `runs:execute` |
| POST | `/findings/{id}/exceptions` | `exceptions:write` |
| POST | `/intake/contracts` | `intake:configure` |
| POST | `/intake/contracts/{id}/versions` | `intake:configure` |
| POST | `/intake/runs` | `intake:execute` |
| POST | `/monitors` | `sentinel:configure` |
| POST | `/monitors/{id}/runs` | `sentinel:execute` |
| POST | `/monitors/{id}/schedule` | `sentinel:schedule` |
| POST | `/monitors/{id}/versions` | `sentinel:configure` |
| POST | `/notifications/inbox/read-all` | `notifications:read` |
| POST | `/notifications/inbox/{id}/read` | `notifications:read` |
| POST | `/notifications/inbox/{id}/unread` | `notifications:read` |
| POST | `/recon/controls` | `recon:configure` |
| POST | `/recon/controls/{id}/versions` | `recon:configure` |
| POST | `/recon/runs` | `recon:execute` |
| POST | `/roles` | `roles:manage` |
| POST | `/runs/{id}/cancel` | `runs:execute` |
| POST | `/users` | `users:manage` |
| POST | `/users/{id}/regenerate-credentials` | `users:manage` |
| POST | `/users/{id}/resend-credentials` | `users:manage` |
| POST | `/users/{id}/reset-password` | `users:manage` |

La inbox aplica usuario destinatario y permisos actuales del módulo a lista, contador y lectura. El responsable de programación se revalida al despachar y antes de STARTED; SYSTEM no es una cuenta ejecutora.
