# Benchmark local de volumen

## Alcance y reproducción

El runner usa un proyecto Compose aleatorio `trackvance-bench-*` y rechaza cualquier
recurso preexistente antes de declararlo propio. Al terminar crea un plan con IDs
exactos y elimina solo ese proyecto. El watchdog detiene esos contenedores si la
memoria cruza la RAM Docker menos 2 GiB, el disco libre cae de 10 GiB o se agota el
tiempo. `PASS`, `FAIL` y `NOT_RUN_RESOURCE_LIMIT` son estados distintos.

Certificación completa de 100 MiB:

```powershell
python scripts/tests/benchmark_cycle.py
```

Smoke CI no certificante, sin PostgreSQL/SQL Server externos:

```powershell
python scripts/tests/benchmark_cycle.py --target-mib 1 --rows 1000 `
  --file-only --max-wall-seconds 600
```

El preset CI usa límites duros 768 MiB PostgreSQL, 1 GiB API, 3 GiB worker y
128 MiB web. Son techos, no reservas. El full usa 1/2/7 GiB y 256 MiB, además de
768 MiB para PostgreSQL fuente y 2.5 GiB para SQL Server. Todos son overrides del
benchmark; `compose.yml` y los límites productivos permanecen intactos.

## Fixture y operaciones

El fixture principal contiene 50.000 filas y cuatro columnas: `record_key`,
`amount`, `event_date` y `payload`. El CSV de 106.194.531 bytes se escribe en
streaming. Cada payload se deriva de SHAKE256 con semilla pública y número de fila,
se codifica URL-safe y ocupa 2.098 bytes (`SHAKE256_URLSAFE_V1`). Su SHA-256 fue
`0353b26611a305b581766ec3ee8d9073068ad66633f45ed79c9c70e74b08b6bb`.

PostgreSQL y SQL Server generan filas de igual forma y tamaño lógico con hashes por
fila/bloque. Las cuentas consumidas por Trackvance solo tienen SELECT. La prueba:

1. carga el archivo dos veces en datasets distintos;
2. ejecuta Intake, Recon de ambas versiones y una observación Sentinel;
3. materializa 50.000 filas desde cada motor como snapshot inmutable;
4. ejecuta Intake sobre ambos snapshots;
5. registra planes, hashes, artifacts, tiempos, filas/s, bytes/s, CPU, memoria,
   block I/O y muestreo de `storage/tmp` cada 0,2 s.

## Máquina y resultado principal

Ejecución del 19 de septiembre de 2026 en Ryzen 7 9700X, Windows 11, 32 GiB de
RAM host, SSD NVMe Samsung 990 EVO Plus 2 TB; Docker Desktop 29.1.3 expuso 16 CPU,
15,21 GiB RAM, Linux x86_64 y overlayfs. Había 1,36 TiB libres. Resultado `PASS`
en 79,46 s, sin OOM ni intervención del watchdog.

| Operación | Tiempo | Filas/s | MB/s de entrada |
| --- | ---: | ---: | ---: |
| Upload archivo origen | 1,58 s | 31.703 | 67,3 |
| Upload archivo destino | 1,31 s | 38.281 | 81,3 |
| Intake archivo | 3,08 s | 16.218 | 34,4 |
| Recon archivo | 4,08 s | 12.266 | 26,1 |
| Sentinel archivo | 2,05 s | 24.437 | 51,9 |
| Snapshot PostgreSQL | 2,36 s | 21.164 | 44,9 |
| Intake PostgreSQL | 3,11 s | 16.068 | 34,1 |
| Snapshot SQL Server | 1,35 s | 37.168 | 78,9 |
| Intake SQL Server | 3,07 s | 16.301 | 34,6 |

El tamaño lógico PostgreSQL fue 106.144.494 bytes y el de SQL Server 105.750.000.
Cada upload conservó el original de 106.194.531 bytes y produjo Parquet de
79.145.600 bytes. Los Parquet externos midieron 55.099.067 y 55.107.251 bytes.
El máximo observado en `tmp` fue 185.340.131 bytes; storage creció como máximo
670.790.434 bytes sobre su baseline. El block I/O combinado máximo muestreado fue
2.815.915.000 bytes.

| Servicio | HWM del proceso | Pico cgroup | CPU acumulada |
| --- | ---: | ---: | ---: |
| API | 812.998.656 B | 1.253.138.432 B | 8,89 s |
| Worker | 1.051.635.712 B | 1.224.929.280 B | 12,62 s |
| PostgreSQL interno | 26.869.760 B | 97.026.048 B | 1,42 s |
| PostgreSQL fuente | 26.607.616 B | 499.183.616 B | 6,81 s |
| SQL Server fuente | PID 1 no representa el motor | 1.150.980.096 B | 16,63 s |
| Web | 5.767.168 B | 128.974.848 B | 0,38 s |

El pico combinado de memoria de contenedores muestreado fue 2.697.222.681 bytes.
El plan Recon calculó 5.097.337.488 bytes de trabajo para 212.389.062 bytes de
entrada, frente al límite blando aislado de 6 GiB. El HWM menor no invalida el
planner: este fixture y estas reglas no cubren el peor caso que el estimador protege.

La evidencia saneada está en
`.codex-local/benchmarks/trackvance-bench-22852-894497/result.json`. El CSV se borra
tras la ejecución. No se guardan ni imprimen passwords.

## Comparación compresible y límites de interpretación

Una ejecución previa con payload repetido `x` y la misma forma terminó `PASS` en
52,87 s. Sus Parquet externos midieron apenas 26.909 bytes, su HWM worker fue
700.846.080 bytes y no ejercitó presión representativa sobre artifacts o temporal.
Se conserva como comparación controlada, no como baseline de capacidad. Dos errores
anteriores del harness quedaron diferenciados de fallos del motor: nginx rechazó
el primer upload con 413 y un timeout de fuente de 900 s violó el contrato máximo de
60 s. Se corrigieron solo en el overlay/runner y se repitió el ciclo completo.

El resultado principal usa strings anchos de alta cardinalidad, pero sigue siendo
una sola forma de 50.000 × 4, una sola concurrencia y una ejecución corta. No mide
matrices de muchas columnas, binarios, XLSX, Parquet de entrada, joins con gran
duplicación, presión concurrente ni hardware distinto. Los tiempos no son un SLA.

## Tiers no ejecutados

| Tier | Estimación Recon de dos entradas | Estado |
| ---: | ---: | --- |
| 100 MiB | 4,69 GiB (5,10 GB con bytes reales) | PASS |
| 500 MiB | 23,44 GiB | NOT_RUN_RESOURCE_LIMIT |
| 1 GiB | 48 GiB | NOT_RUN_RESOURCE_LIMIT |
| 2 GiB | 96 GiB | NOT_RUN_RESOURCE_LIMIT |
| 5 GiB | 240 GiB | NOT_RUN_RESOURCE_LIMIT |

Los cuatro tiers mayores rebasan el planner aislado de 6 GiB y/o Docker después de
la reserva, por lo que no se generaron archivos ni se iniciaron cargas. El resultado
no prueba que esos volúmenes sean imposibles; indica que necesitan otra validación y,
probablemente, un motor de volumen con spill/cancelación reales. No se elevan límites
productivos a partir del margen de este único benchmark.

El smoke CI de 1.074.923 bytes y 1.000 filas también terminó `PASS` en 37,71 s. Su
pico combinado fue 313.366.936 bytes y su temporal máximo igualó el upload
(1.074.923 bytes). Ese smoke detecta roturas del harness y módulos; no cuenta como
certificación de 100 MiB.
