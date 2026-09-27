# ADR 0018 — Retiro del envío de credenciales y conservación del historial

Fecha: 2026-09-27. Estado: decisión de SMTP de 0.6.0 sustituida en 0.6.1. Se conserva la persistencia histórica, no el transporte operativo.

## Antecedente de 0.6.0

0.5.1 declaraba una frontera futura sin adaptador. 0.6.0 implementó `NotificationService → NotificationDelivery → SMTPNotificationDelivery`: un mensaje inmutable con destinatario, asunto y cuerpo sólo en memoria. El template USER_TEMPORARY_CREDENTIALS entregaba por email una temporal de 24 horas. Únicamente USER tuvo resolución operativa; ROLE, USERS, DOMAIN, GROUP y alertas de ejecución quedaron pendientes.

El usuario y su hash se confirmaban antes del envío. El intento pasaba de PENDING a SENT o FAILED, con códigos sanitizados y auditoría NOTIFICATION_SENT/FAILED. SENT significaba aceptación SMTP, no lectura ni llegada final al buzón. Un crash podía dejar PENDING. No existía outbox con secretos ni recuperación de la contraseña previa. Las pruebas históricas usaron Mailpit desechable.

## Decisión vigente en 0.6.1

La creación y regeneración ya no envían correo. El backend genera 32 caracteres URL-safe con CSPRNG, conserva únicamente Argon2 y devuelve una respuesta efímera `UserCredentialIssueResponse` con UserResponse y temporary_credentials. Ese objeto incluye username, temporary_password, expires_at y must_change_password=true. Alta responde 201; regeneración 200 exige `{version}`, permiso users:manage, CSRF y organización. Las respuestas llevan `Cache-Control: no-store` y `Pragma: no-cache`.

El administrador ve la temporal una sola vez en un modal y puede copiar username, contraseña o ambos para entregarlos por el canal que elija. Cerrar o desmontar el modal descarta su estado. No se permite recuperar el secreto por GET ni mantenerlo en caché de React Query, Storage, cookies, URL, logs, auditoría, artifacts, backups o evidencia. Si se pierde, se genera uno nuevo; regenerar invalida la contraseña anterior, renueva 24 horas y revoca todas las sesiones. El primer acceso sigue exigiendo cambio local incluso cuando autentica mediante SSO.

Se elimina el servicio de entrega, el puerto operativo, el adaptador SMTP y el template de envío. Ninguna variable `TRACKVANCE_SMTP_*` habilita una capacidad. SMTP no figura como integración operativa ni como pantalla de configuración y las pruebas nuevas no necesitan Mailpit. Microsoft/Google SSO conserva su implementación, permanece opcional y deshabilitado por defecto; sus secretos no se reutilizan para correo.

Los aliases `/users/{id}/resend-credentials` y `/reset-password` se conservan deprecated y ejecutan exactamente la emisión de `/regenerate-credentials`, sin envío ni intento de notificación. El nombre histórico resend no implica entrega.

## Persistencia y compatibilidad

La migración `0011_notification_delivery`, el modelo NotificationDeliveryRecord y la tabla notification_deliveries se conservan intactos. Registran organización, evento/template, canal, tipo de destinatario, User, snapshot de email, proveedor, número de intento, estado, error_code y fechas. Nunca incluyeron columnas para body, contraseña, token, Authorization, client secret o password SMTP.

0.6.1 no crea nuevos registros por alta/regeneración ni modifica los PENDING/SENT/FAILED existentes. Todas las migraciones 0001..0012 permanecen byte a byte iguales y el head continúa en 0012; no se añade 0013. La compatibilidad de backup/restore incluye las filas históricas, aunque ningún transporte nuevo las utilice.

`GET /notifications/deliveries` permanece deprecated, exige notifications:read y devuelve hasta 200 metadatos de la organización. `GET /notifications/status` también está deprecated y responde siempre enabled=false, configured=false, provider=NONE, security=NONE, remitente vacío y availability=HISTORICAL_ONLY. No lee variables SMTP. Los permisos históricos se mantienen para compatibilidad del catálogo, sin habilitar envíos. UserResponse deja de incluir credential_delivery.

## Validación y futuro

Las pruebas comprueban emisión exclusiva en respuestas autorizadas, Argon2, caducidad, CAS, regeneración y revocación, primer acceso, no invocación SMTP incluso con variables legacy, cero nuevas notificaciones, preservación del historial y ausencia de secretos en respuestas ordinarias, DB, auditoría, logs y almacenamiento. El mock OIDC sigue probando code, PKCE, firmas, claims y usuarios preprovisionados sin correo. La evidencia se publica por ejecución, sin secretos.

No se ha elegido una tecnología para futuras notificaciones. Añadir cualquier canal, resolutor de destinatarios, procesamiento asíncrono o garantías de entrega requerirá otra decisión y una interfaz nueva acorde a sus requisitos. Esta tabla histórica por sí sola no constituye un motor habilitable.
