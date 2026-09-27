# Matriz de permisos HTTP y catálogo 0.6.0

Fuente autoritativa: `backend/src/trackvance/permissions.py`. Esta tabla enumera métodos y rutas del producto; una ruta protegida no enumerada se deniega incluso a Administrator. El prefijo común es `/api/v1`.

Los controles por organización, CSRF, primer acceso y disponibilidad del recurso se añaden al permiso de ruta. Administrator resuelve todos los códigos del catálogo sin grants estáticos. `users:manage` y `roles:manage` son exclusivos de ese rol de sistema.

## Catálogo y dependencias

| Código | Grupo | Dependencias directas | Delegable |
|---|---|---|---|
| `artifacts:download` | Evidencia / exports | — | Sí |
| `audit:read` | Auditoría | — | Sí |
| `connections:manage` | Conexiones | `connections:read` | Sí |
| `connections:read` | Conexiones | — | Sí |
| `connections:use` | Conexiones | `connections:read` | Sí |
| `datasets:read` | Datasets | — | Sí |
| `datasets:write` | Datasets | `datasets:read` | Sí |
| `delivery:alter_target` | Data Delivery | `delivery:execute`, `delivery:read` | Sí |
| `delivery:configure` | Data Delivery | `datasets:read`, `delivery:read`, `destinations:read`, `destinations:use` | Sí |
| `delivery:execute` | Data Delivery | `datasets:read`, `delivery:read` | Sí |
| `delivery:overwrite` | Data Delivery | `delivery:execute`, `delivery:read` | Sí |
| `delivery:read` | Data Delivery | — | Sí |
| `delivery:repair_evidence` | Data Delivery | `delivery:execute`, `delivery:read` | Sí |
| `delivery:review_unknown` | Data Delivery | `delivery:read` | Sí |
| `destinations:manage` | Destinos | `destinations:read` | Sí |
| `destinations:read` | Destinos | — | Sí |
| `destinations:use` | Destinos | `destinations:read` | Sí |
| `exceptions:close` | Excepciones | `exceptions:read`, `exceptions:write` | Sí |
| `exceptions:read` | Excepciones | — | Sí |
| `exceptions:write` | Excepciones | `exceptions:read` | Sí |
| `exports:download` | Evidencia / exports | — | Sí |
| `intake:configure` | Data Intake | `datasets:read`, `intake:read` | Sí |
| `intake:execute` | Data Intake | `datasets:read`, `intake:read` | Sí |
| `intake:read` | Data Intake | — | Sí |
| `notifications:manage` | Notificaciones | `notifications:read` | Sí |
| `notifications:read` | Notificaciones | — | Sí |
| `recon:configure` | ReconOps | `datasets:read`, `recon:read` | Sí |
| `recon:execute` | ReconOps | `datasets:read`, `recon:read` | Sí |
| `recon:read` | ReconOps | — | Sí |
| `roles:manage` | Roles | `roles:read` | No |
| `roles:read` | Roles | — | Sí |
| `rules:read` | Reglas | — | Sí |
| `runs:execute` | Ejecuciones | `runs:read` | Sí |
| `runs:read` | Ejecuciones | — | Sí |
| `sentinel:configure` | Sentinel | `datasets:read`, `sentinel:read` | Sí |
| `sentinel:execute` | Sentinel | `datasets:read`, `sentinel:read` | Sí |
| `sentinel:read` | Sentinel | — | Sí |
| `sentinel:schedule` | Sentinel | `sentinel:execute`, `sentinel:read` | Sí |
| `system:read` | Sistema | — | Sí |
| `users:manage` | Usuarios | `users:read` | No |
| `users:read` | Usuarios | — | Sí |

El backend exige la clausura transitiva completa y rechaza códigos ajenos al catálogo. La UI puede completar dependencias, pero no altera esta autoridad.

## Rutas protegidas

| Método | Ruta | Permiso principal |
|---|---|---|
| GET | `/artifacts/{id}/download` | `artifacts:download` |
| GET | `/audit-events` | `audit:read` |
| GET | `/connections` | `connections:read` |
| POST | `/connections` | `connections:manage` |
| POST | `/connections/test` | `connections:manage` |
| DELETE | `/connections/{id}` | `connections:manage` |
| GET | `/connections/{id}` | `connections:read` |
| PATCH | `/connections/{id}` | `connections:manage` |
| POST | `/connections/{id}/datasets` | `connections:use` |
| GET | `/connections/{id}/objects` | `connections:use` |
| GET | `/connections/{id}/preview` | `connections:use` |
| GET | `/connections/{id}/schemas` | `connections:use` |
| POST | `/connections/{id}/test` | `connections:use` |
| GET | `/dashboard` | `runs:read` |
| GET | `/dataset-versions/{id}/profile` | `datasets:read` |
| GET | `/datasets` | `datasets:read` |
| POST | `/datasets` | `datasets:write` |
| POST | `/datasets/uploads/inspect` | `datasets:write` |
| GET | `/datasets/{id}` | `datasets:read` |
| POST | `/datasets/{id}/refresh-source` | `connections:use` |
| GET | `/datasets/{id}/schema` | `datasets:read` |
| POST | `/datasets/{id}/versions/upload` | `datasets:write` |
| GET | `/delivery/configurations` | `delivery:read` |
| POST | `/delivery/configurations` | `delivery:configure` |
| POST | `/delivery/configurations/{id}/versions` | `delivery:configure` |
| GET | `/delivery/destinations` | `destinations:read` |
| POST | `/delivery/destinations` | `destinations:manage` |
| POST | `/delivery/destinations/test` | `destinations:manage` |
| DELETE | `/delivery/destinations/{id}` | `destinations:manage` |
| GET | `/delivery/destinations/{id}` | `destinations:read` |
| PATCH | `/delivery/destinations/{id}` | `destinations:manage` |
| GET | `/delivery/destinations/{id}/schemas` | `destinations:use` |
| GET | `/delivery/destinations/{id}/table-metadata` | `destinations:use` |
| GET | `/delivery/destinations/{id}/tables` | `destinations:use` |
| GET | `/delivery/destinations/{id}/target-policy` | `delivery:read` |
| POST | `/delivery/destinations/{id}/test` | `destinations:use` |
| POST | `/delivery/preflight` | `delivery:configure` |
| POST | `/delivery/preview` | `delivery:configure` |
| GET | `/delivery/runs` | `delivery:read` |
| POST | `/delivery/runs` | `delivery:execute` |
| GET | `/delivery/runs/{id}/attempts` | `delivery:read` |
| GET | `/delivery/runs/{id}/receipt` | `artifacts:download` |
| POST | `/delivery/runs/{id}/repair-evidence` | `delivery:repair_evidence` |
| GET | `/delivery/runs/{id}/reviews` | `delivery:read` |
| POST | `/delivery/runs/{id}/reviews` | `delivery:review_unknown` |
| GET | `/exceptions` | `exceptions:read` |
| GET | `/exceptions/assignees` | `exceptions:read` |
| GET | `/exceptions/{id}` | `exceptions:read` |
| PATCH | `/exceptions/{id}` | `exceptions:write` |
| POST | `/exceptions/{id}/attachments` | `exceptions:write` |
| GET | `/exceptions/{id}/attachments/{attachment_id}/download` | `artifacts:download` |
| POST | `/exceptions/{id}/comments` | `exceptions:write` |
| POST | `/exceptions/{id}/validate` | `exceptions:write` |
| POST | `/execution-plans/preview` | `runs:execute` |
| GET | `/findings` | `runs:read` |
| POST | `/findings/{id}/exceptions` | `exceptions:write` |
| GET | `/intake/contracts` | `intake:read` |
| POST | `/intake/contracts` | `intake:configure` |
| POST | `/intake/contracts/{id}/versions` | `intake:configure` |
| POST | `/intake/runs` | `intake:execute` |
| GET | `/intake/runs/{id}/errors` | `intake:read` |
| GET | `/monitors` | `sentinel:read` |
| POST | `/monitors` | `sentinel:configure` |
| GET | `/monitors/{id}/alerts` | `sentinel:read` |
| GET | `/monitors/{id}/metrics` | `sentinel:read` |
| GET | `/monitors/{id}/occurrences` | `sentinel:read` |
| POST | `/monitors/{id}/runs` | `sentinel:execute` |
| GET | `/monitors/{id}/schedule` | `sentinel:read` |
| POST | `/monitors/{id}/schedule` | `sentinel:schedule` |
| GET | `/monitors/{id}/series` | `sentinel:read` |
| POST | `/monitors/{id}/versions` | `sentinel:configure` |
| GET | `/notifications/deliveries` | `notifications:read` |
| GET | `/notifications/status` | `notifications:read` |
| GET | `/recon/controls` | `recon:read` |
| POST | `/recon/controls` | `recon:configure` |
| POST | `/recon/controls/{id}/versions` | `recon:configure` |
| POST | `/recon/runs` | `recon:execute` |
| GET | `/recon/runs/{id}/results` | `recon:read` |
| GET | `/roles` | `roles:read` |
| POST | `/roles` | `roles:manage` |
| GET | `/roles/permissions` | `roles:read` |
| DELETE | `/roles/{id}` | `roles:manage` |
| GET | `/roles/{id}` | `roles:read` |
| PATCH | `/roles/{id}` | `roles:manage` |
| GET | `/rules` | `rules:read` |
| GET | `/runs` | `runs:read` |
| GET | `/runs/{id}` | `runs:read` |
| POST | `/runs/{id}/cancel` | `runs:execute` |
| GET | `/runs/{id}/diagnostics` | `runs:read` |
| GET | `/runs/{id}/evidence` | `artifacts:download` |
| GET | `/runs/{id}/execution-plan` | `runs:read` |
| GET | `/runs/{id}/export.csv` | `exports:download` |
| GET | `/runs/{id}/export.xlsx` | `exports:download` |
| GET | `/runs/{id}/results` | `runs:read` |
| GET | `/system/engines` | `system:read` |
| GET | `/users` | `users:read` |
| POST | `/users` | `users:manage` |
| GET | `/users/roles` | `users:read` |
| DELETE | `/users/{id}` | `users:manage` |
| GET | `/users/{id}` | `users:read` |
| PATCH | `/users/{id}` | `users:manage` |
| DELETE | `/users/{id}/external-identities/{identity_id}` | `users:manage` |
| POST | `/users/{id}/resend-credentials` | `users:manage` |
| POST | `/users/{id}/reset-password` | `users:manage` |

## Condiciones adicionales

- `/runs`, `/findings` y `/dashboard` aceptan acceso de lectura a un módulo y filtran el contenido según los permisos efectivos de ese módulo. `runs:read` por sí solo no concede lectura de ningún módulo.
- Un detalle genérico `/runs/{id}` o Configuration requiere `{module}:read` del registro real. Esto se aplica también a resultados, diagnóstico, plan, evidencia y exports. Las descargas de Artifact siguen los vínculos al Run; sin vínculo requieren datasets:read o exceptions:read según el recurso.
- Cancelar un Run y previsualizar su plan requiere `{module}:execute`, aunque se acceda mediante una ruta compartida. Los permisos globales runs no sustituyen esta comprobación.
- Publicar y ejecutar Delivery OVERWRITE requiere delivery:overwrite. Crear estructura o activar columnas de auditoría exige delivery:alter_target además de los permisos base. La política del target puede añadir precondiciones; el flag de UI no es autoridad.
- Cerrar casos y cambiar auto-resolve exige exceptions:close además de exceptions:write. Activar la programación exige sentinel:execute y sentinel:schedule.
- Usuarios/roles/identidades/notificaciones siempre se consultan dentro de la organización del actor. Los IDs ajenos no crean autorización cruzada.

## Endpoints de sesión y públicos

| Acceso | Método | Ruta |
|---|---|---|
| Público/configurado | POST | `/auth/demo` |
| Público/configurado | POST | `/auth/login` |
| Público/configurado | GET | `/auth/providers` |
| Público/configurado | GET | `/auth/sso/google/callback` |
| Público/configurado | GET | `/auth/sso/google/start` |
| Público/configurado | GET | `/auth/sso/microsoft/callback` |
| Público/configurado | GET | `/auth/sso/microsoft/start` |
| Público/configurado | GET | `/health` |
| Público/configurado | GET | `/health/ready` |
| Sesión; permitido en primer acceso | POST | `/auth/first-login/change-password` |
| Sesión; permitido en primer acceso | POST | `/auth/logout` |
| Sesión; permitido en primer acceso | GET | `/me` |

Health también conserva `/health` y `/health/ready` sin prefijo. Los callbacks tienen proveedores concretos permitidos; la plantilla OpenAPI `{provider}` no abre proveedores arbitrarios. SSO y demo siguen condicionados a su configuración.

## Grants iniciales de roles migrados

| Permiso | Administrator | Data Owner / Lead | Data Analyst | Operations | Auditor |
|---|---|---|---|---|---|
| `artifacts:download` | Sí | Sí | Sí | Sí | Sí |
| `audit:read` | Sí | Sí | Sí | — | Sí |
| `connections:manage` | Sí | Sí | — | — | — |
| `connections:read` | Sí | Sí | Sí | Sí | Sí |
| `connections:use` | Sí | Sí | Sí | — | — |
| `datasets:read` | Sí | Sí | Sí | Sí | Sí |
| `datasets:write` | Sí | Sí | Sí | — | — |
| `delivery:alter_target` | Sí | Sí | Sí | — | — |
| `delivery:configure` | Sí | Sí | Sí | — | — |
| `delivery:execute` | Sí | Sí | Sí | — | — |
| `delivery:overwrite` | Sí | Sí | Sí | — | — |
| `delivery:read` | Sí | Sí | Sí | Sí | Sí |
| `delivery:repair_evidence` | Sí | Sí | Sí | — | — |
| `delivery:review_unknown` | Sí | Sí | Sí | — | — |
| `destinations:manage` | Sí | Sí | — | — | — |
| `destinations:read` | Sí | Sí | Sí | Sí | Sí |
| `destinations:use` | Sí | Sí | Sí | — | — |
| `exceptions:close` | Sí | Sí | — | — | — |
| `exceptions:read` | Sí | Sí | Sí | Sí | Sí |
| `exceptions:write` | Sí | Sí | Sí | Sí | — |
| `exports:download` | Sí | Sí | Sí | Sí | Sí |
| `intake:configure` | Sí | Sí | Sí | — | — |
| `intake:execute` | Sí | Sí | Sí | — | — |
| `intake:read` | Sí | Sí | Sí | Sí | Sí |
| `notifications:manage` | Sí | — | — | — | — |
| `notifications:read` | Sí | — | — | — | — |
| `recon:configure` | Sí | Sí | Sí | — | — |
| `recon:execute` | Sí | Sí | Sí | — | — |
| `recon:read` | Sí | Sí | Sí | Sí | Sí |
| `roles:manage` | Sí | — | — | — | — |
| `roles:read` | Sí | — | — | — | Sí |
| `rules:read` | Sí | Sí | Sí | Sí | Sí |
| `runs:execute` | Sí | Sí | Sí | — | — |
| `runs:read` | Sí | Sí | Sí | Sí | Sí |
| `sentinel:configure` | Sí | Sí | Sí | — | — |
| `sentinel:execute` | Sí | Sí | Sí | — | — |
| `sentinel:read` | Sí | Sí | Sí | Sí | Sí |
| `sentinel:schedule` | Sí | Sí | Sí | — | — |
| `system:read` | Sí | — | — | — | Sí |
| `users:manage` | Sí | — | — | — | — |
| `users:read` | Sí | — | — | — | Sí |

Estos grants se usan sólo al migrar/provisionar los roles iniciales. Los cuatro roles no protegidos se pueden modificar y no se resincronizan al consultar permisos. Data Owner histórico se vincula a Data Owner / Lead; no queda una segunda política autoritativa.

Ver ADR 0016 para baja lógica, sesiones, revisión y bloqueo del último administrador. Las pruebas recorren OpenAPI y requieren que cada operación tenga una clasificación explícita.

## Controles adicionales por recurso y operación

La matriz es la puerta de entrada, no reemplaza organización, estado/versionado,
CSRF ni comprobaciones del objeto. Una sesión first-login sólo accede a /me,
logout y change-password. User/Role activos se resuelven en cada petición.

| Operación | Comprobación adicional |
| --- | --- |
| Runs, Findings y dashboard compartidos | Filtrado por módulos con read; un ID exige read del módulo del Run. |
| Cancelar Run / preview de plan | execute del módulo concreto; preview exige también datasets:read. |
| Evidencia, exports y artifacts | Scope de organización y read del módulo de origen; adjuntos requieren lectura del caso. |
| Configurar / ejecutar Delivery | Dataset y destino del mismo scope; configurar depende de destinations:use. Publicar y ejecutar exigen delivery:overwrite para OVERWRITE y delivery:alter_target para CREATE o materializar auditoría. |
| Receipt Delivery | delivery:read además de artifacts:download. |
| Cerrar excepción / cambiar cierre automático | exceptions:close además de exceptions:write. |
| Roles y usuarios | CAS/version, no delegables, último admin, propio administrador y roles asociados protegidos. |
| Target audit | Policy irreversible, columnas reservadas, drift y privilegios SQL; manipular audit=false no desactiva policy. |

`notifications:manage` está reservado en el catálogo para evolución del servicio;
no expone un editor de secretos ni una ruta de envío arbitrario. Configuración
SMTP/OIDC sigue exclusivamente en el entorno del servidor.
