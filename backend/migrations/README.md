# Migraciones de Trackvance Core

La revisión `0001_initial` congela el esquema inicial mediante operaciones explícitas
de Alembic. No carga datos demo, no inicia workers y no depende de cambios futuros
en los modelos para reconstruir esa revisión.

Desde `backend`, con las dependencias instaladas y `DATABASE_URL` apuntando a una
base vacía:

```text
uv run alembic upgrade head
uv run alembic current
uv run alembic check
```

Se admite el SQLite local predeterminado y PostgreSQL mediante
`postgresql+psycopg://...`. La configuración de almacenamiento y conexión es la
misma que usa la aplicación. El arranque del prototipo conserva `create_all` para
su primera ejecución local; este método no sustituye migraciones de esquemas ya
existentes. Para evolucionar una instalación, aplicar `upgrade head` antes de
iniciar la API y el worker.

Si una base ya fue creada por el arranque del prototipo **y corresponde exactamente
al esquema inicial**, adoptar el historial una única vez, después de respaldar y
comprobar su esquema:

```text
uv run alembic stamp 0001_initial
uv run alembic check
uv run alembic upgrade head
```

`stamp` solamente registra la revisión: no crea ni corrige tablas. No usarlo para
omitir una migración pendiente en una base con estructura distinta.

Para una modificación futura de modelos, generar una nueva revisión con
`uv run alembic revision --autogenerate -m "descripcion"`, revisar las operaciones
y aplicar `upgrade head`. No modificar la revisión inicial después de adoptarla.

La reversión `uv run alembic downgrade base` elimina las tablas y sus datos; se
reserva para bases desechables de verificación. Las pruebas de esta revisión usan
una base SQLite temporal para comprobar upgrade, ausencia de diferencias con los
modelos, downgrade y segundo upgrade sin alterar los datos de la aplicación.
