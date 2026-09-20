# Trackvance Core

## Evolución funcional local 0.4.0

El prototipo conserva el monolito modular FastAPI/React, PostgreSQL, worker
y almacenamiento local. Incluye perfiles observados sin trim implícito,
identificadores con ceros iniciales, reglas declarativas avanzadas, salidas
Intake en Parquet, evidencia v2 y reportes Excel estructurados con openpyxl.
Las configuraciones publicadas, ejecuciones y archivos históricos permanecen
inmutables. La capa de entrada admite CSV, Excel XLSX, JSON, Parquet y TXT
delimitado, además de PostgreSQL y SQL Server mediante **Conexiones**;
la migración más reciente es `0007_monitor_scheduling`.

Esta revisión amplía Intake con unicidad compuesta, longitud, comparaciones,
condiciones e integridad contra otra DatasetVersion. ReconOps permite reglas
independientes por columna, políticas de null, transforms y agregaciones 1:N/N:1.
Sentinel incorpora programación local e historia compatible; Excepciones añade
asignación, SLA, adjuntos, reapertura y resolución automática opcional con evidencia
técnica. Configuración permite administrar usuarios locales y sus cinco roles.
La [línea oficial de evolución](docs/roadmap.md) conserva los puntos 1–2 y madura
los puntos 3–9 antes de productizar. Las pruebas de operación y volumen se informan
con resultados medidos; no se habilita infraestructura cloud.

Documentación del ciclo:

- [Arquitectura local y evolución del producto](docs/architecture.md).
- [Puertos de almacenamiento, fuentes, ejecución y cola](docs/adr/0006-architecture-ports.md).
- [Semántica y catálogo de reglas](docs/rules-catalog.md).
- [Reglas avanzadas y referencias inmutables](docs/adr/0008-advanced-intake-rules.md).
- [Conciliación configurable](docs/adr/0009-configurable-reconciliation.md).
- [Programación Sentinel local](docs/adr/0010-local-sentinel-scheduler.md).
- [Excepciones operativas y resolución automática](docs/adr/0011-advanced-exception-workflow.md).
- [Administración local de usuarios](docs/adr/0012-local-identity-administration.md).
- [Manifiestos, identidad y compatibilidad](docs/adr/0002-evidence-and-artifacts.md).
- [Lectores y normalización de datasets](docs/adr/0004-dataset-readers.md).
- [Conexiones, credenciales y snapshots externos](docs/adr/0007-external-connections.md).
- [Validación técnica de excepciones](docs/adr/0005-technical-validation-of-exceptions.md).
- [Contenido y seguridad de los Excel](docs/exports-xlsx.md).
- [Validación y pendientes explícitos](docs/development/validation.md).
- [Operación, Docker y respaldo](docs/development/operations.md).
- [Backup, restore y reset verificados](docs/adr/0013-local-backup-restore.md).
- [Benchmarks medidos y límites](docs/development/volume-benchmark.md).
- [OpenAPI 0.4.0](backend/openapi.json) y [contrato HTTP](backend/API_CONTRACT.md).
- [Especificación técnica v1.1 — implementación 0.4.0](docs/specification/README.md).

La instalación local `trackvance-certification` utiliza `http://localhost:3100`.
El puerto por defecto de una instalación nueva es 3000; `WEB_PORT` y
`TRACKVANCE_WEB_ORIGIN` deben corresponderse. Los resultados de cada ciclo están
en el documento de validación, separados de las capacidades preparadas.

Prototipo local de la plataforma de confiabilidad de datos de Trackvance
Colombia SAS. Integra Data Intake Gateway, ReconOps, Data Sentinel,
Excepciones y Auditoría en una misma aplicación.

Basado en **Trackvance Core — Especificación Técnica v1.1**. Esta evolución
funcional no representa la implementación de la
arquitectura objetivo del documento. Consulta [el estado de arquitectura](docs/architecture.md)
y [el alcance vigente](docs/development/prototype-scope.md).
Los resultados de las pruebas están en [verificación local](docs/development/validation.md).
La especificación oficial v1.1 tiene una [revisión de implementación 0.4.0](docs/specification/README.md)
y conserva su [fuente editable](docs/specification/Trackvance_Core_Especificacion_Tecnica_v1.1.md) en el repositorio.

## Inicio recomendado: Docker Compose y PostgreSQL

Con Docker Desktop iniciado en modo de contenedores Linux, ejecuta desde la raíz
del repositorio:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
```

El bootstrap conserva un `.env` existente o crea uno con una contraseña local
aleatoria, construye las imágenes, aplica Alembic y espera la salud de los cuatro
servicios. Abre **http://localhost:3000** en una instalación nueva, o el puerto
configurado. La instalación de este equipo usa **http://localhost:3100**.
El botón **Entrar al entorno demo** abre la sesión local cuando ese modo está
habilitado. La creación de los datos ficticios de ejemplo se configura por
separado.

Para iniciar y detener una instalación ya preparada:

```powershell
docker compose up -d --wait
docker compose stop
```

PostgreSQL guarda metadata; los archivos y artefactos permanecen fuera de la
base, en un volumen persistente compartido por API y worker. Todos los servicios
usan `restart: "no"`: Trackvance espera el arranque manual aunque se inicie
Docker Desktop. Detenerlo no borra datos. El acceso demo debe permanecer limitado
al entorno local de revisión.

## Acceso demo y datos demo

Las dos capacidades se controlan de forma independiente:

- `DEMO_ACCESS_ENABLED` habilita únicamente `POST /api/v1/auth/demo` y el acceso
  mediante **Entrar al entorno demo**. Si está en `false`, el endpoint devuelve
  `404 DEMO_DISABLED`, aunque ya existan datos demo.
- `DEMO_SEED_ENABLED` habilita únicamente la creación idempotente de datasets,
  configuraciones, ejecuciones, hallazgos y excepciones sintéticas al arrancar.

| `DEMO_ACCESS_ENABLED` | `DEMO_SEED_ENABLED` | Resultado en una instalación nueva |
| --- | --- | --- |
| `true` | `true` | Permite iniciar la sesión demo y crea los datos sintéticos. |
| `true` | `false` | Permite iniciar la sesión demo; sólo garantiza la identidad y organización mínimas, sin datos sintéticos. |
| `false` | `true` | Deshabilita `/auth/demo`, pero crea los datos sintéticos. |
| `false` | `false` | Deshabilita `/auth/demo` y no crea datos sintéticos. |

Ambas variables valen `true` por defecto para conservar el recorrido local
existente. Por compatibilidad segura con instalaciones anteriores, si
`DEMO_ACCESS_ENABLED` no está definida hereda temporalmente el valor de
`DEMO_SEED_ENABLED`; defínela explícitamente para desacoplar ambos modos. Cambiar
`DEMO_SEED_ENABLED` a `false` evita nuevas creaciones, pero no borra datos guardados
en un volumen persistente. Para validar una plataforma limpia, usa una base y un
volumen nuevos; la autenticación local y RBAC no cambian.

## Recorrido de demostración

1. **Centro de control:** usa los filtros por período, dataset, módulo, estado y
   criticidad para localizar fallos. Revisa salud general, controles fallidos,
   excepciones abiertas, datasets afectados y su variación; las secciones de
   atención priorizada, evolución por módulo y ejecuciones recientes llevan al
   detalle desde cada acción.
2. **Datasets:** abre un ejemplo y revisa versiones, esquema y perfil, o crea
   un dataset y carga CSV, Excel XLSX, JSON tabular, Parquet o TXT delimitado.
   El formulario detecta formato, columnas y tipos antes de cargar; permite
   corregir el tipo lógico, seleccionar todas o algunas columnas identificadoras,
   elegir hoja de Excel o delimitador TXT y agregar un área de negocio. Estas
   opciones también están disponibles al crear una versión nueva. El listado
   muestra el origen de la última versión, por ejemplo Manual o Data Intake.
   Si el nombre ya existe, agrega el archivo como una versión nueva.
3. **Intake:** al crear un contrato, selecciona un dataset para cargar su
   esquema y elegir columnas reales en las reglas obligatoria, única,
   numérica y positiva. Cada selector incluye “Todos”; en valores positivos la
   selección masiva se limita a las columnas numéricas. Después ejecuta la
   validación y revisa decisión, errores y salida válida. Agrega reglas avanzadas
   sobre columnas reales: unicidad compuesta, tipo, rango, longitud, lista, regex,
   fecha, comparación entre columnas, condición o referencia a otra versión.
   Las métricas separan filas evaluadas, incumplidas y excluidas.
4. **ReconOps:** selecciona ambas fuentes, claves simples o compuestas, transforms
   explícitos y comparación exacta, tolerancia absoluta, porcentual o temporal por
   columna. Configura nulls y, si corresponde, SUM/COUNT sobre origen o destino.
   Revisa cada comparación, líneas del grupo y los siete tipos de resultado.
5. **Excepciones:** convierte un hallazgo en caso, investígalo y envíalo a
   validación. Solo una ejecución posterior del mismo snapshot de configuración
   que confirme la corrección habilita **Resolver**. Los cierres administrativos
   (descartada, aceptada o no aplica) exigen un motivo y permanecen separados de
   una resolución técnica. El detalle enlaza la ejecución de origen y la de
   validación, y cada cambio conserva su evento en el historial. Asigna responsable,
   prioridad, SLA y fecha objetivo; agrega comentarios/evidencia y filtra vencidos.
   La resolución automática se activa por caso, está apagada por defecto y exige
   la misma comprobación técnica. Una reapertura requiere comentario.
6. **Sentinel:** ejecuta o programa un monitor con intervalo e inicio. El worker
   evalúa la última versión ya registrada, conserva fecha prevista/real y evita
   solapamientos. Revisa controles, tendencias, bandas históricas y alertas internas.
   Actualizar datos desde una conexión sigue siendo una operación separada.
7. **Usuarios:** como Administrator, abre Configuración para crear/editar cuentas,
   asignar roles, activar/desactivar, consultar permisos y restablecer contraseñas.
   Los cambios sensibles revocan sesiones; el último administrador queda protegido.

Cada ejecución completada ofrece un informe Excel y manifiesto JSON. `SUCCESS`
indica que el procesamiento terminó: los datos pueden contener diferencias o
haber sido rechazados por Intake.

## Detalles de Docker Compose

En otros sistemas, copia `.env.example` a `.env`, reemplaza
`POSTGRES_PASSWORD` por una contraseña local alfanumérica y ejecuta:

```sh
docker compose up --build -d --wait
```

Servicios: `web` en localhost:3000, API, worker estándar y PostgreSQL.
Los archivos y los metadatos persisten en volúmenes Docker. No expone
PostgreSQL ni la API al exterior. Detener con `docker compose down` conserva
los volúmenes. No uses `down -v` si quieres conservar los datos.

Los servicios tienen política de reinicio `no`: Docker Desktop no inicia
Trackvance al encender el equipo. Arráncalo manualmente desde esta carpeta con
`docker compose up -d --wait` y detenlo con `docker compose stop`.

Compose ha sido verificado con PostgreSQL 16 y Docker Engine 29.1.3. El build
inicial descarga dependencias; con las imágenes preparadas la aplicación opera
sin servicios externos. Consulta la evidencia de operación y sus límites.

## Conexiones PostgreSQL y SQL Server

En **Conexiones**, crea una conexión, selecciona el motor y completa host, puerto,
base, usuario y contraseña. Prueba el acceso y guarda. Después explora un schema,
elige una tabla/vista, revisa columnas y una muestra limitada y registra el dataset.
El dataset ofrece **Actualizar desde fuente** para crear una versión nueva; Intake,
ReconOps y Sentinel usan esos snapshots con el mismo flujo que los archivos.

La fuente externa es distinta del PostgreSQL interno de Trackvance. Utiliza una
cuenta externa de solo lectura y un host accesible desde el contenedor API. Para
una base instalada en el PC, usa `host.docker.internal`. El Compose base admite
esa salida; el overlay offline debe retirarse si necesitas fuentes externas.

Las contraseñas se cifran fuera de PostgreSQL y del almacenamiento de artifacts.
Conserva los volúmenes `connection_credentials` y `connection_keys` al actualizar
la instalación y respáldalos de forma protegida junto con metadata y artifacts.
Solo la API monta esos dos volúmenes; el worker procesa los snapshots canónicos
sin acceder a credenciales externas. Al editar host, puerto, base, usuario o modo
TLS se exige introducir de nuevo la contraseña antes de probar o guardar.
Nunca publiques ni incluyas sus contenidos en Git. El cifrado de transporte está
activado por defecto; configura certificados y permisos en el servidor según tu
entorno. No se ofrece escritura hacia bases externas.

Para certificar ambos motores en contenedores reales, sin tocar datos de tu
instalación, ejecuta desde la raíz con Docker, Python y pnpm disponibles:

```powershell
python scripts/tests/connections_cycle.py
```

El ciclo incluye PostgreSQL, SQL Server, smoke API y Playwright. Requiere recursos
para ambos motores; crea un proyecto temporal y elimina solamente sus propios
volúmenes. Los detalles de límites, secretos y nuevos conectores están en
[ADR 0007](docs/adr/0007-external-connections.md).

## Mantenimiento y pruebas de volumen

El backup Docker conserva un conjunto consistente: PostgreSQL, artifacts,
credenciales cifradas y clave. Detiene brevemente las escrituras, registra hashes
y vuelve a iniciar sólo los servicios que estaban activos. El restore exige un
proyecto nuevo, verifica la huella y puede dejarlo detenido o iniciarlo explícitamente.

```powershell
python scripts/docker_state.py backup --project trackvance-certification --destination backups/local
python scripts/docker_state.py verify --source backups/local
python scripts/docker_state.py restore --source backups/local --target-project trackvance-restored --web-port 3200 --start
```

El respaldo contiene material sensible: protégelo fuera de Git y conserva `.env`
por separado. Para un reset deliberado, `scripts/reset-local.ps1` detecta el nombre
Compose o recibe `-Project`, muestra el inventario exacto y genera un plan. Borrar
requiere otro comando con `-Plan` y la confirmación literal indicada; conserva
`.env` y rechaza un inventario cambiado. Reset destruye los datos del proyecto;
no es un paso de actualización.

El benchmark reproducible se ejecuta con
`python scripts/tests/benchmark_cycle.py`, siempre en un proyecto desechable.
El perfil variado de 50.000 filas y cuatro columnas completó nominalmente 100 MiB
(106.194.531 bytes), con Parquet de archivo de 79.145.600 bytes. Ese ensayo usó
overrides locales y no eleva los defaults del producto. 500 MiB, 1/2/5 GiB no se
ejecutaron por presupuesto; no son capacidades certificadas ni fallos medidos.
Tiempos, memoria, temporal, fuentes SQL y limitaciones están en
[el informe de volumen](docs/development/volume-benchmark.md).

## Desarrollo directo opcional

Para depurar sin contenedores se conserva el lanzador de procesos con SQLite.
Requiere Python 3.12 o 3.13, Node.js 20.19+ o 22.12+, pnpm 11 y uv. Si falta uv,
instálalo con `python -m pip install uv`. Se instalan las dependencias bloqueadas:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1
```

Con las dependencias ya instaladas se puede añadir `-SkipInstall`. Para detener
únicamente los procesos de este lanzador:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop-local.ps1
```

Este modo guarda base, archivos y logs en `.local/`, excluido de Git. Es una
facilidad de desarrollo distinta de la instalación PostgreSQL en Compose;
no comparten datos. Detén una de ellas si ambas utilizan el mismo puerto.

## Desarrollo y pruebas

```sh
cd backend
uv sync --frozen
uv run pytest tests ../scripts/tests
uv run ruff check src tests ../scripts
uv run mypy src/trackvance --ignore-missing-imports --check-untyped-defs

cd ../frontend
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm test:e2e
```

El ciclo completo Docker con datos aislados y limpieza automática se ejecuta
desde la raíz con `python scripts/tests/docker_e2e_cycle.py`. La certificación de
Conexiones añade `python scripts/tests/connections_cycle.py --full-playwright`
con PostgreSQL y SQL Server reales. Consulta los conteos y resultados vigentes en
[validación](docs/development/validation.md); los informes anteriores conservan
sus resultados históricos.

Con API, worker e interfaz iniciados:

```sh
python scripts/smoke_test.py --base-url http://localhost:3000
```

La prueba de integración crea nuevos datasets identificados como
`Verificación ...`. No borra ni reinicia datos existentes.

El modo directo ofrece OpenAPI en http://127.0.0.1:8000/docs. Compose publica la
web y su proxy, sin exponer el puerto 8000 al host. El contrato está en
[backend/API_CONTRACT.md](backend/API_CONTRACT.md) y [backend/openapi.json](backend/openapi.json).
Las dependencias resueltas se registran en `backend/uv.lock` y
`frontend/pnpm-lock.yaml`.

## Estructura

Las capas se organizan por responsabilidades dentro del monolito; la API y el
worker comparten modelos y servicios. Los puertos permiten sustituir la
infraestructura sin cambiar las reglas. El [mapa de arquitectura](docs/architecture.md)
identifica los archivos y los límites pendientes de esa separación.

```text
backend/       API, persistencia, procesamiento, worker y pruebas
frontend/      SPA React + TypeScript + Vite
demo/          Datos ficticios de demostración
deploy/        Imagen de interfaz y proxy Nginx
docs/          Decisiones y alcance
scripts/       Inicio, parada, bootstrap y prueba de integración
compose.yml    Entorno PostgreSQL local
```

## Límites de esta entrega

- Administración local de usuarios y cinco roles base; OIDC/SSO, roles arbitrarios
  y administración de múltiples organizaciones quedan fuera de esta entrega.
- Archivos CSV/TXT UTF-8, XLSX, JSON y Parquet de máximo 10 MiB, 100.000
  filas y 100 columnas. La inspección usa una muestra de hasta 100 filas salvo
  el esquema embebido de Parquet; la carga completa sigue siendo síncrona.
- Carga y perfil inicial acotados y síncronos; ejecuciones de módulos en worker.
- Reglas declarativas portables con compiladores Polars y DuckDB; ReconOps
  incluye comparación exacta, tolerancias numéricas/temporales y agregaciones
  1:N/N:1 SUM/COUNT. La ejecución completa de runs usa Polars/Python.
- PySpark tiene una decisión de planificación y rechazo explícito cuando se
  requiere, pero no un adaptador de ejecución instalado. Redis/Celery, conectores
  adicionales, object storage y OIDC/SSO son evoluciones preparadas o de producto.
- El despliegue local usa cola persistente en PostgreSQL, leases y heartbeat.
  El scheduler depende de ese worker; no corre con Docker detenido. Los resultados
  de volumen, backup/restore y máximos realmente certificados se publican en
  [validación](docs/development/validation.md). No se extrapolan garantías de
  producción ni volúmenes no ejecutados.

## Si algo no inicia

- **Docker:** consulta `docker compose ps` y `docker compose logs api worker web`.
- **Puerto ocupado:** detén la instancia que lo utiliza o ajusta puerto y origen web.
- **Modo directo sin API:** revisa `.local/api.err.log`.
- **Ejecución en cola:** revisa los logs del worker y Configuración; en modo directo,
  `.local/worker.err.log`.
- **Interfaz no disponible:** revisa `.local/web.err.log` y reinstala las
  dependencias con `pnpm install --frozen-lockfile` en `frontend/`.
- **Docker no disponible:** inicia Docker Desktop y comprueba `docker info`;
  el modo directo funciona independientemente de Docker.

Software propietario de Trackvance Colombia SAS. Uso local de desarrollo.
