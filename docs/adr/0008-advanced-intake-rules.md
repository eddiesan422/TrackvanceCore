# ADR 0008 — Reglas avanzadas y referencias inmutables

Fecha: 2026-09-19. Estado: implementado en 0.4.0; certificación registrada en validation.

## Decisión

Se extiende RuleDefinition y los compilers portables existentes; no se crea
otro motor ni se envía código de usuario a Python/SQL. Nuevas reglas:
COMPOUND_UNIQUE, LENGTH, COLUMN_COMPARE y REFERENCE. TYPE y NOT_NULL ya
existían en backend y se exponen en el builder junto a las demás reglas.
Todos los selectores toman columnas de metadata del DatasetVersion.

`when` es un árbol declarativo acotado de condiciones all/any y comparaciones
tipadas. La máscara de aplicabilidad es independiente del resultado. Una fila
excluida no suma a evaluadas ni fallidas. `IGNORE` aplica esta exclusión a nulls;
ALLOW los considera conformes; FAIL los reporta. REQUIRED/NOT_NULL no omiten
nulos. Unicidad condicional considera sólo la población aplicable.

PolarsCompiler y DuckDBCompiler comparten preparación determinística de
hechos (tuplas, longitud Unicode, Decimal, fecha, referencia), y ejecutan el
predicado final en su engine nativo. No hay eval, exec, SQL arbitrario ni
conexiones externas desde ProcessingEngine. Se conserva el contrato validate
que devuelve máscara booleana y se agrega evaluate con máscaras passed/evaluated.

Referencias fijan un DatasetVersion y columnas ordenadas. La publicación
valida organización y esquema. El servicio resuelve snapshots a través de
StorageProvider, verifica su integridad y construye un contexto de frames
normalizados. ExecutionPlanner incluye esos inputs en su estimación. El run
conserva ArtifactLink RUN_REFERENCE y manifest.references con hashes.
Actualizar el catálogo externo no altera ejecuciones/configuraciones previas.

## Identidad y compatibilidad

Sólo publicación asigna rule_id cuando no existe. Una nueva versión conserva
las identidades enviadas por su versión anterior. Findings nuevos utilizan
SHA-256 de `rule:<rule_id>`; reglas históricas y shorthand conservan su
fingerprint previo. El lector no asigna IDs retroactivos ni cambia artifacts.
Una regla no evaluada no prueba corrección de una excepción.

Para nuevas resoluciones de casos abiertos legacy se identifica la regla mediante
código/columna y el fingerprint compatible, pero también se exigen métricas de
evaluación positiva y cero fallos. Si el candidato histórico no permite identificar
inequívocamente esa regla o no contiene sus contadores, se bloquea el nuevo cierre
por evidencia insuficiente y debe ejecutarse nuevamente el mismo control. Los
casos ya RESOLVED y sus pruebas históricas permanecen intactos.

Se conserva schema_version 2 del contenedor de configuración: nuevos campos
son aditivos y las reglas declaran tipos explícitos. Los manifests históricos
siguen leyéndose y las nuevas referencias son metadata adicional. No se
requiere migración SQL para reglas porque Configuration ya guarda snapshots.

## Límites y evidencia

Condiciones: 4 niveles, 64 nodos, 10 hijos por grupo. Claves: hasta 100 campos.
Referencias sólo a snapshots locales de la misma organización. Longitud usa
puntos de código, no bytes ni grafemas, sin normalización silenciosa.
Regex conserva su subset portable y límites previos. La UI permite condiciones
simples y conserva árboles compuestos definidos por API.

Métricas por regla incluyen evaluated_count, skipped_count, failed_count,
severity y rule_id. Filas de evidencia preservan valores originales, columnas,
parámetros y condición. Accepted Parquet contiene transformaciones explícitas
y excluye únicamente filas con ERROR. XLSX incluye excluidas/severidad y
trazabilidad de referencias. Las pruebas de reglas avanzadas verifican paridad,
Unicode, nulls, Decimal preciso, organización, identidad, lineage y export.
