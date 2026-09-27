# ADR 0018 — Puerto de notificaciones y entrega SMTP

Fecha: 2026-09-27. Estado: implementado en 0.6.0 para credenciales temporales por email.

## Arquitectura

0.5.1 declaraba una frontera futura sin adaptador operativo. 0.6.0 implementa `NotificationService → NotificationDelivery → SMTPNotificationDelivery`. El puerto recibe un `NotificationMessage` inmutable con destinatario, asunto y body sólo en memoria; su representación excluye el body. El servicio persiste intentos y decide estado; el adaptador conoce SMTP. La lógica de usuarios invoca el template `USER_TEMPORARY_CREDENTIALS`, sin crear otro motor de correo.

El contrato admite futuras clases de eventos y templates sin cambiar transporte. Recipient USER es la única resolución operativa. ROLE, USERS, DOMAIN y GROUP permanecen diseño futuro; no se implementan alertas, runs, excepciones ni entregas automáticas de otro tipo. SSO y SMTP son integraciones independientes.

## Persistencia y fallos

La migración aditiva `0011_notification_delivery` crea `notification_deliveries`: organización, evento, template, canal, tipo de destinatario, User, snapshot de email, provider, número de intento, estado, error_code y fechas. No existe columna para body, contraseña, token, cabecera Authorization, client secret o SMTP password.

El User y su hash temporal se confirman antes del envío. Un intento se confirma PENDING; tras la llamada al adaptador pasa a SENT o FAILED y se audita con códigos sanitizados. Un crash entre envío y confirmación puede dejar PENDING: no se afirma entrega ni se reintenta una contraseña cuyo plaintext no se conservó. El operador debe regenerar y reenviar. La infraestructura no promete exactly-once SMTP.

Regenerar produce siempre un secreto nuevo, revoca sesiones y renueva 24 horas; la contraseña anterior queda invalidada. Fallar la entrega conserva la cuenta, hace visible el estado y permite otro intento. No se muestra el secreto al administrador. No se persiste un outbox con contenido sensible ni se recupera la contraseña anterior.

## SMTP y template

Variables: `TRACKVANCE_SMTP_ENABLED`, HOST, PORT, USERNAME, PASSWORD, FROM_ADDRESS, FROM_NAME y SECURITY. Se admiten STARTTLS, SSL/TLS y NONE sólo con `TRACKVANCE_SMTP_ALLOW_INSECURE=true` explícito para local/pruebas. TLS usa validación de certificados por defecto; timeouts limitan las operaciones. Secretos se inyectan sólo al API.

Si no se habilita/configura SMTP, la instalación sigue operativa y el intento queda FAILED/NO_PROVIDER. Errores de autenticación/transporte se convierten a códigos limitados sin publicar respuestas del servidor que pudieran contener datos sensibles. El template contiene nombre, username, contraseña temporal, expiración UTC, URL pública, obligación de cambio y advertencia de seguridad. Los tests Mailpit pueden extraer las líneas `Username:` y `Contraseña temporal:` exclusivamente del buzón desechable; la aplicación no ofrece esta extracción.

`GET /notifications/status` exige notifications:read y devuelve habilitación/configuración/remitente/seguridad sin credenciales. `/notifications/deliveries` devuelve hasta 200 metadatos de la organización; detalles de User incluyen el último intento de credenciales. La configuración del proveedor permanece en entorno, no editable como secreto desde UI.

## Validación y límites

Pruebas comprueban fallo sin proveedor, adapter con error sensible, metadata sin contraseña/body, Argon2, secreto nuevo en regeneración, revocación y Mailpit. El puerto acepta un adaptador inyectado para pruebas sin usar cuentas reales. Las credenciales de proveedores externos no forman parte del respaldo funcional y deben restituirse aparte de los datos, según la guía de operaciones.

Limitación deliberada: el envío es síncrono y local, sin broker, jobs email ni reintento automático con contenido. Futuras notificaciones no sensibles podrán introducir entrega desacoplada mediante este mismo puerto, con garantías y payload definidas en otra decisión.
