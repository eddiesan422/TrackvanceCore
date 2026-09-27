# Configurar email de credenciales

SMTP y SSO son integraciones independientes. Un client secret Google/Microsoft
para login no sirve como contraseña SMTP y el adaptador no reutiliza tokens SSO.

## SMTP genérico

En el `.env` privado establece `TRACKVANCE_SMTP_ENABLED=true`, host, puerto,
remitente y modo de seguridad. Los nombres completos están en `.env.example`.
Para submission con STARTTLS utiliza normalmente puerto 587 y
`TRACKVANCE_SMTP_SECURITY=STARTTLS`; para TLS implícito utiliza el puerto que
indique el proveedor (habitualmente 465) y `SSL` o `TLS`. El adaptador valida el
certificado con el almacén de confianza del sistema. Configura username/password
sólo cuando el servidor los requiera y autorice esa modalidad.

Recrea la API conservando volúmenes. Configuración → Notificaciones muestra
configurado/no configurado, remitente y estado de entrega. Crear un usuario
envía una contraseña temporal de 24 horas. Si falla, el usuario se conserva y
la entrega muestra un código controlado. Una vez corregida la integración, usa
**Regenerar y reenviar credenciales**; esto crea otra contraseña e invalida la
anterior. No existe recuperación ni vista administrativa del plaintext.

`NONE` se limita a un entorno local/test explícito con
`TRACKVANCE_SMTP_ALLOW_INSECURE=true`. No se activa por omitir TLS. Cuando SMTP
está incompleto o deshabilitado, Trackvance inicia y crea usuarios con entrega
FAILED/NO_PROVIDER. No se incluyen body/password en notificaciones persistidas.

## Mailpit de pruebas

Los runners añaden `deploy/docker/compose.mailpit-test.yml` a un proyecto nuevo,
con SMTP interno 1025 y API/UI ligada a loopback. No añadas ese overlay al
Compose de la instalación habitual. El test obtiene la contraseña desde
`/api/v1/messages` y `/api/v1/message/{id}` únicamente en memoria para completar
primer acceso; no la publica en reports, logs o artifacts.
La [documentación de Mailpit](https://mailpit.axllent.org/docs/api-v1/) describe
su API y el [uso en Docker](https://mailpit.axllent.org/docs/install/docker/)
permite reproducir ese entorno desechable.

## Gmail y App Password

Cuando la cuenta y sus políticas lo permiten, activa 2-Step Verification y
genera una contraseña de aplicación específica para este SMTP. Guarda ese valor
en TRACKVANCE_SMTP_PASSWORD, con el correo completo como username. No uses la
contraseña normal de Google ni un secreto OAuth de SSO. La disponibilidad de App
Passwords depende del tipo de cuenta y sus restricciones; consulta la
[guía oficial](https://support.google.com/accounts/answer/185833?hl=en). Usa el
host/puerto/TLS de SMTP que Google documente para tu modalidad de cuenta.

## Microsoft SMTP

Este adaptador implementa SMTP con credenciales y TLS; no implementa SASL OAuth2.
Sólo puede usar un servidor/relay Microsoft cuya configuración autorice esa
modalidad. No se afirma compatibilidad general con SMTP AUTH de Microsoft 365:
las políticas del tenant, Security defaults y evolución de Basic Authentication
pueden impedirlo. No reduzcas la seguridad del tenant para cumplir la prueba.
Si exige OAuth, el caso queda NOT_RUN_UNSUPPORTED_SMTP_AUTH hasta incorporar un
adaptador apropiado. Consulta [SMTP AUTH en Exchange Online](https://learn.microsoft.com/en-us/Exchange/clients-and-mobile-in-exchange-online/authenticated-client-smtp-submission)
y [SMTP con OAuth](https://learn.microsoft.com/en-us/exchange/client-developer/legacy-protocols/how-to-authenticate-an-imap-pop-smtp-application-by-using-oauth).

## Operación y recuperación

La tabla conserva sólo destinatario, template, proveedor, intentos, resultado y
fechas; NOTIFICATION_SENT/FAILED aportan auditoría. SENT significa aceptación por
el servidor SMTP, no confirmación de lectura o entrega final en la bandeja.
Una interrupción entre envío y actualización local puede dejar PENDING: no
reenvíes el mismo secreto; genera credenciales nuevas explícitamente.

`.env` y passwords SMTP/client secrets SSO están fuera del backup. Conserva su
configuración en el mecanismo privado del operador y reintégrala tras restore.
Nunca incluyas esos valores ni correos con contraseñas en evidencia de CI.
En 0.6.0 sólo se envían credenciales USER; alertas de Runs, roles, dominios y
grupos permanecen pendientes aunque el puerto pueda reutilizarse.
