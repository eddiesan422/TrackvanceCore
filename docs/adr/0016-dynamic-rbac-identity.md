# ADR 0016 — Roles persistentes y ciclo de vida de identidades

Fecha: 2026-09-27. Estado: RBAC implementado en 0.6.0 y conservado en 0.6.1; emisión de credenciales actualizada en 0.6.1. Evidencia por versión en `docs/development/evidence/`.

## Problema y decisión

En 0.5.1 `User.role` seleccionaba una tabla de permisos estática. Un rol nuevo exigía cambiar código; una ruta sin clasificación obtenía `runs:read`. En 0.6.0 la autorización utiliza exclusivamente `User.role_id → Role → RolePermission`. El catálogo versionado `permissions.CATALOG` pertenece al producto; la administración crea roles, pero no códigos de permiso. Los permisos no se copian al usuario ni a la sesión.

`Role` pertenece a una organización y conserva UUID, nombre normalizado único, descripción, estado, baja lógica, revisión y fechas. `RolePermission` usa clave compuesta `(role_id,permission_code)`. Cada solicitud carga el rol vigente de la organización del usuario; los códigos desconocidos y roles desactivados/eliminados no conceden acceso. La matriz `ENDPOINT_MATRIX` enumera método y ruta exactos: rutas desconocidas se deniegan incluso al administrador. OpenAPI publica el permiso principal y los tests recorren todas las operaciones para detectar omisiones.

El catálogo separa consulta/configuración/ejecución de Intake, ReconOps, Sentinel y Delivery; fuentes y destinos tienen lectura, uso y administración independientes. Sobrescritura, ALTER del target, revisión UNKNOWN, reparación de evidencia y programación requieren permisos específicos. Las dependencias se validan transitivamente en backend. Configurar/ejecutar requiere leer el dataset; configurar Delivery requiere usar/leer destinos. `users:manage` y `roles:manage` son no delegables: únicamente el Administrator protegido los obtiene, incluso si una fila de permisos custom fuera corrompida manualmente.

Los accesos compartidos a runs requieren además el permiso del módulo real. Listas, findings y dashboard filtran módulos; lectura de detalles, exports, evidencia y artifacts vinculados comprueba el módulo. `runs:read` por sí solo no revela contenido de módulos denegados. Cancelar y previsualizar un plan comprueba ejecución del módulo concreto. La visibilidad de botones no sustituye estas comprobaciones.

## Administrator y concurrencia

El rol de sistema tiene `system_key=ADMINISTRATOR`, único por organización, y resuelve automáticamente todo el catálogo actual sin filas estáticas de grants. No puede borrarse, desactivarse, renombrarse ni perder permisos. Los otros cuatro roles iniciales son editables; `Data Owner` se enlaza al único `Data Owner / Lead` y el texto histórico se conserva.

Toda mutación de identidad adquiere un lock por organización: `pg_advisory_xact_lock` en PostgreSQL y bloqueo de escritura en SQLite. Las revisiones esperadas rechazan ediciones obsoletas. Desactivar o degradar al último administrador activo/no eliminado se bloquea. La baja propia se bloquea; otro administrador debe realizarla. Un rol con cualquier usuario no eliminado, aunque esté desactivado, no puede retirarse. Un usuario eliminado no bloquea la baja lógica del rol. Nombres de roles, usernames y emails de cuentas eliminadas siguen reservados.

Modificar permisos incrementa la versión del rol y afecta la petición siguiente sin invalidar sesiones; `/me` devuelve `role_version` y la UI refresca su estado. Cambiar rol, correo, username o actividad revoca sesiones. First login vuelve a validar la sesión persistida y la vigencia de las credenciales después de adquirir el lock, para que una solicitud que esperaba no sobreviva a una revocación concurrente.

## Usuarios y credenciales

Nuevos usuarios requieren nombres, apellidos, username, correo y role_id; active es opcional y vale true por defecto. El nombre mostrado concatena nombres y apellidos. Username admite 3–80 caracteres ASCII (`a-z`, dígitos, `.`, `_`, `-`), se normaliza a minúsculas y es globalmente único; el correo conserva login compatible. Login por username o email utiliza mensajes genéricos. El registro mantiene `must_change_password`, expiración temporal, fechas de contraseña/último acceso, revisión y baja lógica.

Crear o regenerar produce 32 caracteres URL-safe de entropía criptográfica; únicamente se guarda su hash Argon2. La expiración es de 24 horas. En 0.6.0 se entregaba por SMTP. Desde 0.6.1 se emite sólo en la respuesta inmediata de alta/regeneración `UserCredentialIssueResponse={user,temporary_credentials:{username,temporary_password,expires_at,must_change_password:true}}`, con no-store/no-cache. El modal administrativo muestra el secreto una vez y descarta su estado al cerrar/desmontar; no usa almacenamiento ni caché del navegador. UserResponse normal y GET nunca lo contienen. No se envía correo ni se generan notificaciones. Regenerar sustituye el hash y revoca todas las sesiones; nunca recupera el secreto anterior.

Una sesión de primer acceso sólo puede usar `/me`, `/auth/first-login/change-password` y `/auth/logout`. La nueva contraseña tiene 12–1024 caracteres y debe diferir de la temporal. El cambio elimina expiración/restricción, revoca todas las sesiones y crea cookie/CSRF nuevos. Una sesión local restringida deja de servir si vence la contraseña temporal. SSO puede iniciar este mismo primer acceso aunque la temporal ya haya expirado. Todos los usuarios conservan autenticación local.

## Migración y compatibilidad

`0010_dynamic_rbac_identity` añade roles, grants, identidades externas, estado OIDC y campos User/Session; no modifica 0001–0009. Los registros legacy conservan `name`, `role`, email, hash, actividad, revisión, fechas y actores históricos. Nombres/apellidos y último acceso permanecen NULL. El username se deriva de la parte local del correo, reemplazando caracteres inválidos por `-`; colisiones se resuelven con sufijos `-2`, `-3`, etc., en orden determinista de correo/id. Los roles desconocidos migran sin privilegios.

`identity_bootstrap` adapta únicamente llamadas ORM históricas de bootstrap/tests convirtiendo la asignación textual explícita a un role_id persistente. No se acepta `role` textual en la API nueva ni se consulta para autorizar. Los defaults se aplican al crear roles iniciales, nunca al consultar permisos. El campo de texto se conserva como compatibilidad histórica, no como segunda autoridad.

Las migraciones SQLite que reconstruyen tablas se ejecutan en una transacción explícita, suspendiendo FK solamente en la conexión de migración y comprobando `foreign_key_check` antes del commit; el estado original se restaura. Se prueba upgrade de usuarios/sesiones poblados, downgrade/upgrade y paridad ORM. PostgreSQL conserva sus constraints normales.

0.6.1 no cambia modelo ni esquema: 0001..0012 se mantienen byte a byte; no existe 0013. NotificationDeliveryRecord y sus filas históricas se conservan según ADR 0018.

## Contratos y auditoría

`GET /roles/permissions`, `GET/POST /roles`, `GET/PATCH/DELETE /roles/{id}`; PATCH/DELETE reciben versión esperada. `GET /users/roles` permanece como listado compatible. Users usa GET/POST/PATCH/DELETE y `/users/{id}/regenerate-credentials` con `{version}`. `/resend-credentials` y `/reset-password` quedan aliases deprecated de emisión, sin SMTP ni password en input. El alta responde 201 y la regeneración 200 con el envelope efímero. La baja siempre es lógica. Los detalles incluyen métodos de acceso y estado de cambio/expiración, nunca la contraseña ni metadata de envío.

Eventos: ROLE_CREATED, ROLE_UPDATED, ROLE_PERMISSIONS_CHANGED, ROLE_DISABLED, ROLE_DELETED, USER_CREATED, USER_UPDATED, USER_ROLE_CHANGED, USER_DISABLED, USER_DELETED, USER_CREDENTIALS_REGENERATED y USER_PASSWORD_CHANGED. Sólo identificadores, nombres de campos, códigos de permisos, revisión y estado entran en metadata sanitizada.

Pruebas automatizadas cubren dependencias, catálogo, no delegación, rol protegido, efecto inmediato, revocación, último administrador, bajas con usuarios desactivados/eliminados, conflictos, CSRF, aislamiento, primer acceso, expiración, fugas y accesos compartidos. Los resultados y los E2E reales se registran por ejecución, sin asumir conteos anticipados.
