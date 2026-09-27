# ADR 0017 — SSO OIDC con usuarios pre-provisionados

Fecha: 2026-09-27. Estado: implementado en 0.6.0.

## Frontera de confianza

SSO autentica; Trackvance autoriza mediante sus roles persistentes. Microsoft/Google no crean usuarios, asignan roles ni importan grupos. El administrador debe crear la cuenta antes del primer vínculo. `ExternalIdentity` identifica `provider + issuer + subject`, globalmente único; cada usuario tiene como máximo un vínculo por proveedor. Correo al vincular y fechas son snapshots informativos. Después de enlazar se usa la clave estable, aunque el proveedor cambie el correo.

No se guardan ID tokens, access tokens ni refresh tokens. Client IDs/secrets provienen del entorno del API; no entran en DB funcional, backups de datos, OpenAPI o bundles. El proveedor puede estar deshabilitado o incompleto sin impedir inicio/readiness; `/auth/providers` informa únicamente proveedores listos y estados sanitizados. Readiness no consulta Internet.

## Protocolo

`GET /auth/sso/{microsoft|google}/start` obtiene discovery oficial y utiliza Authlib OAuth2Client para Authorization Code + PKCE S256. Solicita únicamente `openid email profile`, junto con state y nonce criptográficos. La redirect URI deriva de `TRACKVANCE_PUBLIC_URL` (fallback WEB_ORIGIN), exige HTTPS salvo loopback local y no contiene query string. No hay redirect final controlado por el cliente: sólo `/` o `/login?sso_error=SSO_LOGIN_FAILED`.

El servidor guarda un intento efímero con hash de state, hash del vínculo al navegador, nonce, verifier, expiración de diez minutos y marca de consumo. Una cookie HttpOnly/SameSite=Lax liga el intento al mismo navegador. Callback comprueba proveedor, expiración, state y cookie; consume mediante compare-and-swap antes del canje, borrando verifier/nonce. Reintentos y callbacks concurrentes no pueden reutilizarlo.

Authlib realiza canje del código; joserfc verifica JOSE, firma RS256 y JWK. `CodeIDToken` de Authlib valida issuer, audience, exp, iat, sub, nonce, authorized party y at_hash cuando existe. No se implementa un decodificador/verificador JWT propio. El token nunca se entrega al navegador: una autenticación válida termina con la sesión HttpOnly/CSRF normal de Trackvance, rotando una cookie anterior. Las cookies se marcan Secure cuando la URL pública configurada es HTTPS, incluso tras un proxy TLS.

Los errores OAuth/JWT y SMTP pueden contener secretos; la frontera captura y devuelve únicamente códigos genéricos. Auditoría no recibe excepciones raw, código, verifier ni tokens. Los access logs omiten queries de callbacks; el filtro del API cubre también un lanzamiento directo de uvicorn.

## Microsoft personal y organizacional

La aplicación usa discovery de `common` y una App Registration con `AzureADandPersonalMicrosoftAccount`: organizaciones Entra y cuentas Microsoft personales. Se valida `tid` como UUID, issuer `https://login.microsoftonline.com/{tid}/v2.0` y el issuer del JWK cuando Microsoft lo publica. Se admiten las claves con issuer derivado de tenant documentado, sin desactivar la verificación de emisores.

El primer vínculo no confía indiscriminadamente en email/preferred_username de cualquier tenant. Las cuentas personales del tenant consumidor `9188040d-6c67-4c5b-b112-36a304b66dad` pueden enlazarse por direcciones Outlook.com/Hotmail.com/Live.com/MSN.com. Las organizacionales necesitan autoridad de dominio `xms_edov=true` o un tenant que el operador haya permitido explícitamente en `TRACKVANCE_SSO_MICROSOFT_TRUSTED_TENANTS`. La lista de confianza es decisión administrativa, no una claim aceptada del usuario. Los casos sin evidencia suficiente fallan cerrado; esto protege contra un tenant ajeno que emita el correo de una cuenta pre-provisionada.

El correo pre-provisionado se vuelve a verificar bajo lock antes de crear el vínculo, impidiendo que un cambio administrativo concurrente conserve elegibilidad de un correo antiguo.

## Google Gmail y Workspace

Se usa discovery oficial Google y `issuer + sub`. Se aceptan los dos emisores oficiales (`accounts.google.com` y `https://accounts.google.com`) y se normaliza la identidad persistida al segundo. Primer vínculo requiere `email_verified=true` y Gmail personal o `hd` igual al dominio del correo para Workspace. Una Google Account con correo externo no Gmail/no Workspace no se enlaza automáticamente, aunque email_verified sea verdadero.

## Primer acceso, baja y pruebas

Una cuenta con contraseña temporal obtiene tras SSO sólo una sesión de primer acceso. Debe definir una contraseña local nueva; completar el flujo invalida la temporal y rota sesiones. Cuentas desactivadas o eliminadas no pueden entrar por ningún método. Cambiar rol aplica RBAC vigente y revoca sesiones. El administrador puede eliminar el vínculo externo sin borrar User; se revocan sesiones y se audita SSO_UNLINKED.

Los tests unitarios usan claves RSA y el cliente OAuth real con transporte controlado para state, cookie, PKCE, nonce, firma, issuer, audience, expiración, subject, replay, cuentas inexistentes/bajas, primer acceso y cambios de email. El overlay `compose.identity-test.yml` incorpora proveedor OIDC y Mailpit desechables; sólo `TRACKVANCE_SSO_TEST_MODE=true` permite discovery alternativo/HTTP de laboratorio. Esta opción debe permanecer ausente o false en instalaciones reales.

Los E2E mock certifican el código Trackvance, no la configuración comercial de Google/Microsoft. Sin credenciales externas reales el resultado es `NOT_RUN_EXTERNAL_CREDENTIALS`, jamás PASS. La guía operativa documenta registros de aplicación y pruebas opt-in por proveedor. No se implementan Graph/Gmail scopes, group→role, refresh tokens ni directorios externos.

Referencias: [Authlib OAuth2 client](https://docs.authlib.org/en/latest/client/oauth2.html), [Microsoft OIDC](https://learn.microsoft.com/en-us/entra/identity-platform/v2-protocols-oidc), [validación Microsoft](https://learn.microsoft.com/en-us/entra/identity-platform/claims-validation), [Google OIDC](https://developers.google.com/identity/openid-connect/openid-connect).
