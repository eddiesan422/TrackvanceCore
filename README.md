# Trackvance Core

## Ciclo de correcciones 0.3.0

El prototipo conserva el monolito modular FastAPI/React, PostgreSQL, worker
y almacenamiento local. Incluye perfiles observados sin trim implícito,
identificadores con ceros iniciales, reglas declarativas ampliadas, salidas
Intake en Parquet, evidencia v2 y reportes Excel estructurados con openpyxl.
Las configuraciones publicadas, ejecuciones y archivos históricos permanecen
inmutables. La capa de entrada admite CSV, Excel XLSX, JSON, Parquet y TXT
delimitado; la migración más reciente es `0004_exception_validation`.

Documentación del ciclo:

- [Arquitectura local y evolución del producto](docs/architecture.md).
- [Puertos de almacenamiento, fuentes, ejecución y cola](docs/adr/0006-architecture-ports.md).
- [Semántica y catálogo de reglas](docs/rules-catalog.md).
- [Manifiestos, identidad y compatibilidad](docs/adr/0002-evidence-and-artifacts.md).
- [Lectores y normalización de datasets](docs/adr/0004-dataset-readers.md).
- [Validación técnica de excepciones](docs/adr/0005-technical-validation-of-exceptions.md).
- [Contenido y seguridad de los Excel](docs/exports-xlsx.md).
- [Validación y pendientes explícitos](docs/development/validation.md).
- [Operación, Docker y respaldo](docs/development/operations.md).
- [OpenAPI 0.3.0](backend/openapi.json) y [contrato HTTP](backend/API_CONTRACT.md).
- [Especificación técnica v1.1 — revisión de implementación 0.3.0](docs/specification/Trackvance_Core_Especificacion_Tecnica_v1.1.pdf).

La instalación local `trackvance-certification` utiliza `http://localhost:3100`.
El puerto por defecto de una instalación nueva es 3000; `WEB_PORT` y
`TRACKVANCE_WEB_ORIGIN` deben corresponderse. Los resultados de cada ciclo están
en el documento de validación, separados de las capacidades preparadas.

Prototipo local de la plataforma de confiabilidad de datos de Trackvance
Colombia SAS. Integra Data Intake Gateway, ReconOps, Data Sentinel,
Excepciones y Auditoría en una misma aplicación.

Basado en **Trackvance Core — Especificación Técnica v1.1**. Esta primera
entrega es funcional, pero no representa la implementación completa de la
arquitectura objetivo del documento. Consulta [el estado de arquitectura](docs/architecture.md)
y [el alcance histórico](docs/development/prototype-scope.md).
Los resultados de las pruebas están en [verificación local](docs/development/validation.md).
La especificación oficial v1.1 tiene una [revisión de implementación 0.3.0](docs/specification/Trackvance_Core_Especificacion_Tecnica_v1.1.pdf)
de 26 páginas y conserva su [fuente editable](docs/specification/Trackvance_Core_Especificacion_Tecnica_v1.1.md) en el repositorio.

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
habilitado; en una instalación nueva puede preparar datos ficticios de ejemplo.

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
   validación y revisa decisión, errores y salida válida.
4. **ReconOps:** compara dos versiones con claves y tolerancia; filtra
   coincidencias, diferencias, faltantes, duplicados y registros inválidos.
5. **Excepciones:** convierte un hallazgo en caso, investígalo y envíalo a
   validación. Solo una ejecución posterior del mismo snapshot de configuración
   que confirme la corrección habilita **Resolver**. Los cierres administrativos
   (descartada, aceptada o no aplica) exigen un motivo y permanecen separados de
   una resolución técnica. El detalle enlaza la ejecución de origen y la de
   validación, y cada cambio conserva su evento en el historial.
6. **Sentinel:** ejecuta un monitor y revisa cada check con su valor observado
   y esperado. La ejecución conserva su historial y evidencia descargable.

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
desde la raíz con `python scripts/tests/docker_e2e_cycle.py`. El último ciclo
aprobó 233 pruebas backend/operaciones, 67 de componentes, 13 flujos E2E y
84 comprobaciones API, además de migraciones y persistencia tras restart.

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

- Acceso demo explícito para revisión local; administración completa de
  permisos y múltiples organizaciones pendiente.
- Archivos CSV/TXT UTF-8, XLSX, JSON y Parquet de máximo 10 MiB, 100.000
  filas y 100 columnas. La inspección usa una muestra de hasta 100 filas salvo
  el esquema embebido de Parquet; la carga completa sigue siendo síncrona.
- Carga y perfil inicial acotados y síncronos; ejecuciones de módulos en worker.
- Reglas declarativas portables con compiladores Polars y DuckDB; ReconOps
  incluye comparación exacta, tolerancias numéricas/temporales y agregación
  simple 1:N sum/count. La ejecución completa de runs usa Polars/Python.
- PySpark tiene una decisión de planificación y rechazo explícito cuando se
  requiere, pero no un adaptador de ejecución instalado. Redis/Celery, fuentes
  remotas, object storage y OIDC/SSO son evoluciones preparadas o de producto.
- El despliegue local usa cola persistente en PostgreSQL, leases y heartbeat.
  Escala horizontal, operación de gran volumen, backup PostgreSQL automatizado
  y garantías de producción requieren implementación y certificación propias.

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
