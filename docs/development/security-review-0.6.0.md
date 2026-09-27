# Revisión de seguridad de la evolución 0.6.0

Revisión de código, regresiones adversariales y proyectos locales desechables.
No constituye una prueba de penetración externa ni certificación de los IdP.

| Superficie | Control y comprobación |
| --- | --- |
| Privilegios | Catálogo cerrado41, matriz explícita104, ruta desconocida denegada; users:manage/roles:manage no delegables. Administrator por system_key. |
| Actualización de roles | Grants leídos por petición, dependencias transitivas, CAS, bloqueo por organización, último admin y roles asociados protegidos. |
| IDOR / organizaciones | Role de la misma org, User/ExternalIdentity/destino/config/run/attempt scope; módulos compartidos filtran read. Receipt exige delivery:read y artifacts:download. |
| CSRF / sesión | Cookie HttpOnly/SameSite, Secure con origen HTTPS; mutaciones con CSRF; origen validado al establecer sesión; rotación tras first-login y SSO, revocación por cambios sensibles. |
| OIDC | Librerías mantenidas Authlib/JOSE; Code Flow + PKCE S256, state ligado al navegador, nonce, firma, iss/aud/exp, consumo único, redirects internos controlados. |
| Vínculo externo | Preprovisioning activo, autoridad de email inicial, clave permanente provider/issuer/subject; sin roles/grupos externos ni auto-provisioning. |
| Temporal | CSPRNG32 caracteres/24h, Argon2, sólo memoria durante envío; primera sesión restringida y nueva contraseña distinta. Login inexistente/deshabilitado usa hash dummy y error genérico. |
| Correo | Un solo puerto de entrega, TLS y errores constantes; no body/password/SMTP exception en metadata o auditoría. FAILED conserva User, regenerar rota credencial. |
| Logs y bundles | Callback sin query en nginx, access log API excluido/controlado, secretos SSO/SMTP sólo API environment; no variables VITE sensibles. |
| Backup | Metadata y secretos SQL cifrados con sus claves, acceso restringido; .env y secretos SMTP/OAuth externos excluidos. Proyección legacy estricta. |
| Delivery | Username de User interno fijado al encolar, timestamp UTC por intento, mapping reservado, policy irreversible, DDL+DML transaccional y drift fail-closed. |

La revisión independiente encontró y corrigió cuatro fallos de frontera:

1. Un first-login que esperaba el lock podía completar después de revocar su
   sesión. Ahora vuelve a validar la sesión persistida, actividad y expiración.
2. Editar User con el mismo role_id tras renombrar el rol podía reactivar la
   etiqueta legacy como fuente de asignación. La identidad se conserva por ID.
3. El proxy HTTPS podía producir cookie sin Secure al observar HTTP interno.
   Se deriva también del origen público configurado.
4. Un primer enlace SSO que esperaba el lock podía usar un correo ya cambiado
   por el administrador. Ahora revalida el email bajo el lock antes de vincular.

Se añadieron regresiones permanentes para esos casos, firmas inválidas,
state/nonce/replay, revocación, aislamiento y compatibilidad de
issuer Google. El backend sigue siendo autoridad aunque se manipule la UI.

Límites declarados: sin rate limiting corporativo, MFA propio, aprobación de
cuatro ojos, SMTP OAuth2, gestores de secretos cloud ni control de escritores
externos del target. Microsoft/Google externos quedan NOT_RUN_EXTERNAL_CREDENTIALS.
