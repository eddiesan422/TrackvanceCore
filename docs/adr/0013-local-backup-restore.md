# ADR 0013: respaldo, restauración y reset local verificables

- Estado: implementado para Docker Compose y SQLite local.
- Fecha: 2026-09-19.

## Decisión

El estado Docker se respalda como un conjunto coordinado, no como copias sueltas:
dump custom de PostgreSQL, ArtifactStore, credenciales cifradas, clave maestra y
una huella de todos los registros, artifacts, secretos y relaciones de linaje.
`scripts/docker_state.py` es la interfaz de inventario, backup, verificación,
restauración y reset. `scripts/backup_local.py` conserva el mismo principio para
el modo SQLite.

Un backup Docker bloquea primero la entrada web y el scheduler, detiene el worker,
exige que no queden runs/jobs `QUEUED` o `RUNNING`, toma la huella desde la API y
detiene la API. PostgreSQL permanece activo para producir `pg_dump --format=custom`.
Los tres volúmenes de archivos se montan de solo lectura, sin red, y aceptan solo
directorios y archivos regulares. El `finally` vuelve a iniciar exclusivamente los
contenedores que estaban activos. Una captura parcial se conserva para diagnóstico,
pero no obtiene un manifest válido.

El manifest enumera SHA-256, tamaños, modos y entradas de cada archivo; revisión
Alembic, IDs de imagen y estado quiescente. Excluye `.env`, URL de base de datos,
contraseñas, contenido de la clave, referencias de secretos y rutas absolutas del
host. El backup contiene material sensible aunque el manifest no lo muestre: debe
almacenarse con acceso restringido y nunca publicarse como artifact de CI.

## Restauración y prueba

La restauración valida el manifest, todos los hashes y la seguridad de rutas antes
de crear recursos. Solo admite un proyecto `trackvance-...` nuevo y distinto del
origen. Extrae los tres volúmenes sin seguir enlaces, restaura PostgreSQL en una
transacción y arranca primero la API sin seed. `verify_storage.py` vuelve a leer
todos los registros, recalcula SHA de artifacts, resuelve todos los FK y extremos
polimórficos, valida alcance por organización y descifra cada secreto sin imprimirlo.
La huella debe ser exactamente igual a la capturada.

El drill `scripts/tests/docker_backup_cycle.py` usa proyectos de aplicación fuente
y destino desechables, además de una PostgreSQL externa independiente. Crea por API
una conexión real, snapshot y run; respalda, elimina el proyecto fuente antes de
restaurar, y en el destino prueba la credencial restaurada, refresca el snapshot y
ejecuta Intake. La base externa se conserva hasta terminar esa validación. El HTML
servido comprueba integración web básica; Playwright sigue siendo una certificación
separada. El runner elimina únicamente sus proyectos inventariados.

El drill del 19 de septiembre de 2026 terminó `PASS`: comparó 124 relaciones,
siete artifacts y un secreto, verificó hashes, dump y migración `0007`, destruyó
la aplicación fuente antes del restore y consumió de nuevo la credencial restaurada.
La evidencia saneada está en
`.codex-local/recovery/trackvance-recovery-src-11200-d30f76-to-trackvance-recovery-dst-11200-d30f76/result.json`;
declara HTML 200 y Playwright `NOT_RUN`, sin confundir ambas comprobaciones.

## Reset explícito

El reset es deliberadamente un protocolo de dos pasos. `plan-reset` guarda IDs y
nombres exactos de contenedores, volúmenes y redes con etiquetas Compose, nonce,
expiración y hash del plan. `reset` exige la cadena literal
`RESET:<proyecto>:<12 caracteres del hash>`, vuelve a inventariar y aborta si cambió
un solo recurso. Después elimina solamente esos IDs; no usa glob, `down -v` ni
`prune`. `scripts/reset-local.ps1` expone el mismo flujo para PowerShell. Hacer un
backup antes es recomendable, no un requisito impuesto por el comando.

## Consecuencias

- Un dump sin los tres volúmenes, o secretos sin su clave, no es restaurable y no
  se declara backup Trackvance completo.
- Una restauración fallida deja el proyecto nuevo detenido para diagnóstico; nunca
  sustituye ni elimina el origen.
- Los recibos de restore/reset quedan fuera de los volúmenes afectados.
- La consistencia depende de una ventana corta sin ingreso ni ejecución. Una futura
  operación sin pausa requerirá snapshots coordinados del motor y del object store.
