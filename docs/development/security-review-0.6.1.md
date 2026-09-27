# Revisión de credenciales 0.6.1

La frontera autorizada de divulgación es la respuesta inmediata de alta o
regeneración a un administrador con `users:manage`, sesión normal y CSRF.
`UserCredentialIssueResponse` separa `temporary_credentials` del `UserResponse`
normal. Los GET no recuperan la temporal; respuestas de emisión usan
`Cache-Control: no-store` y `Pragma: no-cache`.

## Generación, almacenamiento y errores

`secrets.token_urlsafe(24)` produce 32 caracteres a partir de 192 bits de
entropía. Sólo se persiste Argon2; expiración es 24 horas. Regenerar reemplaza
hash, incrementa versión, renueva expiración y revoca sesiones antes de responder.
La comprobación CAS y el bloqueo por organización permanecen. Primer acceso
local y SSO sólo autoriza `/me`, cambio de contraseña y logout; completar el
cambio rota sesión/CSRF e invalida la temporal.

La frontera de emisión convierte errores inesperados de hashing, persistencia
o construcción del DTO en `CREDENTIAL_ISSUE_FAILED` con texto fijo. No pasa el
texto de excepción al logger general. El primer cambio de contraseña usa la
misma frontera y un error fijo `PASSWORD_CHANGE_FAILED`: un fallo de hashing
o persistencia tampoco puede registrar la nueva contraseña o su hash. Los handlers controlados de integridad
y operación ya sanitizan sus respuestas. Las regresiones inyectan excepciones
que contienen secretos para comprobar esa frontera. Si un fallo ocurre después
del commit, la credencial pudo cambiar aunque el cliente no recibiera respuesta:
se debe consultar el usuario y regenerar con su versión actual.

Auditoría contiene eventos y metadata de identidad, nunca temporal/hash.
`notification_deliveries` permanece para lectura histórica; ninguna emisión
nueva crea una fila ni evento de entrega. El código SMTP y su configuración
estándar se retiraron. SSO conserva PKCE, state, nonce, JOSE y sujeto estable.

## Navegador

Alta/regeneración usan llamadas directas, fuera de React Query mutation cache.
El único estado que retiene la respuesta es el modal montado. Cerrar,
desmontar y `pagehide` eliminan el estado; respuestas tardías de pantallas
desmontadas no reabren el modal. Las pruebas de componente inspeccionan query
y mutation cache. No se escriben cookies, URL, localStorage ni sessionStorage.

Copiar al portapapeles es explícito. No se promete borrar historial del sistema,
contenido entregado externamente ni memoria mediante zeroization criptográfica:
JavaScript/Python administran memoria. Se eliminan referencias controladas por
la aplicación y se impide persistencia recuperable. El usuario debe regenerar
si cerró el modal o perdió la respuesta.

## Evidencia sin secretos

Los helpers E2E capturan las temporales sólo en RAM, introducen contraseñas sin
registrar el argumento en pasos `fill`, comparan booleanos y sanitizan errores.
Con `TV_E2E_PRIVATE_ARTIFACTS` se desactivan trace/screenshot y el contexto DOM
automático de Playwright. El teardown limpia el DOM antes de capturas del runner.
No se publican informes crudos que puedan contener respuestas o errores secretos.

`credential_leak_probe.py` recibe secretos únicamente por stdin. Rechaza el
proyecto principal y nombres fuera de los proyectos de prueba permitidos.
Busca coincidencias en pg_dump, logs completos de contenedores, todo ArtifactStore
y archivos de resultados del navegador; devuelve sólo estados y contadores.
La evidencia de navegador admite un conjunto fijo de claves, enums y números.
La prueba del probe fuerza fugas en cada superficie para verificar que falla
sin reproducir el valor en stdout/stderr.

Los drills de recovery inspeccionan además el dump decodificado y archivos tar
descomprimidos del backup, incluidos manifests, receipts y snapshots de estado.
Los hashes Argon2 y secretos SQL cifrados con claves incluidas por diseño no se
confunden con persistencia plaintext de temporales. Los backups se custodian
privadamente y sólo sus agregados saneados forman parte del repositorio.

## Alcance de validación

Resultados ejecutados, conteos, fallos iniciales y correcciones están en
[validation.md](validation.md) y [evidence/0.6.1](evidence/0.6.1/README.md).
Los cuatro perfiles OIDC mock certifican nuestro cliente; Microsoft/Google
reales conservan `NOT_RUN_EXTERNAL_CREDENTIALS` por alcance explícito.
SMTP no es un gate pendiente: dejó de ser una capacidad operativa.
