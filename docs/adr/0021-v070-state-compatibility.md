# ADR 0021: Compatibilidad verificable del estado 0.7.0

- Estado: aceptada
- Fecha: 2026-10-03

## Contexto

0.7.0 añade adquisición asíncrona, automatizaciones, consumidores de eventos, una bandeja personal y una identidad explícita para Sentinel. Una restauración debe conservar todos los datos históricos y comprobar también el estado nuevo. Excluir tablas desconocidas o todas las columnas añadidas ocultaría pérdidas reales.

## Decisión

La huella nativa corregida usa `schema_version=7`, revisión Alembic
`0016_acquisition_diagnostics` e inventario completo de 42 tablas. La publicación
inicial 0.7.0 usa state 6/0015 y continúa siendo baseline auténtica soportada.
El catálogo físico debe coincidir con el ORM del runtime. La huella incluye
cada adquisición, Job, programación, ocurrencia, outbox, consumo, notificación
y decisión. Revisión/tabla/campo desconocido impide certificar el estado.

C02 añade únicamente error_details/error_reference nullable, sin backfill de
historia. `snapshot-legacy-v6` valida las 42 tablas y elimina sólo ambos campos
si son NULL; exige igualdad exacta del fingerprint anterior, incluidos errores,
áreas, opciones/numeración/límites y read_at. No exige tablas asíncronas vacías
para un backup state6. Diagnósticos nuevos no se pueden esconder como adiciones.

Para restaurar una huella 0.6.1 (`schema_version=5`, `0012_delivery_target_audit`),
la herramienta aplica 0013–0016 y genera `snapshot-legacy-v5`, encadenando primero
la proyección estricta de diagnósticos. Esta baseline sí exige once tablas
nuevas vacías; prohíbe Jobs de adquisición y Runs de preflight. Sólo elimina
jobs.acquisition_id NULL y columnas Sentinel con identidad comprobada contra
el actor histórico de la organización. Las proyecciones v2–v4 mantienen sus guards.

La migración no inventa responsables. Las revisiones con un actor User verificable adoptan ese mismo ID. Una programación actual sin responsable se pausa y conserva su anterior `enabled` en `legacy_enabled_before_identity`. La proyección histórica revierte exclusivamente esa pausa, exige que la bandera sea booleana y que la programación siga deshabilitada. Una asignación posterior, nueva actividad o una bandera inesperada impide usar la proyección. El informe nativo conserva la pausa y ambas columnas nuevas.

Las proyecciones congeladas de 0.4.1, 0.5.0 y 0.5.1 primero verifican estas condiciones y después aplican sus reglas históricas explícitas de identidad y Delivery. La comparación usa el mismo inventario, hashes por registro y contadores; no borra tablas arbitrarias ni compara una selección parcial.

El respaldo Docker reconoce la instalación histórica de cinco servicios y la instalación nueva completa de nueve. Antes de tomar la huella detiene web, scheduler, los dos consumidores y los tres workers; después detiene API para `pg_dump` y los volúmenes. Reinicia sólo los contenedores que estaban activos. Fuente y destino mantienen almacenes de secretos y claves separados, privados y verificados.

Un dataset Parquet particionado conserva su descriptor original y las rutas relativas de cada parte. Se verifican descriptor, inventario de partes, organización, ordinal, filas, tamaños y hashes antes del respaldo y después de restaurarlo. Las rutas absolutas de la metadata se reubican; los bytes y hashes históricos del descriptor y de las partes permanecen idénticos. Los uploads recibidos o registrados también forman parte del respaldo local y su material se comprueba al tomar la huella.

## Consecuencias y verificación

El formato del manifest Docker sigue siendo 2 y el local SQLite sigue siendo 3: no necesitan una conversión artificial. La revisión e inventario de la huella distinguen el estado real. Las pruebas focalizadas cubren las proyecciones, pérdida o incorporación de tablas, pausa Sentinel, exclusión mutua del Job, quiescencia de nueve servicios, secretos y restauración de particiones sin modificar bytes. El checker PostgreSQL corregido ejercita 0008→0016 y conserva 0012→0016; añade 0015→0016→0015→0016 con las 42 tablas pobladas y compara exactamente la proyección state 6. Comprueba paridad física con el ORM. Los resultados ejecutados sobre 0015 de la publicación inicial se conservan como antecedentes separados.
