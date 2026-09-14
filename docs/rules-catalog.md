# Catálogo de reglas y reason codes — 0.2.0

Semántica detallada en [ADR 0003](adr/0003-rule-semantics.md). Toda publicación
es un snapshot inmutable schema 2; un cambio crea nueva Configuration version.

## Reglas por registro (Intake y Sentinel)

Forma: `{code?, type, column, severity: ERROR|WARNING, enabled: true,
parameters: {...}, message?}`. `code` ausente se deriva de type en mayúsculas.
Intake produce errores por registro; Sentinel produce un check con conteos
evaluados/fallidos para la misma expresión. `null_policy` es ALLOW/FAIL/IGNORE;
el shorthand legacy numeric/positive conserva FAIL. REQUIRED rechaza null y
texto vacío, sin considerar whitespace vacío cuando no se declara trim.

| Type / código | Parámetros principales | Semántica |
| --- | --- | --- |
| required / REQUIRED | columna | Campo presente no null/no vacío |
| not_null / NOT_NULL | columna | No null |
| unique / UNIQUE | null_policy | Todos los registros duplicados incumplen |
| numeric / NUMERIC | null_policy | Decimal finito con punto |
| positive / POSITIVE | null_policy | Decimal estrictamente mayor que cero |
| type / TYPE | logical_type | STRING/DECIMAL/INT64/DATE/TIMESTAMP/BOOLEAN |
| range / RANGE | gt o gte; lt o lte | Límites Decimal inequívocos |
| allowed_values / ALLOWED_VALUES | values (hasta 1000 escalares) | Pertenencia exacta sin normalización |
| regex / REGEX | pattern, flags i/m/s | Expresión portable Rust/RE2; máximo 500 caracteres |
| date_rule / DATE_RULE | not_future, min, max, timezone UTC | Fechas ISO; ahora UTC fijado al inicio del run |

Ejemplo de fecha no futura (puede utilizar código personalizado estable):

```json
{"type":"date_rule","code":"DATE_NOT_FUTURE","column":"transaction_date","parameters":{"not_future":true,"min":"2020-01-01","timezone":"UTC","null_policy":"FAIL"}}
```

## ReconOps

`key_columns` admite varias claves. `key_normalization` es obligatorio en el
snapshot nuevo y tiene default `{trim:false,case:"NONE",unicode_normalization:"NONE"}`.
Case: NONE/UPPER/LOWER; Unicode: NONE/NFC/NFKC.

`comparison_rules` contiene una o varias declaraciones, cada una con
`source_column`, `target_column`, `type` y `parameters`:

| Type / código | Parámetros | Resultado |
| --- | --- | --- |
| exact_compare / EXACT_COMPARE | normalization; equal_nulls false | Igualdad textual exacta tras normalización explícita |
| numeric_tolerance / NUMERIC_TOLERANCE | abs, percent opcional, denominator SOURCE/TARGET/MAX_ABS; zero_denominator EXACT_ONLY; equal_nulls | Comparación Decimal con umbral inclusivo |
| date_tolerance / DATE_TOLERANCE | hours o days; timezone UTC; equal_nulls | Diferencia en segundos contra umbral declarado |

`EXACT_MATCH` es alias legacy de NUMERIC_TOLERANCE y aparece indicado en
Biblioteca. Los campos históricos amount_column/tolerance se adaptan a esa
regla sin reescribir la configuración. Configurar abs=0 sigue siendo una
comparación numérica; `01` y `1` pueden coincidir, a diferencia de EXACT_COMPARE.

`aggregation: {side:"TARGET", operation:"sum", column:"amount",
output_column:"sum_amount"}` habilita 1:N simple; también count sin column.
Las comparaciones posteriores referencian output_column en el lado agregado.
AGGREGATE_COMPARE describe esta capacidad en Biblioteca, no un type adicional
de comparison_rules. La configuración completa se exporta.

Clasificaciones estables: MATCH, VALUE_MISMATCH, SOURCE_ONLY, TARGET_ONLY,
DUPLICATE_SOURCE, DUPLICATE_TARGET, INVALID. No son errores técnicos de run.
Detalle de comparación conserva regla, valores, diferencia, tolerancia y
parámetros; agregación conserva las líneas de todos los integrantes.

## Sentinel

Los controles legacy siguen disponibles: SCHEMA_REQUIRED, NULL_RATE,
FRESHNESS y VOLUME_CHANGE. Una columna ausente se distingue de una columna
con nulls; el escenario certificado conserva nueve checks.

| Type / código | Parámetros |
| --- | --- |
| distinct_count / DISTINCT_COUNT | column, min/max |
| distinct_rate / DISTINCT_RATE | column, min/max (fracción 0..1) |
| uniqueness_ratio / UNIQUENESS_RATIO | column, min/max (fracción 0..1) |
| schema_type / SCHEMA_TYPE | column; expected_type opcional, ausente compara contra versión anterior |
| metric_threshold / METRIC_THRESHOLD | metric, column opcional, operator gt/gte/lt/lte/eq, threshold o min/max |
| historical_band / HISTORICAL_BAND | metric, column opcional, window, min_history, iqr_multiplier, fallback_change_pct, min/max opcional |

`metric_method=EXACT_OBSERVED`, `metric_definition_version=2` identifica
perfiles nuevos. Los históricos v1 usan fallback LEGACY_NORMALIZED/1. No se
mezclan estas series. La banda indica MEDIAN_IQR_LINEAR_V1,
FIXED_THRESHOLD_FALLBACK o PREVIOUS_COMPATIBLE_METRIC en el resultado esperado.

## Overrides y transformaciones

Upload acepta multipart `column_overrides` como JSON, por ejemplo
`{"document_id":{"semantic_tag":"IDENTIFIER","logical_type":"STRING"}}`.
Un tipo numérico explícito se valida contra todos los valores, conservando
su representación original. Un override sobre columna ausente se rechaza.

Intake `transforms` es una lista ordenada de `{column,type,parameters}`.
Admite trim, case, unicode_normalization, empty_to_null, id_padding,
remove_characters, decimal_parse y date_parse. No hay transform implícito
durante profiling; las transformaciones funcionales sólo afectan al output
de un contrato que las declara.

## Estado técnico y decisiones

Runs: QUEUED, RUNNING, SUCCESS, FAILED, FAILED_PRECONDITION, CANCELLED.
Intake: APPROVED, APPROVED_WITH_WARNINGS, REJECTED.
Recon: CONFORME, WITH_FINDINGS. Sentinel: HEALTHY, ALERT.
Reglas/checks: PASS, FAIL; severidades ERROR/WARNING.

Fallos de preflight incluyen ENGINE_UNAVAILABLE y RESOURCE_DISK_INSUFFICIENT.
Errores de API incluyen INVALID_RULE_CONFIGURATION, VALIDATION_ERROR,
ARTIFACT_INTEGRITY_ERROR, ARTIFACT_NOT_REGISTERED, EXPORT_LIMIT_EXCEEDED,
RESULTS_NOT_READY, EVIDENCE_NOT_READY, VERSION_CONFLICT e IDEMPOTENCY_CONFLICT.
La UI puede traducir un label sin eliminar el código de la evidencia.
