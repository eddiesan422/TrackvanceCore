# Operación local y verificación de persistencia

Trackvance Core se ejecuta con cinco servicios de Docker Compose: PostgreSQL 16,
API FastAPI, worker `DEFAULT`, `delivery-worker` y pasarela web React/nginx. Los
dos workers reutilizan el mismo código del monolito, pero consumen lanes distintas.
La única puerta publicada es la web, ligada a `127.0.0.1`; PostgreSQL y la API no
publican puertos al host.

## Arranque en Windows

Desde la raíz del repositorio, con Docker Desktop en contenedores Linux:

```powershell
.\scripts\bootstrap.ps1
```

Si `.env` no existe, bootstrap lo crea desde `.env.example` con una contraseña
aleatoria local. No la muestra ni la sube a Git. Conserva un `.env` existente.
El arranque instala las imágenes, aplica Alembic y espera la salud de los cinco
servicios. La interfaz queda en `http://localhost:3000`.

Un entorno adicional puede coexistir con el prototipo nativo:

```powershell
$env:COMPOSE_PROJECT_NAME = 'trackvance-certification'
$env:WEB_PORT = '3100'
$env:TRACKVANCE_WEB_ORIGIN = 'http://localhost:3100'
.\scripts\bootstrap.ps1
python scripts/doctor.py --base-url http://localhost:3100 --docker
python scripts/smoke_test.py --base-url http://localhost:3100
```

Cada nombre de proyecto Compose conserva sus seis volúmenes: PostgreSQL,
ArtifactStore y los dos pares credencial/clave de fuentes y destinos.
`docker compose restart` y `docker compose down` conservan los
volúmenes. No se debe usar `down -v` para reiniciar o actualizar una instalación.
El prototipo nativo usa `.local/trackvance.db` y `.local/storage`; ese entorno no
es la base PostgreSQL de Docker y no se modifica al arrancar Compose.

Los cinco servicios declaran `restart: "no"`. Docker Desktop puede iniciar con
Windows sin levantar Trackvance; el proyecto permanece detenido hasta ejecutar
manualmente `docker compose up -d --wait` desde la raíz. Para apagarlo sin borrar
contenedores ni volúmenes se utiliza `docker compose stop`.

## Readiness y diagnósticos

`GET /api/v1/health/ready` verifica conexión SQL, revisión Alembic y una escritura
temporal en almacenamiento. Devuelve 503 si alguna comprobación falla. Cada worker
escribe su propio heartbeat (`worker-heartbeat-default.json` y
`worker-heartbeat-delivery.json`); `doctor.py --docker` comprueba ambos por lane.
El script respeta `COMPOSE_PROJECT_NAME` y nunca imprime la URL de base de datos,
el contenido de `.env` ni credenciales.

`smoke_test.py` usa únicamente la biblioteca estándar de Python. Crea datos de
verificación nuevos y conserva su evidencia. Comprueba autenticación/CSRF, Intake,
categorías Recon, Sentinel, versiones históricas, excepciones, auditoría y XLSX.
Para cada Excel verifica MIME, nombre descargable, cuatro hojas esperadas y ausencia
de fórmulas en las celdas de negocio. No reinicia ni elimina datos.

## Funcionamiento sin dependencias externas

Las imágenes y paquetes se descargan durante la preparación. Una vez construidos,
la aplicación, los archivos, PostgreSQL y los workers funcionan localmente. No hay
storage, autenticación, fuentes, analítica ni procesamiento cloud requerido.

El override siguiente coloca PostgreSQL, API y worker en una red Docker interna.
Si el proyecto ya existe, se recrea únicamente su red conservando los volúmenes:

```powershell
docker compose down
docker compose -f compose.yml -f deploy/docker/compose.offline.yml up -d --wait --pull never --no-build
```

Docker Desktop no publica la puerta de una pasarela conectada únicamente a una red
interna en el host probado. Por ello nginx tiene un segundo puente sin masquerade.
API, worker `DEFAULT`, `delivery-worker` y PostgreSQL permanecen exclusivamente en
la red interna, sin gateway.
Este override **no garantiza bloqueo de Internet desde nginx**: en Docker Desktop
29.1.3 se observó salida desde ese contenedor incluso sin masquerade. El aislamiento
total del host depende de su firewall o desconexión externa; no se modifica la red
del equipo del usuario. La web sirve archivos y proxy local, sin necesitar esa salida.

## Migraciones y preservación

La revisión actual llega a `0009_delivery_reviews`, precedida por
`0001_initial`, `0002_evidence_v2`, `0003_dataset_ingestion_metadata`,
`0004_exception_validation`, `0005_external_connections` y
`0006_local_identity_exceptions`, `0007_monitor_scheduling` y `0008_data_delivery`. Las
correcciones se incorporan con nuevas migraciones; el desacoplamiento mediante
puertos no requiere modificar el schema. La API aplica las migraciones al iniciar.
En bases SQLite previas sin tabla Alembic, el adaptador
solo adopta un schema original reconocido o uno que coincida con el modelo actual;
un schema desconocido exige revisión y no se modifica a ciegas.

`scripts/check_postgres_migrations.py` usa `DATABASE_URL` del backend, crea una base
temporal de nombre aleatorio y comprueba upgrade desde v1 hasta head con datos
históricos, actores, paridad con modelos y un ciclo downgrade/upgrade. El downgrade se realiza solamente
en esa base temporal. La base de aplicación nunca se baja de versión ni se elimina.
El usuario PostgreSQL debe tener permiso de creación de bases para esta prueba.

```powershell
docker compose cp scripts/check_postgres_migrations.py api:/tmp/check_postgres_migrations.py
docker compose exec -T api python /tmp/check_postgres_migrations.py
```

`scripts/verify_storage.py snapshot` calcula hashes de los registros persistidos y
verifica tamaño/SHA-256 de todos los artifacts registrados. Su salida contiene IDs
y hashes, no datos de negocio ni sesiones. Para certificar un reinicio, finalizar
los runs y detener nuevas operaciones durante ambas capturas:

```powershell
docker compose cp scripts/verify_storage.py api:/tmp/verify_storage.py
docker compose exec -T api python /tmp/verify_storage.py snapshot > before.json
docker compose restart
docker compose up -d --wait --pull never --no-build
docker compose exec -T api python /tmp/verify_storage.py snapshot > after.json
python scripts/verify_storage.py compare before.json after.json
```

Si se recrea el contenedor, debe copiarse de nuevo el script temporal antes de la
segunda captura. No ejecutar login, exports, smoke ni pruebas durante el intervalo:
son operaciones auditadas y agregan registros legítimos que cambiarían la huella.

## Copia y restauración del prototipo SQLite

El respaldo usa la API de backup de SQLite; consolida una fuente WAL en un solo
archivo de copia, sin alterar el journal del origen. Incluye todos los archivos
referenciados por versiones, runs y ArtifactStore, conserva sus bytes y genera un
manifest con SHA-256/tamaños. Se comprueban `integrity_check`, rutas y cobertura.
Incluye también las credenciales cifradas y claves maestras separadas del modo
directo. Si la base referencia conexiones externas o destinos de Delivery, la
ausencia de su secreto o clave correspondiente impide declarar válido el respaldo;
no se inventan credenciales nuevas.

```powershell
python scripts/backup_local.py backup --source .local --destination backups/local-2026-09-13
python scripts/backup_local.py verify --source backups/local-2026-09-13
python scripts/backup_local.py restore --source backups/local-2026-09-13 --destination restored/local-2026-09-13
```

Los destinos deben ser nuevos y estar fuera del origen. Una restauración valida
todos los hashes antes de crear el destino. Reubica las rutas internas hacia el
nuevo almacenamiento, conserva IDs/versiones/configuraciones y verifica referencias.
Los secretos se restauran en `credentials/`, `keys/`, `delivery_credentials/` y
`delivery_keys/`; los cuatro directorios deben conservar acceso restringido. Los
respaldos históricos sin conexiones ni destinos continúan siendo compatibles.
No sustituye la instalación activa. Los backups contienen datos locales y deben
guardarse fuera de Git, con permisos equivalentes a los del almacenamiento original.
Después de copiar cada archivo, incluido `trackvance.db`, el helper compara
tamaño/SHA contra el manifest antes de abrir o mutar la copia. Una sustitución
concurrente aborta la operación.

## Copia y restauración Docker coordinada

`docker_state.py` automatiza PostgreSQL, ArtifactStore y los SecretStore de fuentes
y destinos con sus claves como una sola unidad. El proyecto y la carpeta de destino
son explícitos; el destino debe ser nuevo. Durante una ventana breve detiene entrada,
scheduler, ambos workers y API, rechaza runs/jobs pendientes, genera
`pg_dump --format=custom`, archiva los cinco volúmenes de archivos y vuelve a iniciar
solo los contenedores que estaban activos.

```powershell
python scripts/docker_state.py inventory --project trackvance-core
python scripts/docker_state.py backup --project trackvance-core --destination backups/docker-20260919
python scripts/docker_state.py verify --source backups/docker-20260919
```

El manifest contiene hashes, tamaños, modos, revisión Alembic, imágenes y la huella
quiescente; no contiene `.env`, contraseñas, referencias de secretos, clave ni rutas
absolutas. La carpeta sí contiene credenciales cifradas y clave maestra: restringir
su acceso y no publicarla como artifact de CI. Un backup parcial queda para
diagnóstico, pero `verify` no lo acepta.
El backup usa staging temporal privado, crea copias exclusivamente, ejecuta
`fsync`, vuelve a comprobar tamaño/hash e inventario, marca los componentes
read-only y hace que verify/restore consuman únicamente esos bytes preparados.

La restauración solo opera sobre otro proyecto `trackvance-...` sin contenedores,
volúmenes ni redes. Valida todo antes de crear recursos, restaura PostgreSQL en una
transacción, extrae rutas seguras y exige una huella idéntica que incluye todas las
tablas, SHA de artifacts, FK, linaje por organización y decrypt de cada secreto.
Sin `--start` deja el destino verificado y detenido.

```powershell
python scripts/docker_state.py restore --source backups/docker-20260919 `
  --target-project trackvance-recovery-20260919 --start --web-port 3200
python scripts/doctor.py --base-url http://localhost:3200 --docker `
  --project trackvance-recovery-20260919 --recovery-ready
```

`--start --web-port 3200` deja la copia activa. `--smoke` requiere `--start` y se
reserva para copias aisladas con identidad demo; agrega registros legítimos después
de comparar la huella. Una falla detiene el proyecto nuevo para diagnóstico y nunca
modifica ni elimina el origen.

El drill destructivo seguro crea tres proyectos desechables: PostgreSQL externa,
aplicación fuente y restauración. Crea por API una conexión/snapshot/run, respalda,
elimina la aplicación fuente antes del restore, prueba la credencial restaurada,
refresca la fuente y ejecuta Intake. Conserva solo `result.json` como evidencia CI:

```powershell
python scripts/tests/docker_backup_cycle.py
```

El drill certificado del 19 de septiembre terminó `PASS` con siete artifacts, un
secreto y 124 relaciones exactas. La aplicación fuente fue destruida antes de la
restauración; después se reutilizó la credencial restaurada contra la PostgreSQL
externa, se refrescó el datasource y se ejecutó un Intake nuevo. La comprobación web
de este drill es HTTP/HTML 200; Playwright queda registrado como `NOT_RUN` y se cubre
en su suite separada.

El drill 0.5.0 separado terminó PASS con siete artifacts, dos secretos —fuente y
destino— y 127 relaciones. Destruyó el origen, restauró en 0008 y utilizó la
credencial destino recuperada para una Delivery COMMITTED de cuatro filas. La
compatibilidad desde la baseline 0.4.1/0007 preservó la proyección normalizada de
21 tablas, migró a 24, dejó Delivery vacío y asignó lane DEFAULT a 19 jobs. State
2 no contenía un hash estructural del catálogo; no se afirma una prueba DDL que el
formato histórico nunca almacenó.

## Reset local con plan exacto

El reset nunca se ejecuta directamente desde un nombre o patrón. Primero genera un
plan de 15 minutos con los IDs actuales y una confirmación literal. El wrapper
detecta el proyecto desde `COMPOSE_PROJECT_NAME`, `.env` o `name:` de Compose; se
puede fijar con `-Project`.

```powershell
.\scripts\reset-local.ps1 -Project trackvance-core
# Revisar el JSON y copiar required_confirmation de la salida anterior.
.\scripts\reset-local.ps1 -Plan .codex-local\reset-plans\trackvance-core-AAAAmmdd-HHmmss.json `
  -Confirm 'RESET:trackvance-core:0123456789ab'
```

Antes de eliminar, `reset` recalcula el hash, comprueba expiración, etiquetas y el
inventario completo; cualquier cambio aborta. Elimina solo los IDs del plan, sin
glob, `down -v` ni `prune`, y escribe recibo fuera de los volúmenes. Se recomienda
un backup verificado antes de resetear, pero el comando no impone un backup ni una
segunda excepción oculta.

## Evidencia de esta ejecución — 13 de septiembre de 2026

| Comprobación | Resultado observado |
| --- | --- |
| Docker Desktop / motor | 29.1.3; cuatro servicios sanos en `127.0.0.1:3100` |
| PostgreSQL | `0002_evidence_v2` aplicada |
| Upgrade histórico PostgreSQL | PASS; seis tablas previas conservadas; actor backfill y paridad ORM |
| Roundtrip de migrations | PASS en base temporal; base activa conservada |
| Doctor | 7/7 comprobaciones correctas |
| Smoke funcional | 65/65; tres informes XLSX y ciclo de excepciones |
| Red backend offline | Red `internal=true`; conexión TCP saliente del API bloqueada; smoke completo correcto |
| Salida de nginx | Sigue disponible en este Docker Desktop; no se certifica aislamiento total de la pasarela |
| Tests de operaciones | 5/5; incluye origen WAL activo, corrupción, traversal y destinos protegidos |
| Ruff de scripts | Correcto |
| Backup/restauración SQLite real | 101 archivos; copia verificada y restauración en destino nuevo |

Un primer intento de restauración real detectó sidecars WAL en el manifest. Se
corrigió el manejo de conexiones/journal, se añadió la regresión y la repetición
pasó. Los directorios de ese intento se conservaron para diagnóstico; el backup
verificado es `.codex-local/operations-evidence/local-backup-20260913-v2` y su
restauración `.codex-local/operations-evidence/local-restored-20260913-v2`.

El workflow GitHub Actions está preparado para backend, frontend, PostgreSQL y E2E
Compose. Los resultados locales no implican que dicho workflow remoto ya se haya
ejecutado; este ciclo no publica ni hace push al repositorio.

## Instalación con arranque manual

El proyecto local `trackvance-certification` usa `http://localhost:3100` cuando
está activo y conserva sus datos en volúmenes. Su política de reinicio es `no`,
por lo que no arranca junto con Docker Desktop. La configuración `.env` conserva
el nombre del proyecto, puerto y override de red interna; no se publica su
contraseña. Los snapshots históricos de persistencia están en
`.codex-local/operations-evidence/pre-final-rebuild.json` y
`post-final-rebuild.json`.

### Conexiones externas - 19 de septiembre de 2026

Para consumir PostgreSQL/SQL Server externos, la instalación principal usa ahora
el Compose base (`COMPOSE_FILE=compose.yml`), conservando proyecto, puerto, datos y
`restart=no`. El overlay offline continúa disponible como opción para operación
sin fuentes externas; bloquea la salida necesaria para consultar esas bases.
No se publican puertos adicionales de PostgreSQL interno ni de API.

Además de `postgres_data` y `trackvance_data`, Compose preserva
`connection_credentials` y `connection_keys` para fuentes, y
`delivery_credentials` y `delivery_keys` para destinos. La API monta ambos dominios
porque prueba fuentes y destinos. El worker `DEFAULT` monta únicamente
`trackvance_data`; `delivery-worker` monta datos y solo los secretos de destinos.
El respaldo incluye el dump de metadata y las cinco copias coordinadas de archivos.
Los secretos no son artifacts descargables y no deben incluirse en logs, Git o
contextos de build.

Configura las fuentes con cuentas SELECT y TLS. Para un servidor en el PC usa un
host accesible desde Docker, como `host.docker.internal`. La BD interna almacena
solo metadata; el snapshot de la fuente se guarda como Parquet en ArtifactStore.
La lectura inicial es síncrona y acotada; límites y opciones en ADR 0007.

La certificación independiente se ejecuta con
`python scripts/tests/connections_cycle.py --full-playwright`: crea PostgreSQL y
SQL Server reales en un proyecto nuevo, verifica migraciones, fuentes, Intake,
exports, UI, fallos de acceso y persistencia; finalmente elimina sus propios
contenedores y volúmenes. No añade datos de prueba a la instalación habitual.

### Data Delivery - 23 de septiembre de 2026

La lane `DELIVERY` no comparte credenciales de escritura con el worker normal. El
servicio `delivery-worker` no ejecuta el scheduler y conserva su heartbeat separado.
`doctor.py --docker` valida la lane y los montajes exactos: el worker `DEFAULT` no
puede montar secretos y el de Delivery no puede montar secretos de fuentes.

La certificación aislada se prepara con:

```powershell
python scripts/tests/delivery_cycle.py
```

El runner exige un proyecto `trackvance-delivery-e2e-*` nuevo, levanta PostgreSQL y
SQL Server desechables, crea cuentas de escritura acotadas, prueba CREATE_AND_LOAD,
APPEND, OVERWRITE y UPSERT, además de preflight inválido, permisos insuficientes,
un intento remoto fallido, receipt, manifest, auditoría y ausencia de secretos.
Puede ejecutar el flujo Playwright focal `tests-e2e/delivery.spec.ts`; al terminar
elimina exclusivamente sus contenedores y volúmenes. La ejecución local publicada
aprobó 92 comprobaciones y Playwright 1/1; GitHub Actions se informa por separado.

PostgreSQL requiere `INSERT` para APPEND; `INSERT+DELETE` para OVERWRITE; y
`INSERT+UPDATE+SELECT` más `TEMPORARY` de base para UPSERT. En SQL Server,
cualquier tabla existente requiere `SELECT` además del permiso de escritura para
adquirir el lock y validar catálogos. `OVERWRITE` requiere
`INSERT+DELETE+VIEW DEFINITION`; sin visibilidad
completa de security policies falla cerrado. No habilites `IGNORE_DUP_KEY` en
índices únicos de un target Delivery. PostgreSQL `OVERWRITE` no se ejecuta cuando
`row_security_active` es verdadero para la cuenta. SQL Server UPSERT necesita
`INSERT+UPDATE+SELECT`; la creación de `#temp` depende de la política de `tempdb`.

El restore 0.5.0 aceptaba backups 0.4.1 manifest 1/state 2/0007 y los migraba a 0008.
Aquella certificación preservó exactamente 21 tablas legacy, dejó vacías las tres
tablas Delivery y convirtió 19 jobs históricos a lane DEFAULT. Los directorios de
backup se preparan con permisos privados y cada copia se valida por tamaño/hash
antes de abrirla; no uses un backup cuyo inventario cambie durante la operación.

## Endurecimiento 0.5.1: reparación, revisión y recuperación

La migración vigente es `0009_delivery_reviews`; no se modifican 0001..0008.
Los comandos habituales aplican la migración al arrancar API. Nunca se ejecuta
un downgrade sobre la instalación operativa para probar compatibilidad.

### Entrega COMMITTED con evidencia pendiente

1. Abrir la Run: verificar SUCCESS/COMMITTED y aviso PENDING_REPAIR.
2. Con permiso `runs:execute`, pulsar **Reparar evidencia**. La acción llama
   `POST /api/v1/delivery/runs/{id}/repair-evidence` con sesión y CSRF.
3. REPAIRED completa receipt/manifest; ALREADY_VALID confirma que están íntegros.
   Repetir la acción no vuelve a entregar filas ni crea artifacts duplicados.
4. Ante 409 de evidencia no verificable, conservar error/request_id y revisar
   artifact/hash/configuración local. No lanzar otra entrega para fabricar receipt.
   Un archivo corrupto no se sobreescribe automáticamente: recuperar evidencia
   verificable desde backup requiere intervención específica.

La reparación no solicita contraseña ni llama al destino. Un fallo de red del
receptor no impide reparar datos locales válidos. UNKNOWN y FAILED nunca se
reparan como COMMITTED.

### Revisión externa de UNKNOWN

Comprobar el destino mediante herramientas y permisos propios del operador;
registrar outcome, nota y fecha en el detalle de Run. Outcomes permitidos:
REMOTE_COMMIT_OBSERVED, REMOTE_NOT_COMMITTED_OBSERVED e INCONCLUSIVE. Evitar
credenciales o datos personales innecesarios en la nota; es visible a lectores
autorizados del Run. El historial es append-only: una corrección se anexa.

UNKNOWN sigue UNKNOWN después de guardar. No hay aprobación automática, otro
Job ni replay. Si la revisión justifica una nueva entrega, debe iniciarse como
una acción deliberada distinta y considerar la estrategia/población del destino.

### Backup 0.5.1 y upgrades compatibles

El backup nativo usa manifest 2/state 4/0009 y conserva 25 tablas, artifacts, ambas
familias de secretos y claves. Restore de 0.5.0 / manifest 2 / state 3 / 0008 verifica
una proyección legacy-v3 exacta y que delivery_reviews está vacía. Restore de 0.4.x
/ manifest 1 / state 2 / 0007 conserva proyección legacy-v2, cuatro tablas Delivery vacías
y jobs DEFAULT. No se reescriben backups fuente ni datos originales durante
la comparación. La evidencia de cada drill 0.5.1 vive en validation.md.

El verificador reconoce huellas 0007/state 2, pero el CLI actual de creación de
backup exige la topología Delivery de seis volúmenes y dos workers. Para crear
un respaldo de una instalación todavía en 0007 se utiliza su tooling archivado;
aceptar y restaurar ese respaldo no implica que el CLI nuevo pueda crearlo sobre
la topología antigua. Para runtime 0008 sí conserva la huella nativa state 3.

### Medición Delivery separada del benchmark general

```powershell
python scripts/tests/delivery_benchmark_cycle.py --smoke --max-wall-seconds 1200
python scripts/tests/delivery_benchmark_cycle.py --max-wall-seconds 1800
```

El segundo comando usa 8 MiB/20.000 filas variadas; el primero, 1 MiB/1.000 filas.
El runner crea un proyecto `trackvance-delivery-bench-*` fresco, descarta nombres
ajenos/preexistentes y elimina sólo recursos propios. Cuenta cuatro estrategias
en PostgreSQL16 y SQLServer reales. No eleva límites de upload/filas del producto.
Guarda tiempos preflight/write/total, filas/s, MB/s, CPU, memoria, I/O y storage;
NOT_RUN_RESOURCE_LIMIT o STOPPED_RESOURCE_LIMIT no son PASS ni certificación de
capacidad productiva. Consulte delivery-benchmark-results-0.5.1.md.

Code splitting requiere servir el build frontend nuevo; construir código no
actualiza por sí solo un contenedor ya ejecutándose. Los runners certificados
usan proyectos separados; la instalación principal de localhost:3100 no se
reconstruye como parte de las pruebas de 0.5.1.

## Revisión de arquitectura - 16 de septiembre de 2026

El rebuild de la instalación principal conservó exactamente las huellas de
7 datasets, 12 versiones, 7 configuraciones, 10 runs, 21 findings, 1 excepción,
49 eventos de auditoría, 102 métricas y 39 artifacts. La comparación está en
`.codex-local/architecture-alignment/before.json` y `after.json`.
API, worker, web y PostgreSQL quedaron saludables; los cuatro conservan
`restart=no`. La aplicación queda activa en localhost:3100, sin arranque
automático cuando se reinicie Docker Desktop.

Para repetir todo el ciclo sin datos de prueba en la instalación habitual:

```powershell
python scripts/tests/docker_e2e_cycle.py
```

El runner genera un nombre `trackvance-e2e-*` y puerto libre, rechaza recursos
preexistentes, construye los contenedores y ejecuta doctor, migraciones,
smoke, Playwright y comparación de hashes tras restart. Al finalizar elimina
solo ese proyecto y sus volúmenes. Guarda evidencia en `.codex-local/`.
