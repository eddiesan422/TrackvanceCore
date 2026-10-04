# ADR 0022: Spark distribuido con semántica exacta y buffers acotados

- Estado: aceptada
- Fecha: 2026-10-03

## Contexto

Intake, ReconOps y Sentinel comparten condiciones, políticas de null, transforms ordenados, claves compuestas y Decimal exacto. Una traducción directa a Spark SQL podría redondear valores superiores a DECIMAL(38), cambiar Unicode o fechas y calcular unicidad sólo dentro de un lote. Recoger los resultados en el driver también impediría ejecutar una población grande bajo límites reales de memoria.

## Decisión

La imagen usa Python 3.12, Java 17 y PySpark 4.0.3. Se implementan Spark Local y Standalone client mode. Apache admite Java 17/21 y Python 3.9+ en [Spark 4.0.3](https://spark.apache.org/docs/4.0.3/); [Standalone no admite cluster mode para aplicaciones Python](https://spark.apache.org/docs/4.0.3/submitting-applications.html). La API y el worker conservan la misma configuración de master y presupuesto al construir el plan.

Spark distribuye la población mediante RDD y persiste intermediarios en disco. El kernel Polars portable evalúa predicados escalares por lotes en cada executor, con Decimal arbitrario y las funciones existentes de Unicode/fechas. La metadata declara `PORTABLE_POLARS_BATCH_V1`; no presenta esos predicados como expresiones JVM nativas. Las operaciones globales de unicidad y referencias usan claves tuple y joins distribuidos; las condiciones y null policies determinan la población aplicable antes de contar. ReconOps reparte y ordena la clave compuesta completa y conserva números de registros, duplicados y lineage de agregación. Sentinel combina los contadores distribuidos con el perfil global exacto ya publicado.

El cruce global ordena lookup antes de los números de registros aplicables, particionando por la clave original; un merge streaming evita los buffers por clave del `RDD.leftOuterJoin` de PySpark. Unicidad y referencias pueden recorrer una clave muy frecuente con estado constante. El histórico Sentinel filtra método, versión y métrica antes de limitar a la ventana compatible, hasta 1.000 observaciones por regla, conservando las bandas exactas.

Los registros canónicos contienen String/null en columnas de negocio y `__tv_record_number` Int64 interno. Las partes Parquet y su descriptor conservan esquema físico, filas, tamaños y hashes; el esquema de negocio omite columnas internas. Los lectores históricos mantienen líneas físicas CSV/TXT, incluidos registros multilínea. Los ejecutores reciben rutas verificadas de snapshots, nunca sesiones SQL, credenciales ni una instrucción para refrescar la fuente.

El driver recibe métricas agregadas y checks acotados. Una optimización de fallos escasos puede recoger como máximo `batch_rows` números enteros para broadcast; no recoge valores de negocio. Los resultados y aceptados se escriben completos en partes, sin `collect`, `toPandas` ni concentración en una sola partición. Paginación y CSV usan DuckDB con spill y cursor por lotes. Excel rechaza explícitamente un informe superior al dominio documentado y remite al CSV completo.

Los buffers se acotan por filas y bytes: por defecto 2.048 registros/8 MiB, 64 KiB por registro y 10.000 registros/16 MiB por clave ReconOps. El perfil aporta una cota global de anchura para dimensionar el lote Arrow; una versión antigua sin cota se lee registro a registro. Se comprueba el tamaño tras transformar y al reunir un grupo. Superar un dominio produce `RESOURCE_SPARK_RECORD_LIMIT` o `RESOURCE_RECON_GROUP_LIMIT`; no trunca ni aproxima. Las cotas de buffers no reemplazan los límites del contenedor ni predicen todo el overhead de Arrow/JVM.

El lote Recon incluye además una reserva conservadora para evidencia portable, según cardinalidad real y número de comparaciones. Los grupos ambiguos no retienen detalles de comparación; la agregación conserva lineage. Una clave indivisible que exceda el presupuesto de evidencia falla antes del kernel, y las claves pequeñas con muchos detalles reducen automáticamente el número de grupos por lote.

`ExecutionPlanner` fija AUTO/POLARS/PYSPARK, runtime, deployment, parámetros y presupuesto. Incluye referencias y usa bytes de las partes en vez del tamaño del descriptor JSON. La ausencia del motor y las insuficiencias de recursos producen precondiciones persistidas; una selección explícita no cambia silenciosamente de motor. Las estimaciones se identifican como tales.

El presupuesto Polars incluye una cota conservadora de evidencia completa: filas por reglas habilitadas en Intake y detalles por comparación en Recon, con anchura de valores, parámetros, condiciones y representaciones de salida coexistentes. No infiere el porcentaje de errores a partir del preview. Un archivo comprimido pequeño con muchos checks puede requerir Spark; POLARS explícito se rechaza antes de materializar si excede ese presupuesto estimado.

Cada Run tiene aplicación y JobGroup propios. El monitor verifica cancelación, lease, timeout y disco durante el cálculo. La publicación bloquea Run y aplica CAS al Job RUNNING con owner y lease vigentes, reteniendo el fence hasta commit. Si pierde autoridad, no publica resultados ni una versión parcial. El paquete distribuido contiene sólo código Python y los executors montan exclusivamente artifacts.

## Consecuencias y verificación

Los lotes portables añaden trabajo Python y la ejecución requiere snapshots materializados accesibles a todos los executors. Standalone implementado es de un mismo host con volumen compartido; no certifica Kubernetes ni object storage. Las claves muy sesgadas y registros muy anchos tienen rechazo de recursos explícito, aunque sus valores sean semánticamente válidos. La precisión Decimal no queda limitada a 38 dígitos.

La suite JVM compara resultados completos, métricas, decisiones y numeración contra Polars para el catálogo de reglas, transforms, null policies, claves compuestas, referencias, agregaciones y checks Sentinel. Se ejecutaron 25 pruebas en Local y 26 en Standalone, más un ciclo real Run/Job/artifacts/lineage en SQLite. La recertificación final aprobó 35 pruebas sin omisiones en cada modo después del endurecimiento, incluyendo una clave de 20.000 registros y count por ambos lados. Ambos modos completaron un millón de registros en los tres módulos y conservaron todos los resultados ReconOps. [La medición](../development/spark-volume-0.7.0.md) distingue pruebas reales, contratos simulados, recursos, procedencia y ajustes posteriores.
