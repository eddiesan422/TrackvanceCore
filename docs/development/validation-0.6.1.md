# Validación de Trackvance Core 0.6.1

Baseline real `587909bc4462683e87e403dd2ea29a1d6d4afe08`, versión 0.6.0, rama
`feat/local-prototype`; se verificaron local y remoto antes de editar y antes de
publicar. Implementación `4d3c656ab0bc250f50eb53972d22839da9e4485c`. No existían cambios ajenos que sobrescribir.
La instalación principal parte realmente de 0.6.0/0012/state 5.

Estos resultados son ejecuciones nuevas de 0.6.1. Los escenarios de navegador se
deduplican y los opt-in se prueban en sus proyectos dedicados. Los antecedentes
se conservan en [validation-0.6.0.md](validation-0.6.0.md), sin convertirlos en
certificación actual. [Matriz de 22 criterios](acceptance-0.6.1.md),
[revisión de seguridad](security-review-0.6.1.md), [permisos](permission-matrix.md)
y [evidencia](evidence/0.6.1/README.md) amplían los resultados.

| Gate 0.6.1 | Resultado observado |
| --- | --- |
| Corte y procedencia | 27 septiembre 2026; feat/local-prototype. Baseline 587909bc4462683e87e403dd2ea29a1d6d4afe08, sin commits remotos posteriores al iniciar. Implementación 4d3c656ab0bc250f50eb53972d22839da9e4485c. Ejecuciones nuevas 0.6.1; no se heredan éxitos 0.6.0. |
| Backend local | 1.197 pytest backend/scripts PASS en 95,850 s tras corregir el runner; 0 FAIL y dos avisos upstream conocidos. Incluye cinco regresiones nuevas de diagnóstico seguro; 70 focales del harness PASS. Antes: 1.192 PASS y 54 focales identidad/SSO PASS, conservados como ejecución inicial. Ruff PASS, Mypy 42 archivos PASS y uv sync --frozen PASS. Overlays SQL de prueba validados con Compose y healthcheck TCP. |
| Frontend local | 174 Vitest / 20 archivos PASS en 13,056 s; 0 FAIL/0 SKIP. Instalación pnpm congelada, ESLint, TypeScript y production build PASS. Modal, cierre/pagehide, respuesta tardía y query/mutation cache comprobados. |
| OpenAPI y permisos | 0.6.1: 96 paths, 117 operaciones, 89 schemas; snapshot exacto probado. 41 permisos y 105 reglas protegidas. 31 tablas, head 0012_delivery_target_audit, cero nuevas migraciones. Modelos persistentes y 0001..0012 intactos; hashes de bytes antes/después coinciden. |
| Migraciones PostgreSQL | PostgreSQL 16 real: upgrade/check/downgrade/upgrade, paridad ORM y preservación histórica PASS. Roundtrip 0008↔0012 conserva 24 tablas y ocho vínculos Delivery, con COMMITTED/UNKNOWN. Contenedor desechable eliminado. |
| Compose integral | PASS: smoke API 84; Playwright 26 PASS / 15 opt-in SKIP / 0 FAIL en 125,025 s. Doctor, migraciones, restart=no, snapshot exacto tras restart y cleanup PASS. El tercer intento corrigió sincronización del test y CRLF del clipboard Windows; ambos fallos anteriores están registrados. |
| Identity/SSO | 9/9 Playwright PASS en 45,173 s; ciclo 104,251 s. Temporal/primer acceso/regeneración/revocación, roles y cuatro perfiles OIDC mock RS256. Expiración, cero entregas nuevas, privacidad y restart PASS. No Mailpit. |
| Privacidad ejecutada | Alta y regeneración nunca entran en mutation cache. Copiar3, cerrar y ausencia en DOM/storage/cookies/URL PASS. Probe stdin: 14 secretos RAM en Compose y 15 en identidad, DB/log/artifacts/archivos de navegador PASS. Backups decodificados nativo/060/051 y excepciones inyectadas PASS. Sin screenshots, trace, vídeo ni contexto DOM sensible publicado. |
| Conexiones reales | 96 comprobaciones PostgreSQL/SQL Server y smoke 84 PASS. Playwright 30 PASS / 11 opt-in SKIP / 0 FAIL en 166,452 s. Tipos temporales, adquisición/refresh/historia y módulos; secretos y persistencia tras reinicio PASS. |
| Delivery real | 308 comprobaciones PostgreSQL 16/18 y SQL Server PASS; Playwright 1/1 en 5,189 s. fechaIngesta/usuario, policies, CREATE/APPEND/OVERWRITE/UPSERT, drift, receipt/manifest/linaje y actor interno local/SSO conservados; reinicio y scans PASS. |
| UNKNOWN y límites | Pérdida de acknowledgement inyectada en el adaptador después de commit SQL real PASS, con policy required y sin replay. Fallo físico de red en ventana exacta: NOT_RUN_NONDETERMINISTIC. No se equiparan esas pruebas. |
| Demo limpia y cobertura | Demo sin seed 1/1 PASS en 1,878 s; ciclo 61,566 s, restart y cleanup PASS. 41 escenarios distintos cubiertos: 26 comunes + 4 conexiones + 9 identidad + 1 Delivery + 1 demo limpio. Repeticiones no se suman; SKIP opt-in cubiertos por suites dedicadas. |
| Recovery nativo 0.6.1 | PASS: 9 artifacts, dos secretos SQL, 269 relaciones, 6 roles/95 grants y tablas operativas pobladas. Notificación histórica declarada como fixture sintética; alta/regeneración no añaden filas. Origen destruido antes de restore. Dump decodificado y 15 archivos examinados contra seis secretos conocidos, sin fugas. Ciclo completo repetido después del ajuste TCP: PASS, con preservación, scan y limpieza. |
| Restore auténtico 0.6.0 | PASS desde 587909b/0012/state 5: 78 artifacts y estado histórico exacto; una entrega FAILED/NO_PROVIDER real de la baseline preservada. Nuevas emisiones 0.6.1 sin entregas adicionales. Backup decodificado (82 archivos), logs y cleanup PASS; 143,074 s. Dos fallos iniciales del harness se corrigieron y documentaron. |
| Restore auténtico 0.5.1 | PASS desde 4519ed3/0009/state4: 78 artifacts, proyección histórica SHA-256 idéntica, migración a0012/state 5, cinco roles y usuario heredado. Emisión/regeneración y backup sin plaintext ni nuevas notificaciones; cleanup PASS, 136,233 s. Drills041/050 permanecen antecedentes060, no se atribuyen como nuevas ejecuciones061. |
| Benchmarks acotados | General file-only PASS: 1,074,923 bytes / 1,000 filas / cuatro columnas, 38,509 s, pico muestreado 391,066,417 bytes. Delivery smoke 8/8 PASS, mismo input, 140,991 s, versión 0.6.1. Sin OOM ni certificación de capacidad o percentiles. |
| Proveedores externos | Microsoft personal/Entra y Google Gmail/Workspace: NOT_RUN_EXTERNAL_CREDENTIALS por alcance explícito. SSO implementado, opcional y disabled by default. SMTP fue retirado: no es un proveedor pendiente de certificar ni una dependencia. Canal de futuras notificaciones sin decidir. |
| Instalación Docker real | PASS: trackvance-certification en http://localhost:3100; contenedores existentes 0.6.0 iniciados para backup verificado y upgrade 0.6.1 sin borrar volúmenes. State 5 exacto antes/después y tras reinicio; cinco servicios healthy, API/ambos workers 0.6.1, restart=no, doctor y Alembic 0012. UI/health/footer 0.6.1, Usuarios/Roles/Autenticación sin Notificaciones, local habilitado y Microsoft/Google disabled. Conservados 3 datasets, 2 usuarios, una conexión, un destino, cuatro Runs, nueve artifacts y dos secretos SQL. Acceso demo existente comprobado en principal; login con contraseña local probado en desechables. Sin mutaciones de negocio para validar. |
| GitHub Actions producto | SUCCESS: nueve jobs, commit 4d3c656ab0bc250f50eb53972d22839da9e4485c, workflow 36327154050, intento 1. Backend, frontend, compose-e2e, connections-e2e, delivery-e2e, identity-sso-e2e, backup-restore-e2e, benchmark-smoke y delivery-benchmark-smoke. https://github.com/eddiesan422/TrackvanceCore/actions/runs/36327154050 |
| GitHub Actions publicación | Primer cierre afb8f8d, workflow 36328842735: 8/9 SUCCESS; falló preparar PostgreSQL externo antes de iniciar Trackvance. Causa exacta no recuperable; condición prematura de readiness reproducida y corregida con TCP. Nativo y pytest repetidos PASS. El informe externo sólo cierra cuando los nueve jobs del HEAD corregido terminen SUCCESS. |
| Publicación PDF | Candidato renderizado e inspeccionado por completo antes de --publish. Páginas, SHA-256, hash de fuente/OpenAPI/validación, source commit y revisión se registran en pdf-verification.json. Copia oficial sincronizada en Documentación; edición060 archivada intacta. |

## Fallos encontrados y correcciones

- El primer CI de publicación, `afb8f8d` / workflow `36328842735`, terminó con
  ocho jobs SUCCESS y `backup-restore-e2e` FAILURE. Falló `psql` en la preparación
  del PostgreSQL externo, antes de iniciar Trackvance o ejecutar backup/restore.
  El diagnóstico original suprimió stderr; su causa exacta no se puede recuperar.
  Una reproducción aislada demostró que el healthcheck por socket aceptaba el
  servidor temporal de initdb y que una consulta podía ser interrumpida al
  detenerlo. El healthcheck TCP espera al servidor definitivo. Se añadió
  diagnóstico de exit code y categoría cerrada, sin SQL/stdout/stderr, y regresiones
  con secretos sintéticos. El [intento fallido](evidence/0.6.1/ci-publication-attempt1.json)
  y la [reproducción controlada](evidence/0.6.1/native-recovery/readiness-reproduction.json)
  conservan hechos y límites; no se atribuye retrospectivamente una causa no observada.
- La primera pasada focal de backend requirió ajustar fixtures heredados al
  envelope nuevo; no se relajaron permisos ni reglas de negocio. La revisión
  reprodujo una excepción de hashing del primer acceso que podía llevar texto
  secreto al logger. Alta/regeneración y primer cambio ahora usan fronteras de
  error fijo; las regresiones inyectadas comprueban plaintext/hash ausentes.
- Compose inicial: 25 PASS / 1 FAIL / 15 SKIP; el modal de edición siguió abierto tras
  cambiar la contraseña desde otro contexto. No se conservó la respuesta HTTP de
  ese fallo, por lo que no se afirma haber observado 409. El test refresca estado
  y comprueba PATCH 200. Segundo intento: 25 PASS / 1 FAIL; una prueba sintética aisló
  la normalización LF→CRLF del clipboard Windows. Se normaliza sólo el texto
  compuesto; username y password se comparan exactamente. Tercero: 26 PASS.
- Restore 0.6.0 inicial falló por una ruta relativa resuelta desde el checkout
  histórico, antes de crear contenedores. El segundo conservó state 5 exactamente
  y falló al probar alta porque restore deshabilita demo access deliberadamente.
  Se usa ruta absoluta y se habilita demo únicamente después de comparar el
  destino desechable, sin seed. El tercer ciclo completo pasó. Todos limpiaron
  sus recursos propios; resultados iniciales saneados permanecen publicados.
- Se conservan los avisos upstream Starlette/httpx y AnyIO BlockingPortal.
  Git informa la conversión LF→CRLF del checkout Windows; no se modificaron las
  migraciones: hashes de bytes antes/después idénticos y diff Git vacío.

## Límites y evidencia sensible

Microsoft/Google reales no se ejecutan por alcance explícito; el mock RS256
certifica nuestro cliente OIDC. La reproducción física exacta de una pérdida
de red no determinista permanece NOT_RUN_NONDETERMINISTIC. Los benchmarks son
smoke de 1.000 filas, no certifican 100/500 MiB, GiB, percentiles o capacidad productiva.
El drill recovery nativo comprueba UI HTTP 200 y declara navegador
NOT_RUN_IN_THIS_DRILL; el navegador se prueba en las suites dedicadas.

Los informes no publican contraseñas, dumps, backups, configuración privada,
screenshots de temporales, traces ni error-context con DOM. Los valores de
prueba se mantienen en RAM y los escáneres reciben secretos por stdin. Copiar
al clipboard es una acción explícita del administrador y su historial externo
queda fuera del almacenamiento de Trackvance. Se elimina retención controlada
por la aplicación; no se promete zeroization forense de JavaScript/Python.

La validación de la instalación principal usa navegación de lectura; alta y
regeneración se prueban en entornos desechables. Las auditorías/sesiones de
login legítimas se producen después de comparar el estado persistente del
upgrade y reinicio. El informe final externo registra el CI del HEAD documental.
