# Borrado operativo selectivo excepcional (0.8.5)

La herramienta `scripts/cleanup_operational.py` recibe los IDs exactos de datasets de prueba confirmados por el operador. No forma parte del arranque ni de las migraciones; CI la ensaya sólo en una copia sintética restaurada propia, y nunca selecciona todos los datasets por nombre, prefijo o fecha. Su uso en la instalación habitual sólo procede después de las etapas de certificación, recuperación y promoción autorizadas.

## Contrato y alcance

`plan` lee metadata y archivos, calcula hashes completos, enumera los IDs dependientes de cada dataset, conserva sus excepciones y produce un plan privado sellado que caduca en quince minutos. El wrapper agrega al sello los IDs reales de contenedores, volúmenes y redes. No modifica metadata ni archivos operativos. Crea y elimina exclusivamente su propio contenedor temporal, identificado por nombre aleatorio, etiqueta de ownership e ID.

`apply` necesita ese plan, un backup nativo íntegro, su recibo de restauración real y una identidad de administrador activo. El recibo debe ser schema 1, `STOPPED_VERIFIED`, pertenecer a un proyecto de restore distinto y enlazar los SHA-256 de los bytes exactos de `backup-manifest.json` y `state.json`. Además, cada archivo seleccionado debe existir dentro del archivo `trackvance_data` del backup con tamaño y hash originales. El backup debe corresponder al proyecto y revisión, y sus hashes de **todas las filas y tablas** deben coincidir con el plan. El wrapper verifica de nuevo el inventario Docker vivo al iniciar, antes de los locks, antes del commit y al terminar. Sólo PostgreSQL puede permanecer activo en el proyecto. El backend exige además que no existan trabajos/leases activos ni programaciones habilitadas. La herramienta no detiene servicios ni pausa programaciones por su cuenta.

La selección incluye versiones, runs terminales, configuraciones y sus revisiones/programaciones dependientes por ID, adquisiciones, jobs, hallazgos/casos/adjuntos, artefactos y sus partes/linaje, eventos/avisos internos y definiciones/contextos/ejecuciones de Reportes pertenecientes a las fuentes seleccionadas. Los consumidores mixtos o inciertos protegen su rama completa. Cada DELETE usa un ID concreto y ordena hijos antes de padres. No usa TRUNCATE, borrados de tablas completas, reset, prune ni borrado de raíces/volúmenes.

Usuarios, roles/permisos, sesiones/identidades, secretos y sus almacenes, conexiones/destinos y sus revisiones, macrodominios/dominios/glosario/personas, auditoría, bloques y dependencias de seguridad, políticas/guards/decisiones de destinos se conservan. UNKNOWN y una barrera de destino protegen el run y toda su rama. No se prueba una conexión, no se ejecuta SQL remoto y no se cambia una tabla de destino externo.

## Transacción y cuarentena

En PostgreSQL la revalidación y los DELETE ocurren bajo locks de las tablas de metadata, dentro de una única transacción. En SQLite las pruebas usan `BEGIN IMMEDIATE`. Se revalidan filas, schema físico, dependencias y archivos completos antes de mover o borrar. Cualquier drift obliga a generar un plan nuevo.

La cuarentena es un directorio nuevo explícito, fuera del almacenamiento vivo y del backup; el usuario de la imagen debe poder escribir en su directorio padre y leer el backup privado. Cada archivo seleccionado tiene que ser regular, permanecer bajo el almacenamiento vivo y conservar SHA-256. Los enlaces simbólicos, rutas externas y archivos compartidos con registros conservados se rechazan. Se prefiere rename; entre mountpoints se usa copia exclusiva, fsync y verificación del hash antes de retirar el original. El journal incluye organización, revisión y SHA-256 del contenido sellado; se guarda antes de esos movimientos. En Linux se sincronizan tanto archivos como directorios del journal, promociones y movimientos. El wrapper rechaza aliases físicos y contención de rutas entre backup y cuarentena antes de crear el helper. No se destruye la cuarentena después del commit.

Un fallo antes del commit revierte los DELETE y restaura los archivos movidos. Para una interrupción abrupta, `recover` consulta el marcador de auditoría: si el commit ocurrió, mantiene la cuarentena; si no ocurrió, sólo restaura archivos con referencias de metadata todavía vivas y hash verificable. Una copia parcial cuyo original sigue íntegro permanece en cuarentena y se informa en el recibo. Los conflictos dejan un recibo `RECOVERY_REQUIRED` y requieren inspección y recuperación desde el backup; no se sobrescribe una ruta viva ni se restaura una copia que no coincide con el hash original. `recover` necesita una sesión nueva y revalida metadata bajo locks y quiescencia antes de restaurar. El backup siempre se conserva.

## Invocación

Ejecutar con el Python que tenga las dependencias del repositorio, desde la raíz del checkout certificado. Los parámetros siguientes son marcadores que deben sustituirse por valores revisados:

```text
python scripts/cleanup_operational.py plan --project PROJECT --image sha256:IMAGE_DIGEST --source-commit COMMIT --organization-id ORGANIZATION --dataset-id CONFIRMED_TEST_ID --output PRIVATE_PLAN.json
python scripts/cleanup_operational.py apply --project PROJECT --image sha256:IMAGE_DIGEST --source-commit COMMIT --plan PRIVATE_PLAN.json --backup VERIFIED_BACKUP --restore-receipt VERIFIED_STOPPED_RESTORE.json --actor-id ACTIVE_ADMIN --quarantine-parent PRIVATE_QUARANTINE_PARENT --quarantine-name UNIQUE_DIRECTORY --output PRIVATE_RECEIPT.json
python scripts/cleanup_operational.py recover --project PROJECT --image sha256:IMAGE_DIGEST --source-commit COMMIT --quarantine-parent PRIVATE_QUARANTINE_PARENT --quarantine-name EXISTING_DIRECTORY --output PRIVATE_RECOVERY_RECEIPT.json
```

La imagen debe ser un digest inmutable con las etiquetas 0.8.5 y el commit certificado. No se hace pull. El helper tiene memoria 512 MiB, 0.5 CPU, 128 PIDs, rootfs de sólo lectura y capabilities retiradas. Recibe la URL de PostgreSQL por un pipe privado; no aparece en argv/env Docker, logs ni recibos. Sólo monta el volumen operativo propio, scripts de verificación de sólo lectura, backup de sólo lectura y padre de cuarentena explícito. El stdout es un protocolo JSON limitado a solicitudes de inventario y resultados seguros.

## Evidencia y límites

Los tests sintéticos verifican plan serializado sin mutación, dependencias/configuraciones/programaciones concretas, conservación de accesos/personas/catálogos/auditoría, rechazo de drift de filas/archivo/schema/backup/expiración, UNKNOWN/barreras/consumidores mixtos, rollback inyectado, copia entre mountpoints y recuperación precommit. Las pruebas del wrapper simulan inventarios y el protocolo privado; nunca llaman a Docker real.

El escenario obligatorio `catalog-selective-operational-cleanup085` llama al wrapper sobre el restore propio `trackvance-v080-test-restore085-*`: pobla metadata sintética local sin workers ni conexiones remotas, hace un backup consistente fuera del storage, verifica una segunda restauración detenida y luego ejecuta plan, fallo antes de commit, fallo abrupto, recuperación en sesión nueva y apply. Compara todos los hashes de filas conservadas, comprueba FKs y procedencia congelada mediante SQL de sólo lectura, verifica artefactos conservados y archivos en cuarentena, compara byte a byte los cuatro volúmenes de credenciales/claves y exige exactamente una auditoría nueva enlazada al plan. Los backups, cuarentenas y recibos quedan conservados fuera del storage operativo.

Los tests host del harness no declaran un resultado PostgreSQL real ni autorizan aplicar el plan en la instalación habitual. El recibo nativo sólo se produce cuando el ensayo Linux con la imagen certificada termina realmente. La capacidad del host, la quiescencia viva, los permisos de los mounts y la verificación de backup deben demostrarse en esa fase. Toda incongruencia falla antes de commit y queda registrada por código seguro, sin contenido privado.
