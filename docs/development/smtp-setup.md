# SMTP: antecedente histórico de 0.6.0, deshabilitado en 0.6.1

Esta página se conserva para explicar datos y documentación de instalaciones anteriores. **Trackvance Core 0.6.1 no configura, habilita ni envía correo SMTP.** El adaptador, servicio, template operativo y pantalla de Notificaciones se retiraron. No se ha elegido un transporte futuro.

## Flujo vigente de credenciales

En Configuración → Usuarios, el administrador crea una cuenta o usa **Regenerar credenciales**. La respuesta de esa operación contiene una temporal de 24 horas que se muestra una sola vez, junto con username, nombre, rol y expiración. El modal permite copiar usuario, contraseña o ambos. El administrador puede entregarlos por el canal que elija.

Cerrar o desmontar el modal descarta el secreto; no se puede recuperar mediante el detalle, GET o historial. Si se pierde, se regenera. La regeneración sustituye el hash, invalida la contraseña anterior y revoca todas las sesiones. El primer acceso obliga a definir una contraseña local nueva. No se crea ningún intento de notificación ni se necesita una cuenta de correo, App Password, SMTP AUTH o Mailpit.

La API devuelve `UserCredentialIssueResponse` únicamente desde alta/regeneración y sus aliases deprecated; aplica no-store/no-cache. UserResponse normal no contiene secreto ni credential_delivery. El frontend no conserva la temporal en caché, Storage, cookies o URL. Evita capturas, tickets, logs y archivos de evidencia con credenciales.

## Compatibilidad con instalaciones 0.6.0

0.6.0 tenía un adaptador SMTP con STARTTLS/SSL/TLS y NONE limitado explícitamente a pruebas locales. Los nombres históricos `TRACKVANCE_SMTP_ENABLED`, HOST, PORT, USERNAME, PASSWORD, FROM_ADDRESS, FROM_NAME, SECURITY y ALLOW_INSECURE pertenecen a ese release. **No tienen efecto en 0.6.1 y no existe una combinación que reactive el envío.** No añadas variables, un overlay Mailpit ni secretos SMTP a una instalación nueva.

La tabla notification_deliveries se mantiene para preservar backups e historial. SENT documentaba aceptación por un servidor; FAILED podía registrar NO_PROVIDER u otros códigos; PENDING podía quedar tras una interrupción. Esos estados históricos no cambian ni provocan reenvíos. Alta/regeneración 0.6.1 no añade filas ni eventos NOTIFICATION_SENT/FAILED.

Las lecturas API deprecated `/notifications/deliveries` y `/notifications/status` conservan compatibilidad con permisos y organización. El estado actual es siempre disabled/unconfigured, availability=HISTORICAL_ONLY. No existe pantalla para habilitarlo. Las migraciones 0001..0012 y NotificationDeliveryRecord se conservan intactos; no hay migración 0013.

## SSO y recuperación

El correo sigue siendo metadata de identidad y puede servir para el primer enlace SSO verificado. Microsoft y Google continúan siendo integraciones de autenticación independientes, opcionales y deshabilitadas por defecto. La [guía SSO](sso-setup.md) describe su activación manual futura; no necesita SMTP ni reutiliza sus secretos.

Un restore conserva las filas históricas de notificaciones sin reactivar transportes. `.env` y secretos externos no forman parte del backup funcional. Los secretos OAuth de una integración SSO habilitada se restituyen mediante la configuración privada del operador; las antiguas variables SMTP no habilitan nada. Ver [ADR 0018](../adr/0018-notification-delivery.md) y el [contrato de identidad](identity-060.md).
