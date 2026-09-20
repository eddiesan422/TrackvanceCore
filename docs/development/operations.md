# Operación local y verificación de persistencia

Trackvance Core se ejecuta con cuatro servicios de Docker Compose: PostgreSQL 16,
API FastAPI, worker y pasarela web React/nginx. API y worker comparten el mismo
código del monolito y el volumen de ArtifactStore. La única puerta publicada es
la web, ligada a `127.0.0.1`; PostgreSQL y la API no publican puertos al host.

## Arranque en Windows

Desde la raíz del repositorio, con Docker Desktop en contenedores Linux:

```powershell
.\scripts\bootstrap.ps1
```

Si `.env` no existe, bootstrap lo crea desde `.env.example` con una contraseña
aleatoria local. No la muestra ni la sube a Git. Conserva un `.env` existente.
El arranque instala las imágenes, aplica Alembic y espera la salud de los cuatro
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

Cada nombre de proyecto Compose conserva sus propios volúmenes PostgreSQL y
ArtifactStore. `docker compose restart` y `docker compose down` conservan los
volúmenes. No se debe usar `down -v` para reiniciar o actualizar una instalación.
El prototipo nativo usa `.local/trackvance.db` y `.local/storage`; ese entorno no
es la base PostgreSQL de Docker y no se modifica al arrancar Compose.

Los cuatro servicios declaran `restart: "no"`. Docker Desktop puede iniciar con
Windows sin levantar Trackvance; el proyecto permanece detenido hasta ejecutar
manualmente `docker compose up -d --wait` desde la raíz. Para apagarlo sin borrar
contenedores ni volúmenes se utiliza `docker compose stop`.

## Readiness y diagnósticos

`GET /api/v1/health/ready` verifica conexión SQL, revisión Alembic y una escritura
temporal en almacenamiento. Devuelve 503 si alguna comprobación falla. El worker
escribe un heartbeat local; `doctor.py --docker` comprueba también que sea reciente.
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
API, worker y PostgreSQL permanecen exclusivamente en la red interna, sin gateway.
Este override **no garantiza bloqueo de Internet desde nginx**: en Docker Desktop
29.1.3 se observó salida desde ese contenedor incluso sin masquerade. El aislamiento
total del host depende de su firewall o desconexión externa; no se modifica la red
del equipo del usuario. La web sirve archivos y proxy local, sin necesitar esa salida.

## Migraciones y preservación

La revisión actual llega a `0007_monitor_scheduling`, precedida por
`0001_initial`, `0002_evidence_v2`, `0003_dataset_ingestion_metadata`,
`0004_exception_validation`, `0005_external_connections` y
`0006_local_identity_exceptions`. Las
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
Incluye también las credenciales cifradas y su clave maestra del modo directo.
Si la base referencia conexiones externas, la ausencia de un secreto o de su
clave impide declarar válido el respaldo; no se inventan credenciales nuevas.

```powershell
python scripts/backup_local.py backup --source .local --destination backups/local-2026-09-13
python scripts/backup_local.py verify --source backups/local-2026-09-13
python scripts/backup_local.py restore --source backups/local-2026-09-13 --destination restored/local-2026-09-13
```

Los destinos deben ser nuevos y estar fuera del origen. Una restauración valida
todos los hashes antes de crear el destino. Reubica las rutas internas hacia el
nuevo almacenamiento, conserva IDs/versiones/configuraciones y verifica referencias.
Las credenciales y la clave se restauran en `credentials/` y `keys/` del destino;
ambos directorios deben conservar acceso restringido. Los respaldos históricos
sin conexiones continúan siendo compatibles.
No sustituye la instalación activa. Los backups contienen datos locales y deben
guardarse fuera de Git, con permisos equivalentes a los del almacenamiento original.

## Copia y restauración Docker coordinada

`docker_state.py` automatiza PostgreSQL, ArtifactStore, SecretStore y su clave como
una sola unidad. El proyecto y la carpeta de destino son explícitos; el destino debe
ser nuevo. Durante una ventana breve detiene entrada, scheduler, worker y API,
rechaza runs/jobs pendientes, genera `pg_dump --format=custom`, archiva los tres
volúmenes y vuelve a iniciar solo los contenedores que estaban activos.

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

La restauración solo opera sobre otro proyecto `trackvance-...` sin contenedores,
volúmenes ni redes. Valida todo antes de crear recursos, restaura PostgreSQL en una
transacción, extrae rutas seguras y exige una huella idéntica que incluye todas las
tablas, SHA de artifacts, FK, linaje por organización y decrypt de cada secreto.
Sin `--start` deja el destino verificado y detenido.

```powershell
python scripts/docker_state.py restore --source backups/docker-20260919 `
  --target-project trackvance-recovery-20260919
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

El arranque aplica la migración aditiva `0005_external_connections`. Además de
`postgres_data` y `trackvance_data`, preserva `connection_credentials` (cifrado) y
`connection_keys` (clave maestra). Solo la API monta estos dos últimos volúmenes;
el worker monta únicamente `trackvance_data`. El respaldo debe incluir un dump de
metadata y las copias coordinadas de artifacts, credenciales y clave descritas
arriba. Los secretos no son artifacts descargables y no deben incluirse en logs,
Git o contextos de build.

Configura las fuentes con cuentas SELECT y TLS. Para un servidor en el PC usa un
host accesible desde Docker, como `host.docker.internal`. La BD interna almacena
solo metadata; el snapshot de la fuente se guarda como Parquet en ArtifactStore.
La lectura inicial es síncrona y acotada; límites y opciones en ADR 0007.

La certificación independiente se ejecuta con
`python scripts/tests/connections_cycle.py --full-playwright`: crea PostgreSQL y
SQL Server reales en un proyecto nuevo, verifica migraciones, fuentes, Intake,
exports, UI, fallos de acceso y persistencia; finalmente elimina sus propios
contenedores y volúmenes. No añade datos de prueba a la instalación habitual.

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
