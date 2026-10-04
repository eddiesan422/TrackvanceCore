# Adquisición y recorrido de volumen 0.7.0

Medición real del contexto desechable `trackvance-v070-test-core-5e417a35814f`,
con PostgreSQL y workers de adquisición, Spark, Delivery y eventos independientes.
El inventario del proyecto principal se comprobó antes y después. Estos resultados
describen ese entorno; no representan un SLA ni certifican volúmenes mayores.

## Fixture y población

Cada tier contiene un millón de registros y cinco columnas. La semilla pública
`trackvance-v070-million-observed-v1` produce payloads SHAKE256 URL-safe distintos
por registro. Se conservan identificadores de doce dígitos, decimales con ocho
posiciones, veinte regiones, Unicode descompuesto, emoji, espacios y campos CSV
multilínea. Hay exactamente 10.000 NULL y 10.000 textos vacíos en `observed`.

El generador escribe CSV, NDJSON y Parquet con buffers de 2.048 registros. Mide
bytes reales, ancho UTF-8 mínimo/máximo/promedio, cardinalidad, razón gzip y hashes;
el tamaño nominal del tier identifica el objetivo del generador, no el tamaño del
archivo ni la cantidad de filas. La progresión cambia ancho y bytes, manteniendo
un millón de registros y un millón de payloads diferentes.

| Objetivo | CSV real, bytes | NDJSON real, bytes | Parquet real, bytes | Ancho UTF-8 observado min/max/promedio |
|---|---:|---:|---:|---|
| 100 MiB | 121.413.300 | 184.413.159 | 59.408.757 | 90 / 131 / 116,393059 |
| 500 MiB | 541.413.300 | 604.413.159 | 377.525.863 | 510 / 551 / 536,393059 |
| 1 GiB | 1.090.413.300 | 1.153.413.159 | 791.832.404 | 1.059 / 1.100 / 1.085,393059 |

Los tres formatos de cada tier tienen la misma población y el mismo SHA-256 de
registros JSON UTF-8 canónicos, ordenados por `record_id`:

| Objetivo | SHA-256 canónico de todos los registros |
|---|---|
| 100 MiB | `231a977eb87159a2b5aa2c76290e5ba419b5f9645feb74d8da08e1f175089ece` |
| 500 MiB | `f0eb629c0ed78cfd10e054d0c9a090dccf1ed878bafb501d1565e101c2895be0` |
| 1 GiB | `e96cfff4a4ce4242cf98c96af60d2640cde64b47fe029576a2cf900fc5075132` |

## Operaciones verificadas

Los tiers obligatorios de 100/500/1024 MiB terminaron **PASS**: recepción raw por la
ruta normal de nginx, registro HTTP 202 idempotente, adquisición y perfil global de cada
formato. Para cada CSV, el recorrido registró preflight completo, Intake PYSPARK
aprobado, salida aceptada materializada, Delivery CHAINED a PostgreSQL y tres
notificaciones personales. El hash completo del origen, la salida aceptada y la tabla SQL
coincide; la tabla y cada versión contienen exactamente un millón de registros.
La ocurrencia encadenada identifica la salida concreta de su Intake disparador.

| Fase, segundos | 100 MiB | 500 MiB | 1 GiB |
|---|---:|---:|---:|
| Recibir/adquirir/perfilar/verificar CSV | 73,302 | 143,657 | 235,536 |
| Preflight completo | 86,588 | 148,765 | 210,868 |
| Intake Spark y hash accepted | 49,732 | 56,721 | 67,458 |
| Delivery encadenado y hash SQL | 71,280 | 80,407 | 110,441 |
| Recibir/adquirir/perfilar/verificar NDJSON | 95,283 | 141,341 | 183,510 inicial + 28,524 revalidación |
| Recibir/adquirir/perfilar/verificar Parquet | 209,172 | 143,567 | 79,732 |

El tiempo Parquet de 100 MiB precede al lector Arrow que descomprime cada grupo
una sola vez. Hubo reinicios coordinados y certificaciones Spark independientes
durante ventanas de adquisición; los tiempos no constituyen una comparación de
rendimiento bajo condiciones idénticas.

También se verificó PostgreSQL real con cursor, snapshot completo, refresh nuevo,
cancelación con cursor activo y dos versiones previas intactas. SIGKILL del worker
de adquisición produjo dos intentos con UUID diferentes y exactamente una versión
completa con hash idéntico, en 129,848 s. Alterar exclusivamente un staging privado
produjo `ARTIFACT_INTEGRITY_ERROR`, ninguna versión y ninguna salida parcial; ese
fixture se retiró y quedó EXPIRED, conservando su fallo histórico.

## Recursos y límite de la evidencia

Se muestrearon cada dos segundos RSS sumado de procesos, cgroup v2, `cpu.stat`,
`memory.events`, temporales y disco libre del host. Los picos conjuntos de cgroups
backend observados fueron 2.037,3 MiB, 3.802,2 MiB y 5.465,9 MiB para los tres tiers.
Cada pico conjunto es el máximo de la suma de `memory.current` de los servicios
obtenidos dentro de un mismo ciclo de muestreo. Las lecturas secuenciales tardan
aproximadamente 1–2 segundos; no forman una instantánea atómica. No se suma el
máximo individual de cada servicio. En Spark del tier 1 GiB, los máximos RSS por
servicio fueron adquisición 205,3 MiB, API 683,2 MiB, Delivery 291,9 MiB y worker
1.262,9 MiB; esos máximos no se suman como
si ocurrieran simultáneamente. Cgroup incluye caché de archivos y spill. Los picos
de vida de un contenedor se etiquetan separadamente del pico muestreado por fase.
No se observó OOM durante los recorridos.

Una CPU calculada entre contenedores reiniciados no es válida: el informe original
mostraba deltas negativos en esos casos. La evidencia final los marca `null` y
`COUNTER_RESET`, con medición incompleta; no reconstruye CPU inventada. Los runners
actuales suman únicamente deltas dentro del mismo `container_id` y registran cada
reinicio. Las fases instantáneas sin muestras conservan memoria `null`.

La certificación usó explícitamente un máximo de upload de 2 GiB y de valores
observados de 4 GiB en el archivo privado del contexto. Los defaults productivos
permanecen en 1/2 GiB; el CSV nominal de 1 GiB supera el límite productivo por el
overhead real medido. Los niveles opcionales de 2/5 GiB quedaron
**NOT_RUN_RESOURCE_LIMIT** por el guard previo a generar fixtures:
`objetivo + 64 MiB > max_upload_bytes` o
`objetivo × 2 > max_observed_bytes`. Para 2 GiB, la estimación de upload es
2.214.592.512 bytes, mayor que 2.147.483.648; la estimación observada de
4.294.967.296 bytes coincide con el máximo y no activa esa segunda condición.
Para 5 GiB, ambas estimaciones exceden sus límites: 5.435.817.984 bytes de upload
y 10.737.418.240 observados. No se generaron archivos CSV, NDJSON ni Parquet de
esos objetivos; sus tamaños físicos son desconocidos. El guard no demuestra que
un Parquet de objetivo 2 GiB sea imposible ni certifica capacidad adicional.
Un runner Docker de 7 GiB con reserva de 2 GiB ofrece
un presupuesto inferior al pico cgroup medido; no está certificado para esta matriz.

## Incidencia conservada y reproducción

Después de adquirir y verificar NDJSON1 GiB, consultar su perfil con el proceso
API anterior devolvió HTTP504. Se conserva `volume-evidence-attempt-1.failed.json`
y su SHA en `attempt_history`. Reiniciar únicamente API resolvió la consulta:
4,6192 s directa y 4,2479 s por nginx, HTTP200, respuesta27.415 bytes, muestra20
de23.043 bytes y perfil persistente1M. La causa interna del proceso anterior no
se demostró; no se atribuye el fallo únicamente al tamaño de muestra ni se elevó
el timeout del proxy. La reanudación verificó nuevamente NDJSON y completó Parquet;
la cadena CSV aprobada se conservó sin repetirla ni borrar la incidencia.

La prueba posterior de navegador volvió a revelar una demora real del perfil.
Dos solicitudes HTTP simultáneas agotaron su timeout, y las capturas del proceso
API persistente situaron la espera en `PythonImportCacheItem::LoadModule` de
DuckDB y locks/búsquedas de `importlib`, dentro de sus consultas parametrizadas de
metadatos y muestra. Los subprocesos Linux fríos no reprodujeron esa demora, por
lo que no se declara demostrada la causa interna ni un deadlock permanente.

La presentación del perfil ahora utiliza el perfil global persistido y una
muestra Arrow de hasta veinte registros, con lotes acotados antes de convertir
a objetos Python; el ancho histórico desconocido utiliza un registro por lote.
Los hashes completos, esquema y conteos de cada parte siguen verificados, con
conteos obtenidos directamente del footer Parquet. No hay consultas analíticas
ni búsqueda de tipos de parámetros DuckDB en esta ruta de presentación. Tras
cargar el cambio, el mismo proceso API respondió HTTP 200 a diez solicitudes
con paralelismo dos y treinta solicitudes con paralelismo diez; máximo 4,686 s,
perfil global 1M y veinte registros de muestra correctos. La consulta del perfil
real de 1 GiB respondió HTTP 200 en 3,035 s. El timeout permaneció en treinta
segundos y la incidencia original permanece conservada.

Para un contexto nuevo, protegido por `scripts/certification_v070.py`:

```powershell
python scripts/tests/volume_cycle.py --context <contexto-privado> --sizes 100 500 1024 --optional-large
```

La prueba de navegador `frontend/tests-e2e/volume.spec.ts` utiliza la recepción,
inspección, registro HTTP 202, navegación fuera del dataset, publicación del contrato,
preflight, configuración/automatización y ejecución Spark desde controles normales.
`--verify-browser-report <volume-ui.json>` comprueba después todas las filas y hashes
de sus versiones source/accepted y tabla SQL.

La certificación final de navegador terminó **PASS**: dos pruebas reales
(`volume.spec.ts` y `automation.spec.ts`), cero omisiones, fallos o retries flaky,
en 206,098 segundos. Desde controles normales se recibieron 121.413.300 bytes CSV,
se registró la adquisición, se abandonó y recargó la pantalla, se comprobó el
perfil global, se publicó el contrato y el preflight completo, se activó la
automatización y se ejecutó Intake PYSPARK. La entrega encadenada comprometió la
salida accepted concreta y publicó receipt y notificaciones personales.

El verificador posterior recorrió todos los registros del origen, accepted y
tabla SQL: exactamente 1.000.000 en cada uno, con SHA-256 canónico
`231a977eb87159a2b5aa2c76290e5ba419b5f9645feb74d8da08e1f175089ece` idéntico.
El origen y accepted conservan 10.000 NULL y 10.000 textos vacíos. El verificador
SQL utiliza una fila de buffer. Tras completar los hashes quedaron cero Jobs
activos y cero adquisiciones activas; el inventario principal permaneció intacto.
El resumen publicable sin rutas privadas ni credenciales se conserva en
[`acquisition-browser-certification.json`](evidence/0.7.0/acquisition-browser-certification.json).

## Ensayo adicional de etapas y recorrido integral

El 4 de octubre de 2026 se repitieron los tres CSV con namespaces, datasets,
versiones y destinos SQL nuevos, usando el código
`191ff2805f755f7b2d090ef3aeaa642fc2ae476a`. Los tres recorridos terminaron **PASS**.
Este ensayo adicional conserva las cifras anteriores y mide un reloj integral
nuevo desde antes de transferir el archivo por HTTP hasta verificar SQL y la
bandeja personal. El reloj incluye preparación, publicaciones, comprobaciones
completas de hashes y pausas de instrumentación del certificador. Los fixtures
ya existían en el mismo host; no es una prueba de caché fría ni un SLA.

Cada adquisición produjo un millón de registros y un perfil completo idéntico
al persistido en su versión original, comparando todos sus campos, esquema y
hash de esquema. Se verificó que las versiones originales permanecieron
inmutables. El origen nuevo, la salida accepted de Intake PYSPARK **APPROVED** y
la tabla PostgreSQL de Delivery **COMMITTED** conservan el mismo hash completo
de su tier, incluyendo 10.000 NULL y 10.000 textos vacíos. Delivery seleccionó
la versión accepted concreta del disparador y la bandeja incluyó los tres
recursos esperados.

| Reloj o fase medida, segundos | 100 MiB | 500 MiB | 1 GiB |
|---|---:|---:|---:|
| Transferencia HTTP raw | 0,660746 | 2,595792 | 4,923375 |
| Crear dataset y registrar adquisición | 0,098604 | 0,295387 | 0,591193 |
| Espera durable hasta SUCCESS, con polling | 29,526450 | 58,547290 | 98,070818 |
| Verificar todos los registros del origen | 5,429654 | 7,989627 | 11,469903 |
| Preflight completo y publicar configuración | 28,658 | 29,439 | 36,536 |
| Intake PYSPARK y hash accepted | 54,904 | 49,970 | 59,054 |
| Delivery encadenado y hash SQL | 70,787 | 86,945 | 107,521 |
| Comprobar bandeja personal | 1,572 | 1,431 | 1,487 |
| **Reloj integral observado** | **191,686482** | **237,246066** | **319,704406** |

La suma explícita de las cuatro últimas fases de cadena es
155,921 / 167,785 / 204,598 segundos. Esa suma excluye recepción, adquisición y
otras comprobaciones; no sustituye el reloj integral ni divide retroactivamente
los tiempos agrupados del primer ensayo. Tampoco se resta un perfil independiente
del tiempo anterior.

El estado durable se consultó cada 0,5 segundos. Cada transición se encuentra
entre el inicio de la solicitud anterior y el fin de la primera respuesta con
el estado nuevo. Las duraciones siguientes son límites inferior y superior,
no tiempos exactos; el JSON conserva todas las ventanas de entrada y salida.
READING y MATERIALIZING forman un grupo porque se alternan durante la lectura.

| Estado: intervalo de duración, segundos | 100 MiB | 500 MiB | 1 GiB |
|---|---:|---:|---:|
| QUEUED | 0,000015–0,590586 | 0,000013–0,784866 | 0,000013–1,102820 |
| READING/MATERIALIZING | 14,000559–15,017920 | 41,009704–42,037132 | 75,519282–76,554383 |
| PROFILING | 12,498934–13,516264 | 11,999178–13,015148 | 16,004406–17,032454 |
| PUBLISHING | 0,992061–2,009477 | 3,494585–4,510849 | 4,483797–5,511876 |

Se observaron 26/25/33 respuestas en PROFILING y 4/3/5 ciclos de recursos con
sondas completadas dentro de su ventana estable. Los máximos de separación entre
observaciones fueron 0,500570 / 0,504726 / 0,576157 segundos; las solicitudes
individuales alcanzaron 0,081863 / 0,265052 / 0,576145 segundos. La incertidumbre
de HTTP y del muestreo está incluida en los intervalos publicados.

Los límites de adquisición usados siguieron siendo 256 MiB para el motor,
5.000 filas y 8 MiB por lote, upload 2 GiB y valores observados 4 GiB. La cota
del motor no es una cota de RSS del proceso ni de `memory.current` del cgroup;
este último también incluye caché de archivos. Para PROFILING, únicamente se
atribuyen recursos de sondas que comenzaron y terminaron dentro de la ventana
estable del estado:

| Recursos observados en PROFILING | 100 MiB | 500 MiB | 1 GiB |
|---|---:|---:|---:|
| RSS máximo acquisition-worker, MiB | 331,992 | 453,605 | 481,137 |
| Cgroup máximo acquisition-worker, MiB | 448,996 | 1.139,129 | 1.535,965 |
| CPU acquisition-worker entre sondas retenidas, segundos | 7,411996 | 7,084830 | 13,356226 |
| Spill compartido máximo observado, MiB | 0 | 377,281 | 843,219 |
| Temporales compartidos máximos observados, MiB | 398,752 | 1.479,339 | 2.864,435 |

Las siguientes fases tienen muestreadores propios. La CPU es la suma de deltas
de los cuatro roles backend entre sus primeras y últimas sondas retenidas; no
es tiempo de pared. El RSS mostrado corresponde al worker responsable
(Delivery para preflight/entrega, DEFAULT para Intake y API para bandeja).
El JSON publica también RSS, cgroup y CPU de cada uno de los cuatro roles,
disco libre, errores de medición y contadores de memoria por fase.

| Fase / objetivo | CPU backend, segundos | RSS responsable, MiB | Pico conjunto cgroup, MiB | Temporales compartidos, MiB |
|---|---:|---:|---:|---:|
| Preflight / 100 MiB | 33,480133 | 238,984 | 1.039,965 | 463,325 |
| Intake / 100 MiB | 94,962897 | 1.120,914 | 1.823,004 | 513,816 |
| Delivery / 100 MiB | 75,804750 | 282,820 | 1.718,070 | 492,660 |
| Bandeja / 100 MiB | no medida | 243,578 | 1.447,582 | 231,578 |
| Preflight / 500 MiB | 34,719103 | 270,840 | 3.204,121 | 1.006,921 |
| Intake / 500 MiB | 79,829357 | 1.206,680 | 4.018,434 | 1.749,256 |
| Delivery / 500 MiB | 84,969198 | 324,070 | 3.813,551 | 1.013,300 |
| Bandeja / 500 MiB | no medida | 245,965 | 2.956,824 | 231,578 |
| Preflight / 1 GiB | 42,471092 | 344,641 | 4.915,297 | 1.536,953 |
| Intake / 1 GiB | 99,825089 | 1.239,242 | 5.704,770 | 2.952,655 |
| Delivery / 1 GiB | 97,578477 | 344,613 | 5.670,266 | 1.536,953 |
| Bandeja / 1 GiB | no medida | 233,195 | 4.241,105 | 231,578 |

La bandeja tuvo una sola muestra, por lo que sus deltas CPU y eventos de memoria
se publican como `null`, con `INSUFFICIENT_RETAINED_COUNTER_PROBES`. No se midió
spill separado con esos muestreadores de cadena; se conserva `null` y
`NOT_RECORDED_BY_PHASE_SAMPLER`. El muestreador integral y las ventanas de
adquisición sí conservaron spill. Las lecturas de temporales corresponden al
mismo almacenamiento compartido y su máximo no se suma por servicio. Incluyen
archivos temporales preexistentes; el spill contabiliza directorios tocados desde
el inicio del nuevo ciclo. Estas cifras son ocupación observada, no bytes escritos
acumulados ni tamaño total del catálogo de artefactos.

| Recursos del recorrido integral | 100 MiB | 500 MiB | 1 GiB |
|---|---:|---:|---:|
| Ciclos del muestreador integral | 53 | 67 | 91 |
| CPU backend entre sondas retenidas, segundos | 251,894378 | 286,168562 | 381,188579 |
| Pico conjunto cgroup del muestreador integral, MiB | 1.813,004 | 4.021,180 | 5.693,684 |
| Mayor pico conjunto observado por cualquier muestreador, MiB | 1.823,004 | 4.021,180 | 5.704,770 |
| Spill compartido máximo integral, MiB | 0 | 505,219 | 976,531 |
| Temporales compartidos máximos integrales, MiB | 514,158 | 1.742,114 | 2.994,278 |
| Mínimo disco libre del host entre muestreadores, bytes | 1.329.615.585.280 | 1.328.872.882.176 | 1.328.863.682.560 |

Los muestreadores integral y de fase son independientes. Cada pico conjunto es
el máximo de una suma de `memory.current` dentro de un ciclo secuencial; no es
una instantánea atómica, no suma máximos individuales y no utiliza el pico de
vida de los contenedores. El mayor observado quedó bajo el guard de 6.144 MiB
para los cuatro roles backend. No hubo OOM ni `oom_kill`, pero sí presión real:
en PROFILING 1 GiB, acquisition-worker acumuló 19.206 eventos `memory.events.max`.
En el integral de 500 MiB acumuló 3.259; en el de 1 GiB los incrementos fueron
40.334 en adquisición, 10.182 en API, 83 en Delivery y 22.373 en DEFAULT. Estos
resultados no certifican margen para concurrencia adicional. Ningún contador CPU
se reinició durante los tres ensayos y no hubo errores ni rechazos del guard.

El primer intento de este ensayo adicional se conserva como **FAIL**: tras
adquisición, perfil y preflight completos, publicar la configuración devolvió
HTTP 504. Antes de repetir una escritura se comprobó que la configuración se
había publicado. La captura nativa tardía encontró el proceso inactivo; la causa
interna sigue `NOT_PROVEN`. Los GET directos y por proxy posteriores respondieron
200 antes del reinicio. La creación del descriptor de Delivery se cambió a
metadatos/footer Arrow acotados y su muestra limitada a lotes Arrow de una fila;
la iteración completa del worker conserva DuckDB. Después se recreó solamente
API, sin aumentar cotas ni timeouts, y se repitieron los tres recorridos desde
UUID y reloj nuevos. Los tiempos de este PASS no rescatan ni sustituyen el reloj
del intento fallido.

La evidencia final registra cero Jobs, adquisiciones, Runs, leases de Jobs y
leases de eventos activos al comenzar y al terminar; el inventario principal
permaneció **UNCHANGED**. Se conservan todos los recursos privados hasta su
limpieza coordinada. El resultado saneado, los hashes, intervalos, recursos y
antecedente fallido están en
[`acquisition-timing-certification.json`](evidence/0.7.0/acquisition-timing-certification.json).
Para repetir el ensayo en un contexto guardado con fixtures y baseline existentes:

```powershell
python scripts/tests/acquisition_timing_cycle.py --context <contexto-privado> --baseline docs/development/evidence/0.7.0/volume-certification.json --with-chain
```
