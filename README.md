# Trackvance Core

## Ciclo de correcciones 0.2.0

El prototipo conserva el monolito modular FastAPI/React, PostgreSQL, worker
y almacenamiento local. Incluye perfiles observados sin trim implícito,
identificadores con ceros iniciales, reglas declarativas ampliadas, salidas
Intake en Parquet, evidencia v2 y reportes Excel estructurados con openpyxl.
Las configuraciones publicadas, ejecuciones y archivos históricos permanecen
inmutables; la migración nueva es `0002_evidence_v2`.

Documentación del ciclo:

- [Semántica y catálogo de reglas](docs/rules-catalog.md).
- [Manifiestos, identidad y compatibilidad](docs/adr/0002-evidence-and-artifacts.md).
- [Contenido y seguridad de los Excel](docs/exports-xlsx.md).
- [Validación y pendientes explícitos](docs/development/validation.md).
- [Operación, Docker y respaldo](docs/development/operations.md).
- [OpenAPI 0.2.0](backend/openapi.json) y [contrato HTTP](backend/API_CONTRACT.md).

Docker Compose con PostgreSQL se comprobó en `http://localhost:3100` usando
el proyecto aislado `trackvance-certification`; el puerto por defecto sigue
siendo 3000. `WEB_PORT` y `TRACKVANCE_WEB_ORIGIN` deben corresponderse.

Prototipo local de la plataforma de confiabilidad de datos de Trackvance
Colombia SAS. Integra Data Intake Gateway, ReconOps, Data Sentinel,
Excepciones y Auditoría en una misma aplicación.

Basado en **Trackvance Core — Especificación Técnica v1.1**. Esta primera
entrega es funcional, pero no representa la implementación completa de las
52 páginas del documento. Consulta [el alcance](docs/development/prototype-scope.md)
y [la decisión de arquitectura](docs/adr/0001-local-prototype.md).
Los resultados de las pruebas están en [verificación local](docs/development/validation.md).

## Inicio rápido en Windows

Requisitos: Python 3.12 o 3.13, Node.js 20.19+ o 22.12+, pnpm 11 y uv.
Si falta uv, instálalo con `python -m pip install uv`. El instalador usa los
archivos de bloqueo para conservar las versiones de las dependencias.

Desde la raíz de este repositorio:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1
```

Abre **http://localhost:3000** y selecciona **Entrar al entorno demo**.
La primera apertura prepara una organización ficticia y datasets de ejemplo.
Las ejecuciones se procesan en Python; las cifras de la interfaz provienen de
la API y de los datos guardados.

Si las dependencias ya están instaladas:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/start-local.ps1 -SkipInstall
```

Para detener únicamente los procesos iniciados por este proyecto:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/stop-local.ps1
```

Los datos no se borran al detenerlo. La base SQLite, los archivos y los
registros de ejecución quedan en `.local/`, excluido de Git.

## Recorrido de demostración

1. **Centro de control:** revisa datasets, ejecuciones y atención pendiente.
2. **Datasets:** abre un ejemplo y revisa versiones, esquema y perfil, o crea
   un dataset y carga tu CSV UTF-8 (coma o punto y coma).
3. **Intake:** selecciona un contrato y una versión, ejecuta la validación y
   revisa decisión, errores y salida válida.
4. **ReconOps:** compara dos versiones con claves y tolerancia; filtra
   coincidencias, diferencias, faltantes, duplicados y registros inválidos.
5. **Excepciones:** convierte un hallazgo en caso, investígalo y documenta su
   causa y resolución. Cada cambio conserva su evento en el historial.
6. **Sentinel:** ejecuta un monitor y revisa cada check con su valor observado
   y esperado. La ejecución conserva su historial y evidencia descargable.

Cada ejecución completada ofrece un informe Excel y manifiesto JSON. `SUCCESS`
indica que el procesamiento terminó: los datos pueden contener diferencias o
haber sido rechazados por Intake.

## Docker Compose

Requiere Docker Desktop iniciado con contenedores Linux. El bootstrap crea
`.env` si falta y genera una contraseña local para PostgreSQL:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/bootstrap.ps1
```

En otros sistemas, copia `.env.example` a `.env`, reemplaza
`POSTGRES_PASSWORD` por una contraseña local alfanumérica y ejecuta:

```sh
docker compose up --build -d --wait
```

Servicios: `web` en localhost:3000, API, worker estándar y PostgreSQL.
Los archivos y los metadatos persisten en volúmenes Docker. No expone
PostgreSQL ni la API al exterior. Detener con `docker compose down` conserva
los volúmenes. No uses `down -v` si quieres conservar los datos.

El modo directo y Compose son instalaciones separadas; no comparten su base
de datos. Detén el modo directo antes de iniciar Compose en el mismo puerto.
Compose ha sido verificado con PostgreSQL 16 y Docker Engine 29.1.3. El build
inicial descarga dependencias; con las imágenes preparadas la aplicación opera
sin servicios externos. Consulta la evidencia de operación y sus límites.

## Desarrollo y pruebas

```sh
cd backend
uv sync --frozen
uv run pytest
uv run ruff check src tests
uv run mypy src/trackvance --ignore-missing-imports --check-untyped-defs

cd ../frontend
pnpm install --frozen-lockfile
pnpm lint
pnpm typecheck
pnpm test
pnpm build
pnpm test:e2e
```

Con API, worker e interfaz iniciados:

```sh
python scripts/smoke_test.py --base-url http://localhost:3000
```

La prueba de integración crea nuevos datasets identificados como
`Verificación ...`. No borra ni reinicia datos existentes.

La API local ofrece OpenAPI en http://127.0.0.1:8000/docs. El contrato de este
primer corte está en [backend/API_CONTRACT.md](backend/API_CONTRACT.md).
Las dependencias resueltas se registran en `backend/uv.lock` y
`frontend/pnpm-lock.yaml`.

## Estructura

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
- CSV UTF-8, máximo 10 MiB, 100.000 filas y 100 columnas. XLSX pendiente.
- Carga y perfil inicial acotados y síncronos; ejecuciones de módulos en worker.
- Subconjunto de reglas y comparación decimal por claves. PySpark, DuckDB,
  comparación 1:N y DSL completa pendientes.
- Los procesos estándar y SQLite permiten revisar el producto. Las garantías
  de operación continua, concurrencia, recuperación y gran volumen todavía
  requieren ampliación y pruebas.

## Si algo no inicia

- **Puerto ocupado:** detén la instancia anterior con `scripts/stop-local.ps1`.
- **Sin conexión a la API:** revisa `.local/api.err.log`.
- **Ejecución en cola:** revisa `.local/worker.err.log` y Configuración.
- **Interfaz no disponible:** revisa `.local/web.err.log` y reinstala las
  dependencias con `pnpm install --frozen-lockfile` en `frontend/`.
- **Docker no disponible:** inicia Docker Desktop y comprueba `docker info`;
  el modo directo funciona independientemente de Docker.

Software propietario de Trackvance Colombia SAS. Uso local de desarrollo.
