# ADR 0014: política de benchmark de volumen local

- Estado: baseline de 100 MiB implementado y medido; tiers mayores condicionados.
- Fecha: 2026-09-19.

## Decisión

Los límites se sustentan con cargas reales en un proyecto Docker desechable. El
runner `scripts/tests/benchmark_cycle.py` genera un CSV streaming determinista,
ejecuta uploads, Intake, Recon y Sentinel, y adquiere snapshots de PostgreSQL y SQL
Server reales con cuentas de solo lectura. Registra bytes/filas/columnas, SHA-256,
artifacts, planes, tiempo, filas/s, bytes/s, CPU, HWM/RSS, cgroup, block I/O y uso
observado de storage temporal. Un watchdog detiene solo el proyecto benchmark si
consume la reserva de Docker, quedan menos de 10 GiB de disco o vence el tiempo.

Los overrides viven en `deploy/docker/compose.benchmark.yml`: upload 128 MiB,
snapshot 160 MiB, planner 6 GiB, timeout 1200 s y límites duros por contenedor.
No cambian los defaults de producto: upload 10 MiB, snapshot 64 MiB, planner
512 MiB y timeout 300 s. `TRACKVANCE_MAX_SNAPSHOT_BYTES` exige entero positivo y
falla al arrancar si su valor explícito es inválido.

El fixture por defecto usa payload por fila derivado de SHAKE256 con semilla pública
y codificación URL-safe (`SHAKE256_URLSAFE_V1`). Esto reduce la compresibilidad y
hace repetible el archivo sin conservar 100 MiB en Git. `--fixture-mode compressible`
mantiene un caso controlado de valores repetidos para comparar. Los resultados no
se extrapolan entre distribuciones, cardinalidades, anchos de celda o hardware.

## Tiers y estados

Se evalúan 100, 500, 1024, 2048 y 5120 MiB. Cada tier termina en `PASS`, `FAIL` o
`NOT_RUN_RESOURCE_LIMIT`. Este último es una omisión explícita, nunca un éxito.
Antes de crear el fixture se compara el cálculo real de `ExecutionPlanner` para un
Recon de dos entradas con el límite blando, memoria Docker menos 2 GiB y disco.
Después del baseline también se conservan los picos medidos.

En la máquina de referencia (16 CPU Docker, 15.21 GiB RAM Docker, 1.36 TiB libres)
el caso variado de 106,194,531 bytes, 50.000 filas y cuatro columnas completó los
tres módulos, snapshots PostgreSQL/SQL Server e Intake sobre ambos en 79.46 s.
Intake, Recon y Sentinel tardaron 3.08, 4.08 y 2.05 s; procesaron aproximadamente
16.218, 12.266 y 24.437 filas/s. El HWM fue 812,998,656 bytes en API y
1,051,635,712 bytes en worker; el pico combinado muestreado fue 2,697,222,681 bytes,
sin OOM ni watchdog. Se observaron 185,340,131 bytes máximos en `tmp` y crecimiento
de storage de 670,790,434 bytes. Los Parquet de archivo midieron 79,145,600 bytes;
los snapshots PostgreSQL y SQL Server, 55,099,067 y 55,107,251 bytes.

Como comparación, el caso compresible con igual forma terminó en 52.87 s y produjo
Parquet externos de solo 26.909 bytes. Es evidencia útil del flujo, pero subestima
ArtifactStore, disco temporal y memoria. Ninguno de los dos fixtures representa por
sí solo toda distribución, cardinalidad, cantidad de columnas o tipo de dato.

Los tiers de 500 MiB, 1 GiB, 2 GiB y 5 GiB no se ejecutaron: el cálculo Recon es
aproximadamente 24, 48, 96 y 240 GiB frente al override de 6 GiB. Esa decisión no
demuestra imposibilidad ni justifica cambiar límites productivos; exige otra
arquitectura/motor o una validación nueva. El amplio margen entre estimación Recon
(5,10 GB) y los picos medidos del worker (670.774.067 bytes muestreados y HWM de
1.051.635.712 bytes) tampoco basta para elevar defaults a partir de un único fixture
variado.

## CI y uso local

CI ejecuta un smoke no certificante y de bajo consumo:

```powershell
python scripts/tests/benchmark_cycle.py --target-mib 1 --rows 1000 --file-only --max-wall-seconds 600
```

La certificación local completa usa los defaults del runner. El resultado saneado
es `result.json`; fixtures, volúmenes, passwords y backups con claves no se publican.
Una ejecución con `--keep` conserva únicamente recursos creados después de probar
que el proyecto estaba vacío. Si encuentra cualquier recurso previo, falla y no
ejecuta limpieza.
