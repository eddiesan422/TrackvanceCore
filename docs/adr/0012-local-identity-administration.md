# ADR 0012: administración local de usuarios y roles

- Estado: implementado en el prototipo local 0.4.0.
- Fecha: 2026-09-19.

## Decisión

La administración local amplía el modelo `User` existente, sus sesiones y la
política central `permissions.py`. No requiere un directorio externo ni tablas
de roles configurables para satisfacer los cinco roles base: Administrator,
Data Owner / Lead, Data Analyst, Operations y Auditor. `Data Owner` permanece
como alias histórico con iguales permisos.

`users:read` permite consultar cuentas y permisos efectivos; `users:write`,
crear, editar, activar/desactivar, asignar uno de los roles conocidos y
restablecer una contraseña. Administrator dispone de ambos permisos; Auditor
solo consulta. Desactivar sustituye a borrar: las referencias históricas de
auditoría, ejecución, asignación y adjuntos permanecen válidas.

Las respuestas contienen identidad, actividad, rol, permisos efectivos, revisión
y fechas. Nunca devuelven hash de contraseña, token, sesión ni secreto. Los inputs
de contraseña usan `SecretStr`, mínimo 12 caracteres y máximo 1024. Se conserva
literalmente la contraseña, sin trim, y se almacena únicamente hash Argon2.
Restablecer significa definir una contraseña nueva y revocar todas las sesiones;
no recupera ni muestra la contraseña previa. La comunicación de la contraseña
inicial/restablecida es responsabilidad del administrador por un canal seguro.

Cambiar correo, rol o actividad también revoca sesiones. El middleware consulta
usuario/actividad/rol en cada petición, por lo que no confía en permisos guardados
en el navegador. La unicidad global del correo se conserva por compatibilidad con
el login existente; los conflictos no revelan a qué organización pertenece.

## Concurrencia y seguridad

La migración `0006_local_identity_exceptions` añade `users.version`, `updated_at`
y `password_changed_at`; no modifica migraciones aplicadas ni contraseñas.
Las mutaciones usan compare and swap. Desactivar o degradar administradores exige
que permanezca al menos otro administrador activo en la misma organización.
Un lock transaccional por organización en PostgreSQL serializa esa decisión entre
ediciones concurrentes de usuarios distintos; SQLite usa su bloqueo de escritura.

Todos los endpoints aplican RBAC, CSRF y aislamiento por organización. Las auditorías
`USER_CREATED`, `USER_UPDATED` y `USER_PASSWORD_RESET` conservan actor estable y
campos seguros. La UI muestra permisos y errores reales, bloquea controles para
roles de consulta y exige confirmar la nueva contraseña en el formulario de reset.

## APIs

- `GET /api/v1/users[?active=true|false]` y `GET /users/{id}`.
- `GET /api/v1/users/roles`: cinco roles base y permisos efectivos.
- `POST /api/v1/users`: nombre, correo, rol, actividad y contraseña explícita.
- `PATCH /api/v1/users/{id}`: revisión esperada y cambios de perfil/rol/actividad.
- `POST /api/v1/users/{id}/reset-password`: revisión esperada y nueva contraseña.

Las rutas abreviadas se entienden bajo `/api/v1`. Respuestas de usuario tipadas en
OpenAPI. El acceso demo continúa controlado por su configuración independiente;
esta implementación no afirma que demo sea SSO ni identidad de producción.

## Verificación y futuro

Tests backend/UI/E2E cubren matriz de cinco roles, CSRF, organización, cuentas
inactivas, correo duplicado, Argon2, secretos ausentes, revocación de sesiones,
revisión obsoleta y último administrador. OIDC/SSO permanece objetivo futuro y
deberá proporcionar identidad estable sin reemplazar RBAC ni trazabilidad locales.
