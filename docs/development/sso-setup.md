# Prueba manual de Microsoft y Google SSO

Estas instrucciones permiten ejecutar pruebas externas opt-in. El mock de CI
certifica el cliente OIDC de Trackvance; no certifica cuentas ni servicios reales.
Mientras no se hayan configurado credenciales y ejecutado cada caso, el resultado
es `NOT_RUN_EXTERNAL_CREDENTIALS`. Nunca escribas secretos en una captura, ticket,
commit, comando compartido o archivo de evidencia.

## Preparación común

1. Confirma el origen público real de la instalación. En el equipo revisado es
   `http://localhost:3100`; otro entorno debe usar su propia URL. Establece
   `TRACKVANCE_PUBLIC_URL` y `TRACKVANCE_WEB_ORIGIN` coherentes en el `.env` privado.
   En HTTPS las cookies deben viajar por HTTPS. No uses URLs con credenciales,
   query strings ni fragmentos como URL pública.
2. En Configuración → Usuarios crea primero la cuenta con el correo exacto de la
   identidad que vas a probar y un rol de Trackvance. No existe auto-provisioning.
   No es necesario que SMTP esté configurado para autenticar por SSO, pero el
   primer acceso seguirá exigiendo definir una contraseña local.
3. Registra una aplicación Web con callback exacto para cada proveedor. El secreto
   permanece sólo en la API, no en React ni en los workers. No habilites implicit
   grant ni scopes de correo/calendario/Graph.
4. Configura las variables descritas abajo y recrea la API con Compose. Consulta
   Configuración → Autenticación o `/api/v1/auth/providers`: sólo debe aparecer
   un proveedor que esté habilitado y completo. Readiness no prueba Internet.
5. Utiliza una ventana privada para aislar las cuentas del proveedor. No mezcles
   `localhost` y `127.0.0.1` durante un mismo flujo: state está ligado al navegador.

## A. Cuenta personal Microsoft: Outlook, Hotmail, Live

1. Abre el [centro de administración de Microsoft Entra](https://entra.microsoft.com/),
   entra en App registrations y registra una aplicación para Trackvance.
2. En Supported account types selecciona **Accounts in any organizational
   directory and personal Microsoft accounts**. La audiencia correspondiente es
   `AzureADandPersonalMicrosoftAccount`. Una aplicación sólo de un tenant o sólo
   organizacional no cubre esta prueba.
3. Añade plataforma **Web**. Para la instalación de ejemplo registra
   `http://localhost:3100/api/v1/auth/sso/microsoft/callback`. No añadas parámetros
   de consulta a la URI personal. No registres el callback como SPA.
4. Copia Application (client) ID. En Certificates & secrets crea un client secret
   con vigencia administrada y copia su **valor**, no su ID, al `.env` privado.
5. Configura `TRACKVANCE_SSO_MICROSOFT_ENABLED=true`,
   `TRACKVANCE_SSO_MICROSOFT_CLIENT_ID` y
   `TRACKVANCE_SSO_MICROSOFT_CLIENT_SECRET`. Trackvance usa la autoridad `common`
   para Code Flow + PKCE; no pide Microsoft Graph.
6. Preprovisiona el correo personal que el proveedor devuelve. Pulsa **Continuar
   con Microsoft** e inicia sesión con esa cuenta. El primer enlace personal
   requiere el tenant consumer de Microsoft y un dominio personal reconocido
   (Outlook/Hotmail/Live/MSN); otras identidades ambiguas se rechazan.
7. Si es primer acceso, define una contraseña local nueva. Confirma el mismo
   username/rol interno en `/me`, luego consulta Métodos de acceso en Usuarios:
   Microsoft vinculado y último acceso. La fecha de enlace está disponible como
   `external_identities[].linked_at` en el DTO del usuario.

La configuración de registro y audiencia sigue la [guía oficial de registro de
aplicaciones](https://learn.microsoft.com/en-us/entra/identity-platform/quickstart-register-app).

## B. Microsoft Entra work/school

Usa la misma aplicación con audiencia organizacional + personal y callback Web.
Preprovisiona el correo de la cuenta organizacional. El primer vínculo exige
prueba de dominio verificado (`xms_edov`) o que el tenant UUID figure explícitamente
en `TRACKVANCE_SSO_MICROSOFT_TRUSTED_TENANTS`, separado por comas. Declara únicamente
tenants cuyo gobierno de identidades controlas o confías: permitir un tenant
significa confiar en sus afirmaciones de identidad para este primer enlace.
No uses comodines ni añadas un tenant desconocido para hacer pasar un test.

Pulsa Microsoft, selecciona la cuenta corporativa y completa los requisitos del
tenant (consentimiento, MFA o políticas de acceso). Trackvance no administra esas
políticas ni toma roles/grupos de ellas. Confirma User, username y permisos internos.
El issuer validado corresponde al tenant concreto, aunque discovery inicial use
common; la firma debe provenir de una clave permitida para ese issuer. Consulta
las [reglas de validación de claims de Microsoft](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation).

## C. Gmail personal

1. En [Google Cloud Console](https://console.cloud.google.com/) selecciona un
   proyecto de integración propio. Configura Google Auth Platform/consent screen:
   nombre, contacto de soporte y audiencia apropiada. Si el proyecto está en
   modo de pruebas externo, añade la cuenta Gmail a los test users.
2. Crea un OAuth client de tipo **Web application**. Añade como Authorized redirect
   URI `http://localhost:3100/api/v1/auth/sso/google/callback`, sustituyendo origen
   por el real. Esta integración backend no necesita Gmail API.
3. Guarda client ID y client secret en el `.env` privado mediante
   `TRACKVANCE_SSO_GOOGLE_CLIENT_ID` y `TRACKVANCE_SSO_GOOGLE_CLIENT_SECRET`;
   establece `TRACKVANCE_SSO_GOOGLE_ENABLED=true`.
4. Preprovisiona el Gmail exacto en Trackvance. Pulsa **Continuar con Google**,
   selecciona esa cuenta y concede sólo openid/email/profile. Define contraseña
   local si la sesión es de primer acceso. Confirma Google vinculado y rol local.

El cliente Web y la redirect URI se documentan en el [flujo OAuth para aplicaciones
Web](https://developers.google.com/identity/protocols/oauth2/web-server). La
[referencia OIDC](https://developers.google.com/identity/openid-connect/openid-connect)
describe issuer/sub y los claims de email.

## D. Google Workspace

Reutiliza un cliente Web cuya audiencia/consentimiento permita ese dominio. Una
aplicación de audiencia Internal puede limitarse a su propia organización; para
probar también Gmail debe elegirse una configuración que admita ambas cuentas.
El administrador Workspace puede necesitar aprobar el cliente.

Preprovisiona el email corporativo y autentica desde Google. El enlace inicial
exige `email_verified=true` y `hd` compatible con el dominio del email. Una
Google Account creada con un correo externo, sin Gmail ni `hd`, no se vincula
automáticamente: email_verified por sí solo no garantiza que Google siga siendo
autoridad sobre ese correo. La razón está en la [guía de autenticación backend
de Google](https://developers.google.com/identity/sign-in/android/backend-auth).

## Verificación y desvinculación

Después de cada prueba consulta el usuario y los eventos SSO_LINKED y
SSO_LOGIN_SUCCESS. Compara el user_id interno antes/después; no debe crearse un
User adicional. Un segundo login debe usar la misma ExternalIdentity. Cambiar
permisos del Role afecta la próxima petición; cambiar el Role revoca sesión.
Desactivar o eliminar lógicamente User impide tanto login local como SSO.

Un Administrator abre Configuración → Usuarios → Métodos de acceso y desvincula
Microsoft o Google. Esto elimina únicamente el vínculo de autenticación, conserva
User e historia y revoca las sesiones afectadas según el contrato. Para volver a
enlazar se aplica de nuevo la validación inicial; no se heredan grupos externos.

Guarda por caso una evidencia saneada: proveedor, tipo de cuenta, hora, commit,
resultado, user_id de fixture, permisos comprobados y error genérico si falló.
No guardes callback completo, código, tokens, cookies, ID token ni capturas de
secretos. En `real-provider-results.json` distingue PASS, FAIL y NOT_RUN con motivo.

## Errores frecuentes

| Síntoma | Comprobación |
| --- | --- |
| No aparece botón | Enabled=true, client ID y secret presentes, URL pública válida; recrear API. |
| redirect_uri_mismatch | Esquema, host, puerto y path exactos; cliente Web, no SPA. |
| Cuenta personal Microsoft rechazada | Audiencia AzureADandPersonalMicrosoftAccount, autoridad common y correo personal permitido. |
| Cuenta organizacional sin vínculo | Correo preprovisionado y autoridad del dominio; xms_edov o tenant UUID explícitamente confiable. |
| Google external email rechazado | Falta autoridad Gmail/Workspace; no saltar la validación. |
| State o nonce inválido | Flujo vencido/reutilizado, cookies ausentes o mezcla de hosts; iniciar un flujo nuevo. |
| Usuario no habilitado | Cuenta inexistente o desactivada/eliminada; no se auto-crea. Un rol inactivo no concede permisos de negocio. |
| Sólo aparece cambio de contraseña | Es primer acceso obligatorio, incluso por SSO. |
| Error de red o firma | Discovery/JWKS accesibles, reloj del host correcto, secret vigente; no desactivar validación. |

La prueba real depende de credenciales y consentimiento externos. La suite
`identity-sso-e2e` usa un mock desechable y no requiere esas credenciales.
