# CI de Trackvance Core 0.8.0

El workflow `Trackvance CI` valida el SHA fuente exacto del push o de la rama del
pull request. El modo rápido sirve para desarrollo. La certificación que permite
actualizar una instalación exige el modo completo, su manifiesto íntegro, el gate
final y todos los jobs aplicables terminados con éxito sobre ese mismo SHA.
Un resultado de otra revisión, un job omitido o un escenario incompleto mantiene
abierta la certificación. Los fallos y reintentos anteriores se conservan.

## Selección y ejecución

En Actions, `Run workflow` permite escoger la rama, `mode=full` y
`cache_mode=cold|warm`. El valor inicial del modo manual es `full`. Un commit con
`[ci full]` solicita el recorrido completo; `[ci cold]` deshabilita la restauración
de cachés en el primer intento del push. La selección escrita en
`selection.json` registra SHA, motivo, paths, grupos y hash del manifiesto.

`auto` selecciona `fast` sólo para cambios documentales acotados reconocidos.
Código, tests, harnesses, workflow, Docker, dependencias, migraciones, contratos,
documentación operativa/de arquitectura y cualquier diff o ruta incierta fuerzan
`full`. Solicitar `fast` no elude esas reglas. Los tres grupos rápidos son
`backend`, `frontend` y `compose-critical`: ejecutan las comprobaciones existentes
de código/contratos/migraciones/aislamiento y los recorridos reales Compose de
demo vacío y persistencia con navegador. No son una certificación de volumen,
SQL multifuente o recuperación de la versión final.

El push ordinario se omite cuando existe un PR abierto con el mismo SHA que ya
recibirá su validación; el despacho manual y `[ci full]` conservan su ejecución
explícita. Las validaciones de desarrollo obsoletas pueden cancelarse por rama.
Las completas utilizan una identidad por run y no se cancelan por un push nuevo.
La matriz admite hasta 16 grupos en paralelo, con un stack pesado por runner y
`fail-fast: false`; mantiene los límites de producto y de cada perfil Docker.
La capacidad efectiva depende de la cuota y de los runners disponibles; no
garantiza una duración de cierre. Las ejecuciones full previas siguen activas.

## Manifiesto completo

`scripts/ci/scenarios.json` declara **19 grupos y 64 escenarios obligatorios**.
Los 16 jobs funcionales históricos quedan cubiertos; XLSX se divide en cuatro
grupos, con prerrequisitos propios, sin reutilizar bases, fixtures ni resultados
PASS de otro job. `select`, `images` y `gate` son trabajos de coordinación y no
aumentan ese conteo de grupos funcionales.

| Grupo | Escenarios | Cobertura obligatoria |
| --- | ---: | --- |
| backend | 6 | Tests backend/scripts, Ruff, mypy, contratos, roundtrip PostgreSQL, probe Linux confinado. |
| frontend | 4 | Lint, tipos, tests de componentes y build. |
| compose-critical | 2 | Demo vacío y recorrido real con persistencia después del reinicio. |
| corrections-acquisition | 9 | Ocho adquisiciones 100.000/100.001/400.000/1.000.000 inline/shared y rechazo de 1.000.001 preservando la versión anterior. |
| corrections-browser | 2 | Navegadores reales de 400.000 y 1.000.000 shared; cadena y oráculos completos de fuente, salida y destino SQL. |
| corrections-dispatch | 3 | Cadena de un millón inline, despacho de dos datasets de un millón con worker ocupado y no leída tras reinicio API. |
| corrections-recovery | 3 | Cancelación durante lectura, crash/lease de un millón shared y backup/restore nativo con diagnóstico y no leída. |
| identity-sso | 1 | RBAC, identidad y OIDC sintético firmado/PKCE en navegador. |
| connections | 1 | Motores externos reales del fixture, permisos y navegador. |
| delivery | 1 | Estrategias, auditoría, permisos y navegador. |
| backup-restore | 4 | Nativo 0.8.0 y fuentes auténticas 0.5.1, 0.6.0 y 0.6.1. |
| catalog-reports | 8 | Tres fuentes de 120/400.000/1.000.000, navegador, snapshot conjunto PostgreSQL, HTTP efímero y recuperación nativa/auténtica 0.7.0. |
| benchmark-smoke | 1 | Smoke real con mediciones y cleanup; no acredita el benchmark de capacidad. |
| delivery-benchmark-smoke | 1 | Ocho casos smoke de Delivery. |
| spark-local | 2 | Paridad real y tres módulos con un millón en modo LOCAL. |
| spark-standalone | 2 | Paridad real y tres módulos con un millón en modo STANDALONE_CLIENT. |
| async-volume-100 | 6 | CSV/JSONL/Parquet de un millón, automatización/negativos, navegador con integridad y recovery completo. |
| async-volume-500 | 4 | CSV/JSONL/Parquet de un millón y automatización/negativos. |
| async-volume-1024 | 4 | CSV/JSONL/Parquet de un millón y automatización/negativos. |

Las comprobaciones de población usan fixtures sintéticas con oráculos
independientes sobre todas las filas y valores, perfiles y numeración; un conteo
o una muestra no los sustituye. Los grupos de dispatch/recovery vuelven a crear
los datos que necesitan. Sus receipts de prerrequisitos no cuentan como escenarios
obligatorios de adquisición y no pueden reemplazarlos.

## Selección exacta de navegador

Cuando `TRACKVANCE_CI_IMAGE_MANIFEST` tiene un valor y el harness recibe una lista
de argumentos vacía, enumera todos los archivos reales `tests-e2e/**/*.spec.ts`.
Incluye los specs nuevos, incluso en subdirectorios. Sólo excluye los ocho opt-ins
de la tabla cuando su flag no vale exactamente `true`; sus fixtures y pruebas
se ejecutan en el grupo dedicado indicado. Los paths son relativos a `frontend`.

| Spec opt-in | Flag requerido | Grupo obligatorio |
| --- | --- | --- |
| tests-e2e/automation.spec.ts | TV_AUTOMATION_E2E | async-volume-100 |
| tests-e2e/volume.spec.ts | TV_VOLUME_E2E | async-volume-100 |
| tests-e2e/corrections-volume.spec.ts | TV_CORRECTIONS_E2E | corrections-browser |
| tests-e2e/connections.spec.ts | TV_CONNECTIONS_E2E | connections |
| tests-e2e/roadmap-source-cycle.spec.ts | TV_CONNECTIONS_E2E | connections |
| tests-e2e/delivery.spec.ts | TV_DELIVERY_E2E | delivery |
| tests-e2e/identity-sso.spec.ts | TV_IDENTITY_SSO_E2E | identity-sso |
| tests-e2e/demo-access-clean.spec.ts | TV_EXPECT_CLEAN_DEMO | compose-critical, escenario compose-clean-demo |

El summary guarda `selected_spec_files` y `excluded_opt_in_specs`, con archivo,
flag requerido y grupo de cobertura; no guarda valores del entorno. El gate
contrasta ese inventario exacto con el manifiesto. Un SKIP inesperado o una
selección vacía falla; no se oculta una prueba nueva ni se convierte un skip en
PASS. Los argumentos explícitos conservan su selección y, fuera de esa ruta CI,
la invocación sin argumentos mantiene el comportamiento habitual.

## Imágenes y cachés

El job `images` construye backend y web una vez por SHA, en Linux amd64, con
locks y etiqueta OCI `org.opencontainers.image.revision`. Exporta las imágenes,
un manifiesto con sus IDs `sha256`, tamaño y SHA-256 de los tar, y la prueba
`frontend-build-proof.zip` del mismo SHA.
Cada suite verifica todos los archivos antes de cargar, inspecciona la revisión y
el ID cargado y usa los IDs inmutables en su Compose privado. Los tags locales son
alias de transporte; no demuestran por sí solos la revisión ejecutada.
`TRACKVANCE_CI_IMAGE_MANIFEST` activa esa ruta explícita y su preflight de aislamiento.
Las fuentes legacy auténticas se siguen construyendo desde su commit propio.
El artifact de imágenes usa `compression-level: 1`; la compresión exterior no
cambia los tar ni sus hashes. Sus bytes y tiempos de compresión, descarga y
descompresión deben medirse: este nivel no acredita por sí solo un ahorro, y la
carga Docker sigue procesando el contenido original completo.

La prueba del frontend procede del target `build` del mismo Dockerfile web,
aprovechando las capas BuildKit ya construidas. El ZIP incluye únicamente
`source/`, con los archivos de frontend versionados y verificados byte a byte
contra Git, y `dist/`, con el resultado real del build; `node_modules` permanece
en la imagen de construcción. Su descriptor registra SHA fuente, ID de esa
imagen, tamaño y SHA-256 del ZIP. Se limitan a 20.000 archivos, 512 MiB sin
comprimir y 128 MiB de ZIP. La importación para promoción valida paths e
inventario, rechaza enlaces y archivos ambiguos, compara los fuentes con Git
y cada archivo de `dist` con la imagen nginx certificada. Sólo admite además
su página heredada `50x.html`, cuyo hash registra. Esta prueba permite verificar
el frontend construido en CI sin reinstalar ni reconstruir sus dependencias
durante la promoción; no sustituye sus tests ni el gate completo.

Las cachés contienen descargas de uv/pnpm, Chromium y capas BuildKit. Sus claves
incluyen plataforma, herramientas y locks pertinentes; los entornos se vuelven
a instalar con locks congelados. La ejecución fría no restaura esas cachés, aunque
puede guardarlas para una posterior caliente. Nunca se cachean PostgreSQL, datos,
fixtures, artefactos de negocio, backups, resultados de tests ni receipts PASS.

Todos los harnesses conservan Python 3.12. El entorno backend bloqueado y uv se
instalan en el job backend y en los tres grupos `async-volume-100`,
`async-volume-500` y `async-volume-1024`, que generan las poblaciones Parquet con
pyarrow. Los otros 14 grupos de la matriz usan stdlib y helpers del host; sus
imports de producto y herramientas backend se ejecutan dentro de sus contenedores.
Este ajuste no elimina los installs ni las pruebas dentro de esos contenedores.

El manifiesto de imágenes registra bytes y duración de exportación. Cada suite
registra carga y el intervalo real de descarga/verificación/carga en
`image-load.json`. Los pasos del workflow permiten separar construcción y
preparación de dependencias. Que una caché exista no acredita un hit ni una mejora:
se deben comparar las mediciones reales fría y caliente del mismo contrato.

## Aislamiento, plazos y evidencia

Los runners crean proyectos UUID, redes, volúmenes, PostgreSQL y secretos
sintéticos propios; usan puertos loopback disponibles fuera del habitual 3100.
El preflight CI histórico valida toda la configuración Compose resuelta, incluso
servicios inactivos, y la compara con el inventario habitual leído por labels.
Rechaza recursos externos/compartidos, binds de datos o secretos reales, env-file
ambiental, identidades DB/SSO/SMTP reales, sockets de control, privilegios,
namespaces host, reinicio automático y servicios sin cotas CPU/RAM/PIDs.
El mock OIDC explícito de la suite de identidad es la única excepción sintética
admitida; no habilita proveedores externos. El perfil 0.8.0 mantiene sus guardas
estrictas y el presupuesto agregado de 9 GiB.

Cada grupo tiene un deadline inferior al timeout del job. Por ejemplo, Catálogo
tiene 6.300 s frente a 120 minutos; los grupos XLSX tienen márgenes propios para
diagnóstico, subida de evidencia y limpieza. Un timeout de escenario es FAIL.
Los navegadores acotados reclaman su propio grupo de procesos; el cleanup sólo
puede retirar recursos nuevos reconocidos como propios, nunca el principal.

Los escenarios XLSX escriben progreso y receipts terminales incrementales con
UUID, SHA, run/attempt/job, filas, variante, tiempos, recursos y adjuntos con hash.
Un fallo posterior no borra los resultados anteriores ni vuelve PASS el grupo.
Los otros grupos normalizan sus reportes recién creados, sin descubrir evidencia
histórica. `group-timing.json` y los diagnósticos saneados se suben también al fallar.
Los logs crudos, dumps, queries, datos y secretos permanecen privados; los artifacts
públicos contienen únicamente documentos explícitos saneados y sus hashes.

Los artifacts se identifican dentro de cada run por SHA fuente e intento:
`ci-selection-<SHA>-<attempt>`, `ci-images-<SHA>-<attempt>`,
`ci-image-proof-<SHA>-<attempt>`, `ci-evidence-<group>-<SHA>-<attempt>` y
`ci-gate-<SHA>-<attempt>`. Los downloads usan el intento actual y los receipts
validan también `run_id` y `run_attempt`. Un reintento conserva los artifacts
anteriores con sus nombres propios; no los sobrescribe ni mezcla evidencia
vieja para completar un intento nuevo. Dos runs del mismo SHA siguen separados
por su identidad de run, aunque compartan el número de intento.

La comparación fría/caliente conserva ambos conjuntos completos con run,
intento, SHA, modo de caché, hashes y mediciones reales. El nombre del artifact
evita colisiones; no demuestra un cache hit ni autoriza reutilizar un resultado
PASS. Cada ejecución debe producir sus propias comprobaciones y gate. El coste
de exportar y transferir las imágenes y el ZIP se incluye al medir la mejora.

## Gate y promoción

`final_gate.py` comprueba la selección full, sus 19 grupos/64 escenarios exactos,
los `needs` reales exitosos, SHA/run/attempt/job, filas/variantes, tiempos,
identidad de imágenes, hashes y contenido de cada evidencia. Rechaza escenarios
ausentes, duplicados, inesperados, fallidos, cancelados, omitidos, RUNNING o de
otro SHA, además de adjuntos alterados o pruebas/oráculos incompletos.
Los skips de plataforma permitidos en suites unitarias se registran explícitos;
no acreditan escenarios Linux/JVM ni permiten omitir un escenario obligatorio.
Los recorridos generales de navegador siguen la selección exacta descrita arriba;
el gate rechaza cualquier selección incompleta o prueba nueva omitida.
Un gate rápido usa `DEVELOPMENT_ONLY` y `certifies_final=false`.

La aceptación final requiere `FULL_CERTIFICATION`, `certifies_final=true`, run
completado SUCCESS y todos los jobs aplicables completados SUCCESS del SHA final,
incluidos coordinación y gate. Después se obtiene un backup fresco consistente,
se verifica en un restore aislado detenido y se promueven imágenes de ese SHA.
La actualización conserva datos/volúmenes, .env, secretos, SSO/demo, puerto,
origen y políticas de reinicio. Sólo se migran cambios compatibles y se verifica
el estado operativo sin fixtures ni conexiones reales de prueba. Ver
[operación](operations.md) y [ADR 0028](../adr/0028-isolated-certification-recovery-upgrade-080.md).

## Historial y protocolo de medición

La [baseline saneada](evidence/0.8.0/ci-optimization-baseline.json) conserva fuentes
y hashes de logs/metadatos existentes; no vuelve a ejecutar ni certifica sus SHA.
El run `37368090419`/`495f4eb`, intento 4, terminó con 15 SUCCESS y XLSX CANCELLED
por el presupuesto de 90 minutos. Sus intervalos de ejecución suman **303,35
minutos de runner**, incluyendo frontend reutilizado del mismo SHA y el job
incompleto. Excluyen cola y no son minutos facturados ni un coste de certificación
completa. Los tres intentos anteriores tuvieron backend sin runner ni steps
durante 903/902/902 s; esas esperas de infraestructura se registran por separado.

| Medición real 495 | Segundos | Alcance |
| --- | ---: | --- |
| Backend, job | 405 | Instalación y verificaciones; tests, 320 s en el paso. |
| Frontend, job | 57 | Tests, 28 s en el paso; 33 archivos/252 tests en el log. |
| XLSX, job cancelado | 5.425 | Incluye setup/cleanup; paso funcional 5.376 s, incompleto. |
| Catálogo/Reportes, job | 4.050 | Paso funcional 3.938 s; ciclo saneado 3.934,695 s. |
| Catálogo API 120 / 400k / 1M | 38,412 / 1.045,771 / 2.343,219 | Incluye fixtures y verificaciones completas, no sólo motor SQL. |
| Catálogo navegador / HTTP / recovery | 36,009 / 95,360 / 243,209 | Componentes del ciclo; no se suman otra vez al total. |
| Backup, job | 655 | Nativo y tres fuentes auténticas. |
| Builds explícitos visibles | 180 | Mínimo conocido: 52+54+74; los builds internos no tienen desglose exacto. |

Los ocho intervalos observados de adquisición XLSX suman 1.393,372 s, excluyendo
generación de fixtures y oráculos completos. El navegador terminado midió
387,001 s; su población 400k se infiere del orden del driver, no de metadata del
summary. Ninguno acredita el navegador restante ni el restore XLSX incompleto.
La revisión posterior 630 amplió el presupuesto monolítico a 180 minutos;
sus checkpoints se conservan como antecedente del reparto actual.

El resultado final histórico 630 (`37388511854`, intento 1) fue **FAIL**:
15 jobs SUCCESS y Catálogo/Reportes fallido durante la población de un millón.
XLSX terminó SUCCESS en 3.344 s de job (55,73 min). El intervalo del run desde
23:26:35 UTC del 5 de octubre hasta su actualización terminal 00:29:06 UTC del
6 de octubre fue 3.751 s; sus 16 intervalos de jobs sumaron 14.546 s, o 242,43
minutos de runner, incluido el job fallido. Son intervalos REST, sin afirmar
facturación ni CPU útil. No constituyen una certificación completa ni una
comparación porcentual con 495. El snapshot previo de 11 SUCCESS/cinco pendientes
queda como antecedente; el resultado terminal lo sustituye para ese run.
La suite local 630 de frontend pasó 252 tests, lint/tipos/build; la de backend
Windows produjo 1.269 PASS/46 SKIP. Sus alcances difieren de backend+scripts Linux
y no se suman.

La comparación completa fría/caliente del nuevo contrato queda pendiente al
corte de este documento. Cada informe debe separar build, exportación,
transferencia/descompresión, preparación, fixtures, oráculos, navegador, recovery
y cleanup, además de colas y coste agregado sin duplicar componentes contenidos.
No se declara una reducción porcentual ni coste final sin ambos recorridos reales
comparables. El **informe externo de cierre** registra después del commit los
runs/intentos, resultados fría/caliente, SHA documental final, gate completo,
backup/restore y promoción, vinculados al hash del PDF revisado. No reescribe sus
bytes ni anticipa resultados futuros; tampoco permite certificar otro SHA.

Los resultados actuales y sus gates abiertos se mantienen en
[validación](validation.md). El PDF debe describir ese mismo contrato, generarse
desde los insumos congelados y revisarse completo antes del commit documental
que la certificación final ejecutará.
