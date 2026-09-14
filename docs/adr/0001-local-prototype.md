# ADR 0001: primer prototipo funcional local

Fecha: 2026-09-10. Estado: adoptado para el prototipo 0.1.

## Contexto

La especificación Trackvance Core v1.1 define una plataforma completa, con
React/TypeScript/Vite, FastAPI, PostgreSQL, procesamiento Python y workers
separados. El encargo inicial es montar el prototipo local en el repositorio
TrackvanceCore existente. El repositorio contenía únicamente su README.

## Decisión

Mantener un solo repositorio, una sola API, una sola identidad y evidencia
compartida entre Intake, ReconOps, Sentinel y Excepciones. La interfaz consume
la API; nunca accede a la base de datos ni procesa archivos de negocio por su
cuenta. Los trabajos de los tres módulos se ejecutan en un proceso worker.

Ofrecer dos arranques:

1. Prototipo directo en Windows: SQLite, archivos en `.local/storage`, API,
   worker e interfaz en procesos locales. Evita depender de Docker Desktop
   para la primera revisión y persiste los cambios entre reinicios.
2. Docker Compose: PostgreSQL, API, worker y Nginx, con volúmenes persistentes
   y exposición de la interfaz únicamente en loopback.

La sesión de demostración se inicia mediante una acción explícita del usuario
y requiere `DEMO_SEED_ENABLED=true`. No representa autenticación endurecida
para despliegue público. Los datos de ejemplo pertenecen a una organización
ficticia y se generan de forma reproducible.

## Límites deliberados

Esta entrega es un primer corte funcional. No declara cumplida toda la
especificación v1.1. Admite CSV UTF-8 de hasta 10 MiB y 100.000 filas; el
profiling inicial de la carga es síncrono y acotado. Intake implementa un
subconjunto de reglas, Recon comparación por claves y tolerancia decimal
absoluta, y Sentinel comprobaciones explicables de salud.

Quedan para ampliar: XLSX, mapeos y transformaciones completos, administración
de permisos y organizaciones, versionamiento editable completo de contratos,
planificador multimotor con DuckDB/PySpark, leases de trabajo y recuperación
robusta, retención, backup/restore y pruebas de gran volumen. La separación
hexagonal por contexto del documento se completará al ampliar los módulos;
esta base inicial mantiene una organización Python más pequeña.

## Consecuencias

SQLite permite revisar el producto en el equipo actual, pero no prueba las
garantías de concurrencia de PostgreSQL. Las pruebas locales y la validación
de Compose se informan por separado. Los resultados técnicos y los de negocio
se muestran por separado: un trabajo `SUCCESS` puede producir datos rechazados
o discrepancias. Los resultados antiguos conservan su configuración y fuentes.
