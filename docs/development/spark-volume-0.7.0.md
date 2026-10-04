# Spark 0.7.0: ejecución real, paridad y volumen

La certificación final completó Intake, ReconOps y Sentinel en Local y Standalone sobre un millón de registros por input. Cada población de resultados y aceptados coincide íntegramente con un oráculo incremental independiente: valores, multiplicidades y posición física o lineage. Standalone ejecutó dos executors registrados en contenedores separados. Las mediciones corresponden al 3 de octubre de 2026 en America/Bogota; los application IDs Standalone llevan fecha UTC del 4 de octubre. La [evidencia final saneada](evidence/0.7.0/spark-certification.json) conserva cifras, parámetros, huellas y procedencia verificable.

## Runtime y alcance

La imagen `trackvance-v070-isolated:backend` contiene Python 3.12, Java 17.0.20.1 y PySpark 4.0.3; su identidad medida fue `sha256:0d54553a57b395cda319fd541b03d4981eb3145d0da4ff321b7dd97f8647dfb8`. Ambos ciclos montaron el código actual de sólo lectura y registraron `source_fingerprint=3bdfe0166af8dab6e50ee916c83b02fda9d0c3a2b6908717766df443d2acc5d1`. Esta huella identifica los bytes leídos durante la prueba; no debe compararse con una huella de otra plataforma sin considerar diferencias de finales de línea. Java 17 y Python 3.12 cumplen la [compatibilidad oficial de Spark 4.0.3](https://spark.apache.org/docs/4.0.3/). Standalone usa client mode porque [cluster mode no admite aplicaciones Python](https://spark.apache.org/docs/4.0.3/submitting-applications.html).

El hardware disponible era Docker Desktop con 16 CPU y 15,2 GiB. Los ensayos utilizaron recursos independientes con prefijo `trackvance-v070-test-spark-{uuid}`, sin publicar puertos. El proyecto histórico de cinco servicios permaneció detenido y su inventario no cambió. DEFAULT y ACQUISITION del core de certificación se detuvieron mediante un helper guardado tras comprobar cero jobs, adquisiciones y leases activos; se conservó su estado previo. Local y Standalone corrieron secuencialmente. `scripts/tests/spark_cycle.py` genera entradas incrementales y ejecuta aplicaciones reales; no inicia Docker. Los harnesses Local y Standalone crean, miden y retiran únicamente sus recursos propios, verificando también el inventario del core protegido.

Este ciclo certifica los motores sobre snapshots canónicos Parquet y el perfil global de esas entradas. No equivale a una certificación de ingestión de todos los formatos, de sinks SQL, ni de transporte SQL hacia executors: la adquisición materializa y verifica primero la DatasetVersion. El ciclo real de publicación descrito abajo usa SQLite desechable; la integración general PostgreSQL se registra por separado.

## Contrato implementado

| Capacidad | Implementación y comprobación |
| --- | --- |
| Intake | Las 14 reglas de columna: required, not_null, unique, numeric, positive, type, range, allowed_values, regex, date_rule, compound_unique, length, column_compare y reference. Se comparan resultados y métricas completos contra Polars. |
| Condiciones y null | La población aplicable se fija antes de unicidad/global joins; ALLOW/FAIL/IGNORE, valores vacíos, null y claves compuestas con delimitadores dentro del dato conservan la semántica. |
| Transformaciones | trim, case, unicode_normalization, empty_to_null, id_padding, remove_characters, decimal_parse y date_parse, en orden declarado y conservando el valor cuando falla un parseo. |
| Precisión y texto | Decimal superior a 38 dígitos, diferencias de 10^-38, tolerancias pequeñas, Unicode descompuesto, emoji, identificadores con ceros y fechas/timestamps con offset. No conversión Decimal a float. |
| ReconOps | Claves simples/compuestas, normalización declarada, duplicados de ambos lados, SOURCE_ONLY/TARGET_ONLY, INVALID, exact_compare, numeric_tolerance y date_tolerance; agregaciones sum/count por lado, lineage y entrada vacía. Se conserva la identidad posicional de comparaciones repetidas. |
| Sentinel | Checks de schema/freshness/volumen, contadores globales de reglas de columna, métricas perfiladas exactas y comparación con histórico/baseline mediante la misma implementación portable. |
| Publicación | Resultados completos y aceptados multipart, esquema String/null más número interno, perfil global, Findings, manifest y vínculos entre Run, versión padre, versión derivada y Artifact. |
| Control | Aplicación y JobGroup por Run, cancelación Spark real, monitor lease/timeout/disco y fence Run→Job antes del commit de metadata. |

Spark realiza partición, joins, unicidad y escritura global mediante RDD persistidos en disco. Los kernels escalares y grupos ReconOps usan Polars portable en lotes de executor; `scalar_kernel=PORTABLE_POLARS_BATCH_V1` identifica esa implementación. Ninguna población de negocio se recoge en el driver. Se agregan contadores y, sólo para fallos escasos, hasta un lote de números enteros para broadcast. Los resultados se escriben sin `toPandas`, `collect` de negocio ni `coalesce(1)`.

La prueba de paridad JVM contiene casos representativos del catálogo; la existencia de un adaptador no significa que cada combinación posible de parámetros y distribución de datos tenga un ensayo de volumen separado.

El planner incluye una estimación conservadora de evidencia Polars completa, además de inputs/referencias: contempla que toda regla Intake habilitada falle en cada fila y que Recon conserve todos los detalles de comparación. Incluye valores, parámetros/condiciones y estructuras coexistentes; una muestra sin fallos no reduce esa cota. Dos regresiones verifican AUTO→Spark y rechazo de POLARS explícito antes de materializar. Las estimaciones no prometen un pico RSS exacto.

## Paridad y publicación

| Ensayo | Resultado y procedencia |
| --- | --- |
| FINAL Local | 35 PASS (16 escenarios JVM + 19 contratos/guards), 0 SKIP y 0 FAIL; suite dedicada 57,828 s y ciclo Docker completo 216,452 s. Después de la suite, los tres módulos procesan el millón de filas con verificación completa de resultados y aceptados. `strengthened-local-million/{parity.xml,evidence.json,docker-summary.json}`. |
| FINAL Standalone | 35 PASS (16 escenarios JVM + 19 contratos/guards), 0 SKIP y 0 FAIL; suite dedicada 65,767 s y ciclo Docker completo 263,744 s. Dos executors reales, los mismos casos y los tres módulos sobre un millón de filas con verificación completa. `strengthened-standalone-million/{parity.xml,evidence.json,docker-summary.json}`. |
| Publicación real incluida en ambas suites finales | `test_real_local_spark_run_publishes_complete_registered_lineage`: Run/Job, compute Spark Local, resultados y aceptados multipart, perfil, Findings, manifest y Artifact registrados en SQLite desechable. Este caso fija `local[2]` incluso dentro del harness Standalone; los otros 15 escenarios JVM de ese harness y sus tres módulos de volumen usan el cluster Standalone. |
| Ensayos iniciales históricos | Suites mixtas de 25 PASS Local/26 PASS Standalone, 0 SKIP, 44,53/58,05 s. Combinan casos JVM y contratos; no representan 25/26 escenarios JVM. Una prueba inicial adicional de publicación real pasó en 11,35 s. Sus logs conservan la evolución previa a los guards finales. |
| Recertificación intermedia de guards | Suites mixtas de 35 PASS (16 JVM + 19 contratos/guards), 0 SKIP: Local 64,59 s y Standalone 69,755 s, ciclo Docker Standalone 97,605 s. Certificaron guards, cancelación y publicación antes del ciclo final de millón de filas fortalecido; sus tiempos no son las mediciones finales de volumen. |
| Contratos de publicación | Tres pruebas iniciales con compute simulado verifican multipart, orden, integridad, cancelación sin publicación parcial y fence tras commit; se distinguen de la prueba real anterior. |
| Guards y semántica posteriores | 80 PASS y 15 NOT_RUN_OPT_IN en host sin Java habilitado, incluyendo scheduler/reglas avanzadas, anchura/grupo, merge global perezoso, ventana histórica y planner con referencias/bytes físicos; `final-host-contracts.log`. Ruff y mypy sobre los cinco archivos de ejecución pasan. |
| CI intermedio, commit `87e25b57` | Local y Standalone: 35 PASS, 0 SKIP, y los tres módulos sobre un millón de registros con verificación completa de valores y posiciones. Los cuatro fingerprints de resultados y aceptados coinciden entre modos. La evidencia está marcada INTERMEDIATE_CI y no sustituye el gate final de toda la release. |

Las suites finales incluyen catálogo, condiciones globales/null, transformaciones ordenadas, claves compuestas, comparaciones y agregación sum/count por ambos lados, una clave caliente de 20.000 registros, cancelación real y publicación. El desglose 16 JVM + 19 contratos/guards se obtiene de los nombres de los 35 casos JUnit, no del número de contenedores ni aplicaciones: en el harness Standalone son 15 escenarios del cluster y una publicación Local. El millón de filas se ejecuta después y no se cuenta como 35 escenarios adicionales.

Cada fingerprint `CANONICAL_JSON_ROW_SHA256_COUNT_SUM_XOR_V1` incluye todos los valores y la posición física o el lineage: para cada fila calcula SHA-256 de JSON UTF-8 canónico y acumula conteo, suma módulo 2^256 y XOR; el digest final aplica SHA-256 al estado completo. El oráculo de la fixture calcula esos estados de forma incremental; ReconOps usa plantillas Polars acotadas por clasificación y cardinalidades deterministas. Una regresión de 50.001 filas compara el oráculo con la población Polars completa. Los cuatro fingerprints coinciden con su oráculo y entre Local y Standalone finales. El método es independiente del orden de particiones y conserva la multiplicidad. Los hashes de bytes/orden físico de Parquet pueden diferir entre modos, como ocurrió en Intake y sus aceptados; esa diferencia no altera la igualdad lógica comprobada.

La [evidencia segura del CI intermedio](evidence/0.7.0/ci-intermediate-87/spark-certification.json) conserva commit, IDs de jobs y artifacts, hashes de procedencia, mediciones y cuatro fingerprints completos. Los ZIP descargados coinciden con los SHA-256 publicados por GitHub. Se conserva como INTERMEDIATE_CI y no sustituye la certificación Spark final ni el gate de toda la release.

El primer ensayo Local encontró una regresión real en la recompilación de tolerancias: Decimal podía serializarse como `1E-20`, representación que la declaración portable rechazaba al compilar por segunda vez. `config_semantics.py` conserva ahora formato decimal fijo y se comprueba la idempotencia de `effective_config`, también con magnitudes grandes. Otro ajuste conservó el diagnóstico legacy de normalización. El log inicial fallido permanece junto al log corregido; no se oculta ni se cuenta como PASS.

## Un millón de registros

La fixture contiene cuatro columnas de negocio, número físico original, ID duplicado cada 50.000 registros, decimal inválido cada 10.000, label null cada 20.000, Unicode variado y una referencia de 20 regiones. Origen y destino tienen un millón de filas cada uno; el destino introduce diferencias exactas de 10^-38. La generación usa partes de 8.192 registros. Antes de Sentinel, el perfil recorre la población completa con DuckDB spill y estadísticas Decimal exactas. El límite de memoria analítica de 256 MiB se refiere a DuckDB; la suma RSS incluye también el proceso Python y las librerías nativas.

| Perfil global | Segundos | Pico suma RSS, bytes | Pico cgroup muestreado, bytes | Muestras |
| --- | ---: | ---: | ---: | ---: |
| Local | 8,947 | 276.983.808 | 586.551.296 | 33 |
| Standalone | 9,130 | 278.343.680 | 418.979.840 | 34 |

| Modo | Módulo | Segundos | Resultados completos | Pico suma RSS del driver y descendientes, bytes | Pico cgroup muestreado del driver, bytes |
| --- | --- | ---: | ---: | ---: | ---: |
| Local[2] | Intake | 51,699 | 188 | 1.737.011.200 | 1.829.416.960 |
| Local[2] | ReconOps | 53,161 | 1.000.038 | 1.853.841.408 | 2.133.057.536 |
| Local[2] | Sentinel | 17,753 | 6 | 1.814.925.312 | 1.999.106.048 |
| Standalone | Intake | 55,453 | 188 | 640.225.280 | 767.905.792 |
| Standalone | ReconOps | 60,354 | 1.000.038 | 812.548.096 | 963.596.288 |
| Standalone | Sentinel | 22,793 | 6 | 747.683.840 | 894.091.264 |

Intake produjo 999.900 aceptados completos en 492 partes, 150 errores y 38 warnings en diez partes de resultados. ReconOps conservó 999.800 MATCH, 81 VALUE_MISMATCH, 81 INVALID, 38 DUPLICATE_SOURCE y 38 DUPLICATE_TARGET, en 490 partes. Sentinel publicó seis checks, tres PASS y tres FAIL. La decisión funcional Intake REJECTED o Sentinel ALERT es el resultado esperado de la fixture; PASS del ensayo significa que toda la evidencia y las métricas coinciden con lo esperado. El runner recorre todas las partes, valida schemas/conteos, calcula el hash físico y exige igualdad completa con los fingerprints independientes siguientes.

| Población | Filas | SHA-256 lógico final, igual al oráculo en ambos modos |
| --- | ---: | --- |
| Resultados Intake | 188 | `667508dc10a63d4e822c55a527259ee094b54fe108ddd9f5d0c54e24ead1aa33` |
| Aceptados Intake | 999.900 | `2548c0051e24ce39789257fe1918f6b7bb7ec443474c7d19732b1475c1c17ae5` |
| Resultados ReconOps | 1.000.038 | `9e250cbb4425454943f4654e076f9a0531e6fe7c130e246587fead4c9ef341be` |
| Resultados Sentinel | 6 | `f3257e3de926bfc290fec54766631337f0538f5542c57eb42f11e58f99ea1d66` |

Las aplicaciones Local fueron `local-1791088490897` (Intake), `local-1791088541740` (ReconOps) y `local-1791088595899` (Sentinel). Standalone registró `app-20261004044013-0001`, `app-20261004044107-0002` y `app-20261004044208-0003`, respectivamente; cada una observó tres entradas en `getExecutorMemoryStatus`, correspondientes al driver y dos executors.

## Límites y medición de executors

Local se ejecutó en un contenedor con límite de 3 GiB y 2 CPU. Standalone asignó master 512 MiB/0,5 CPU, cada executor 1,5 GiB/1 CPU y driver 2 GiB/2 CPU, para un máximo total de 5,5 GiB. Cada contenedor limitó procesos a 512. El heap de driver y cada executor fue 768 MiB, dos cores totales, cuatro particiones y resultado máximo del driver de 16 MiB.

| Proceso Standalone | Pico working set informado por Docker, bytes | `memory.peak` cgroup, bytes | Límite, bytes |
| --- | ---: | ---: | ---: |
| Master | 160.746.700 | 169.070.592 | 536.870.912 |
| Executor 1 | 705.377.075 | 792.555.520 | 1.610.612.736 |
| Executor 2 | 707.893.657 | 796.758.016 | 1.610.612.736 |
| Driver | 774.478.233 | No se obtuvo el peak global; se conserva el muestreo por módulo anterior | 2.147.483.648 |

Local observó working set máximo de 1.691.143.372 bytes y `memory.peak=2.151.247.872` bytes, bajo su límite de 3.221.225.472 bytes. `docker stats` informa working set y puede excluir cache de archivos. El sampler lee cgroup v2 cada 250 ms y suma RSS de `/proc` del proceso y sus descendientes. Esa suma puede contar librerías compartidas más de una vez y no incluye executors remotos. Los executors se midieron por separado; ninguna cifra de sólo driver se presenta como memoria del cluster. La memoria muestreada puede perder picos breves; `memory.peak` completa la medición donde se obtuvo. Ambos harnesses terminaron con exit code 0, `oom_killed=false`, `cleanup_finished=true` y `protected_main_inventory_unchanged=true`; retiraron sus contenedores, red y volumen propios.

Estas mediciones finales incluyen los guards de lotes por bytes, registro/grupo, planner, histórico acotado y merge global streaming que evita buffers poblacionales por clave. También incluyen el guard de representación Decimal anterior a una expansión grande. Los ensayos iniciales se conservan como históricos de su revisión; sus tiempos no se mezclan con esta tabla. La integración API adquisición→Spark→Delivery aporta mediciones independientes de la cadena completa, y la certificación Spark no sustituye las verificaciones SQL, recuperación o UI de toda la release.

## Dominio y rechazos explícitos

Los límites actuales predeterminados son 2.048 registros/8 MiB por lote, 64 KiB por registro, 10.000 registros/16 MiB por clave ReconOps y 16 MiB para resultados escalares del driver. Cada registro suma bytes UTF-8 más margen por columna. Se comprueba también el tamaño de la transformación. El perfil global contiene una cota conservadora por anchura máxima de todas las columnas; las versiones históricas sin ese hecho usan lectura Arrow de un registro. Una única página Parquet y el overhead nativo siguen bajo el límite real del contenedor.

| Condición | Resultado |
| --- | --- |
| Runtime PySpark/Java incompatible o ausente | ENGINE_UNAVAILABLE, precondición persistida. |
| POLARS explícito con población estimada superior al presupuesto | RESOURCE_MEMORY_INSUFFICIENT; no fallback. |
| Presupuesto Spark o disco insuficiente | RESOURCE_SPARK_MEMORY_INSUFFICIENT / RESOURCE_DISK_INSUFFICIENT. |
| Registro o transformación demasiado anchos | RESOURCE_SPARK_RECORD_LIMIT; sin resultado truncado. |
| Clave ReconOps que supera filas o bytes configurados | RESOURCE_RECON_GROUP_LIMIT; sin producto cartesiano ni evidencia parcial. |
| Cancelación, timeout o pérdida de lease | Se cancela JobGroup y se impide publicación por fence. |
| Excel supera 100.000 filas, 500.000 celdas o 16 MiB estimados | Rechazo explicado con alternativa CSV completo; no XLSX silenciosamente reducido. |

El rechazo de anchura/sesgo es de recursos y no impone una precisión Decimal artificial. El ensayo de cuatro columnas con grupos pequeños no certifica toda combinación de anchura, columnas o skew. Los tiers de adquisición 100 MiB/500 MiB/1 GiB, Delivery SQL y restauraciones tienen evidencia propia. Aumentar un parámetro requiere revisar límites reales y volver a medir.

## Reproducción y evidencia

Las pruebas JVM son opt-in: `TRACKVANCE_SPARK_TESTS=1` convierte un runtime ausente en fallo. En un runtime compatible se ejecuta `python -m pytest backend/tests/test_spark_engine.py -q`. La suite usa storage temporal y no necesita la instalación del usuario. Para el runner dentro de un contenedor aislado: `python scripts/tests/spark_cycle.py --root /ruta/temporal/nueva --rows 1000000`; el directorio debe ser nuevo.

Los harnesses oficiales de CI construyen primero `docker build -f backend/Dockerfile -t trackvance-v070-isolated:backend .`. Local ejecuta `python scripts/tests/spark_local_docker_cycle.py --evidence .codex-local/v070/spark/local-nuevo --rows 1000000`; Standalone ejecuta `python scripts/tests/spark_docker_cycle.py --evidence .codex-local/v070/spark/standalone-nuevo --rows 1000000`. Ambos exigen al menos 35 pruebas dedicadas sin SKIP (16 escenarios JVM reales y 19 contratos/guards) y después recorren los tres módulos sobre todas las filas solicitadas. Exportan JUnit, logs sanitizados, `evidence.json` y `docker-summary.json`; un módulo omitido o un modo distinto causa fallo. `--parity-only` omite expresamente el ciclo de volumen.

Sólo admiten directorios nuevos dentro de `.codex-local/v070/spark`, generan nombres y labels exclusivos, comprueban el contexto de Docker y comparan el inventario protegido antes/después. La limpieza verifica prefijo y owner label antes de retirar un recurso y se rechaza si cambia el contexto. Doce pruebas de guards comprueban aislamiento, rechazo de recursos ajenos, cero omisiones, presencia de executors y fingerprints completos válidos. Un fingerprint ausente/null o con digest inconsistente nunca aprueba el gate. Su ejecución requiere una ventana de recursos independiente de otros ensayos. El overlay operativo se combina con `compose.yml` y `--profile spark-standalone`; no crea ni inicia recursos como efecto de un import.

La ejecución final Local pertenece a `trackvance-v070-test-spark-2f8bf6911c7b` y Standalone a `trackvance-v070-test-spark-f9e8f7a9a976`. Sus archivos privados están en `.codex-local/v070/spark/strengthened-local-million/` y `strengthened-standalone-million/`: `parity.xml`, `evidence.json`, `docker-summary.json`, `driver.log` y logs de master/executors donde corresponda. Los logs iniciales y recertificaciones intermedias permanecen en la misma carpeta privada, excluida de Git, con su alcance histórico.

El [JSON final versionado](evidence/0.7.0/spark-certification.json) contiene SHA-256 de cada `evidence.json`, `docker-summary.json` y `parity.xml`, el desglose JUnit, parámetros efectivos, aplicaciones, memoria y fingerprints completos. Verifica `cross_mode_metrics_equal`, `cross_mode_complete_values_equal` y `cross_mode_source_equal`; su alcance es FINAL_SPARK_CERTIFICATION. La publicación se limita a fixtures sintéticas y metadatos saneados, sin datos operativos del usuario ni secretos.
