# ADR 0009 — Conciliación configurable por columna y agregaciones explícitas

Fecha: 2026-09-19. Estado: implementado en 0.4.0; certificación registrada en validation.

## Decisión

Se conserva el reconciliador existente y su separación entre hallazgos de
negocio y fallos técnicos. Claves simples/compuestas y comparaciones exactas,
numéricas absolutas/porcentuales y temporales ya existen. Se amplía su
configuración y evidencia para escenarios reales sin matching difuso ni N:M.

Cada fuente puede declarar transforms ordenados (los mismos de Intake).
Se aplican a una copia de su frame antes de normalización de claves,
agrupación y comparación. Nunca reescriben snapshots. La normalización de
valores exactos sigue siendo independiente por comparación.

`aggregations` contiene SUM/COUNT para un único lado. SOURCE es N:1 y TARGET
es 1:N. Cada output_column es distinto y no puede sobrescribir una clave.
Las comparaciones del lado agrupado sólo pueden usar claves u outputs
agregados. Se elimina así la selección implícita del primer miembro del grupo
en configuraciones nuevas. SUM exige que todos los miembros sean numéricos;
si alguno no lo es, el valor agregado inválido genera INVALID. COUNT cuenta
registros. Las líneas de todos los miembros permanecen en la evidencia.

La forma histórica `aggregation` se adapta al publicar sin modificar su
snapshot anterior. Ejecuciones históricas conservan su semántica original.
La agregación de ambos lados y matching probabilístico quedan fuera de alcance.

## Nulos, métricas y acceso

`null_policy` por comparación permite MATCH_NULLS, MISMATCH e INVALID. Si se
omite, se conserva `equal_nulls` histórico. Null y texto vacío son diferentes.
Tolerancia porcentual conserva denominador SOURCE/TARGET/MAX_ABS y política
EXACT_ONLY en cero. Decimal no pasa por float.

La UI selecciona claves/campos desde ambos esquemas y ofrece los outputs
agregados como columnas comparables. Muestra todas las comparaciones por
resultado y métricas evaluadas/fallidas/inválidas por comparación. El Excel
describe transformaciones, agregaciones, políticas, detalles y ambos inputs.
Los manifests incluyen configuración efectiva y diagnósticos reproducibles.

## Verificación

Pruebas portables cubren políticas de null y comparaciones existentes.
Nuevas pruebas cubren claves compuestas, transformaciones de Decimal con
separadores explícitos, múltiples SUM/COUNT N:1 y rechazo de columnas
ambiguas. E2E adicional publica la configuración desde UI y verifica
conciliación, detalle y líneas de origen. Se mantienen las clasificaciones
MATCH, VALUE_MISMATCH, SOURCE_ONLY, TARGET_ONLY, DUPLICATE_SOURCE,
DUPLICATE_TARGET e INVALID.
