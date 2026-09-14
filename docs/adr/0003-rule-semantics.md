# ADR 0003 — Observaciones, reglas portables y normalización explícita

Fecha: 2026-09-14. Estado: aplicado en 0.2.0.

## Profiling e identificadores

La discrepancia 44/43 procedía del trim aplicado por la lectura CSV anterior:
`Cliente 12` y `  Cliente 12  ` colapsaban antes del profiling. El nuevo lector
conserva los valores. No aplica trim, case folding, normalización Unicode ni
conversión funcional implícita. El perfil se identifica con
`OBSERVED_EXACT_V2` / método `EXACT_OBSERVED`, versión 2.

Distinct excluye sólo null. Espacios, case y representaciones Unicode son
observaciones distintas. En CSV, campo vacío sin comillas es null y `""` es
texto vacío. `distinct_rate` es distintos/no nulos; `uniqueness_ratio` es
registros cuyo valor aparece una sola vez/no nulos. Población vacía produce 0.

DATE requiere que todos los no nulos sean fechas ISO YYYY-MM-DD válidas.
TIMESTAMP requiere timestamp ISO con offset explícito; una columna que mezcla
fechas, timestamps, formatos ambiguos, texto vacío o fechas inválidas queda
STRING. Los null no fuerzan STRING. Un campo `id` o `*_id` recibe tag
IDENTIFIER y tipo lógico STRING. No se convierten universalmente los números:
las magnitudes inequívocas se infieren DECIMAL. Un override validado permite
cambiar tipo/tag; el canonical conserva el texto observado, incluidos ceros.

Los perfiles históricos permanecen intactos y visibles como legacy. No se
presenta el cambio como una recertificación retroactiva de sus métricas. Una
nueva carga del mismo archivo produce un DatasetVersion nuevo con semántica v2.

## Intake y Recon

Sólo se aplican transforms ordenados publicados en el contrato: trim, case,
Unicode, empty_to_null, padding de ID, remove_characters, decimal_parse y
date_parse con parámetros/formats explícitos. El valor recibido del error
se toma de la observación original; la salida aceptada usa los transforms.

La normalización de claves Recon se publica como `{trim, case,
unicode_normalization}`. Default nuevo: false/NONE/NONE. El orden es Unicode,
trim, case. La configuración v1 sin el campo se lee mediante un único
adaptador `LEGACY_V1_TRIM_EMPTY_NULL`, conservando su trim anterior y su hash
almacenado. La configuración efectiva y su hash se guardan en el manifest.

EXACT_MATCH se admite como alias histórico explícito de NUMERIC_TOLERANCE.
EXACT_COMPARE compara exactamente después de la normalización declarada y
permite varias parejas de columnas. No interpreta el texto como un importe.
Null/empty key son INVALID; espacios sólo se eliminan cuando se declara.

La tolerancia numérica es inclusiva. Con porcentaje, el umbral efectivo es
`max(abs, abs(denominador) * percent / 100)`. Denominador SOURCE/TARGET/MAX_ABS
declarado; cero usa EXACT_ONLY. Las operaciones preparatorias usan Decimal
con precisión ajustada a los datos, sin paso por float. La tolerancia temporal
acepta horas o días y compara instantes UTC; fecha sin hora significa medianoche.

Agregación 1:N simple admite SOURCE/TARGET, sum/count y columna de salida.
Conserva todas las líneas del grupo en el detalle; el lado único duplicado
sigue siendo un hallazgo. No implementa matching N:M ni asignación difusa.

## Compilación y métricas

RuleDefinition valida el contrato declarativo antes de publicar. Los caminos
`compile_rule`, `compile_comparison` y `compile_metric` producen expresiones
portables. PolarsCompiler y DuckDBCompiler ejecutan predicados nativos; una
preparación determinística común resuelve Decimal, fechas, normalización y
cuantiles para evitar redondeos distintos por engine. No se ejecuta scripting
arbitrario, Python eval/exec ni SQL del usuario. Los patrones usan la
intersección Rust/RE2; se rechazan backreferences/lookaround no portables.

Sentinel guarda método y versión de definición con cada serie. Las bandas
históricas usan sólo runs SUCCESS con la misma métrica, método y versión.
Se calcula mediana, Q1, Q3 e IQR mediante interpolación lineal versionada;
límites Q1-k·IQR y Q3+k·IQR. Sin suficiente historia usa umbral explícito o
la métrica previa compatible y deja registrado el fallback. Schema type puede
usar tipo esperado explícito o la DatasetVersion anterior; sin anterior
establece baseline, sin simular un cambio de esquema.

## Alcance portable

La paridad de predicados Polars/DuckDB está probada para reglas por registro,
comparaciones y métricas. El pipeline operativo usa Polars; DuckDB no es un
pipeline completo seleccionable, y no existe todavía adaptador PySpark. El
planner estima memoria/disco y rechaza una carga que requiera motor no
disponible. No se certifica procesamiento de 250k/1M registros ni un timeout
duro por proceso en esta entrega; se conservan los límites 10 MiB/100k filas.
