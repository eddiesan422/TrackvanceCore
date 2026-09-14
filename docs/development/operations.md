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

La corrección añade `0002_evidence_v2`, sin modificar `0001_initial`. La API aplica
las migraciones al iniciar. En bases SQLite previas sin tabla Alembic, el adaptador
solo adopta un schema original reconocido o uno que coincida con el modelo actual;
un schema desconocido exige revisión y no se modifica a ciegas.

`scripts/check_postgres_migrations.py` usa `DATABASE_URL` del backend, crea una base
temporal de nombre aleatorio y comprueba upgrade v1→v2 con datos históricos, actors,
paridad con modelos y un ciclo downgrade/upgrade. El downgrade se realiza solamente
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

```powershell
python scripts/backup_local.py backup --source .local --destination backups/local-2026-09-13
python scripts/backup_local.py verify --source backups/local-2026-09-13
python scripts/backup_local.py restore --source backups/local-2026-09-13 --destination restored/local-2026-09-13
```

Los destinos deben ser nuevos y estar fuera del origen. Una restauración valida
todos los hashes antes de crear el destino. Reubica las rutas internas hacia el
nuevo almacenamiento, conserva IDs/versiones/configuraciones y verifica referencias.
No sustituye la instalación activa. Los backups contienen datos locales y deben
guardarse fuera de Git, con permisos equivalentes a los del almacenamiento original.

## Copia de PostgreSQL y ArtifactStore

El utilitario SQLite no sirve para PostgreSQL. Para una copia Compose consistente:

1. Finalizar los runs y pausar nuevas operaciones; detener API y worker.
2. Crear un dump PostgreSQL con `pg_dump --format=custom` dentro del contenedor
   PostgreSQL y copiarlo al host con `docker compose cp`. Evitar redirección binaria
   por PowerShell para no modificar los bytes del dump.
3. Copiar el volumen `trackvance_data` completo, conservando la raíz de almacenamiento.
   No respaldar directamente los archivos internos del volumen de PostgreSQL activo.
4. Calcular SHA-256 del dump y de la copia de artifacts, conservarlos juntos y arrancar
   API/worker. No incluir `.env` ni contraseñas en el manifest.
5. Restaurar con `pg_restore` en una base nueva y con un volumen de artifacts nuevo;
   ejecutar Alembic, readiness y `verify_storage.py` antes de usar la copia restaurada.

La prueba automatizada de migrations usa PostgreSQL real; la utilidad automática de
backup/restore de esta entrega está implementada para SQLite. Un proceso automatizado
de backup integral PostgreSQL más su volumen de artifacts sigue pendiente.

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

## Instalación que queda encendida

El 14 de septiembre se deja el proyecto `trackvance-certification` en http://localhost:3100, con los cuatro servicios saludables y política de reinicio `unless-stopped`. La configuración local `.env` conserva proyecto, puerto y override de red interna; `docker compose ps` muestra esta instalación. No se publica su contraseña. El reinicio completo de API, worker, web y PostgreSQL conservó todas las huellas comparadas y 81 artifacts. Los snapshots están en `.codex-local/operations-evidence/pre-final-rebuild.json` y `post-final-rebuild.json`.
