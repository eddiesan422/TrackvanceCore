# Validación del ciclo C01–C06 de Trackvance Core 0.7.0

Baseline `12ca7061696d3581a18237dc7737348a3462e2c4`, rama `feat/local-prototype`. Los [resultados iniciales](validation-0.7.0-initial.md) se conservan como antecedentes. Este ciclo permanece ABIERTO hasta pruebas completas, PDF revisado, CI del HEAD final y upgrade real verificado. Ningún verde previo se hereda.

## Gates de esta corrección

| Alcance | Estado actual y condición |
| --- | --- |
| C01 adquisición XLSX | En certificación: defaults normales, inline/shared 100000/100001/400000/1000000; oráculo completo, todas las filas y valores, inferencia tardía, encabezado físico 4 y dimensiones falsas. |
| C02 diagnóstico y límites | Implementado; suite completa pendiente. Preserva errores históricos y publica detalles/referencia seguros únicamente cuando están disponibles. |
| C03 áreas | Implementado; casos HTTP y componente con 151 datasets propios y exclusión de otra organización. La suite completa y ambos formularios de navegador son gates adicionales. |
| C04 timezone | Implementado; fechas inválidas bloqueadas, ancla UTC histórica/cursor preservados y DST existente. Suite completa y creación/edición/reapertura de navegador pendientes. |
| C05 despacho | Implementado; instrumentación contra I/O y corrupción focal. Gate real PostgreSQL: cuatro programaciones sobre dos datasets/versiones distintos de 1 M, targets distintos y quinto worker ocupado; setup, dispatch y espera de cola separados. |
| C06 bandeja | Implementado; setter idempotente con ámbito personal/permisos. Gates de recarga, logout/reinicio y preservación por backup/restore pendientes. |
| Regresión | Nueva ejecución completa de backend/scripts/frontend/lint/types/build/contratos, PostgreSQL, Spark Local/Standalone y suites Docker/SQL/E2E originales. |
| Recuperación | Huella actual state 7/0016 de 42 tablas; state 6/0015 soportado mediante proyección estricta de sólo los dos diagnósticos NULL. Backup/restore real nuevo pendiente. |
| Documento v1.1 | Fuente, generador e inputs corregidos; PDF candidato, extracción y revisión visual de todas las páginas pendientes. |
| Publicación | Sólo feat/local-prototype; todos los jobs del SHA final deben finalizar SUCCESS sin omitir gates. |
| Main | Sigue intacto durante certificación; upgrade final autorizado con backup nuevo verificado, mismos seis volúmenes/puertos/secretos y sólo migración 0016. |

## Resultados de desarrollo conservados

La primera prueba real de 400000 registros inline publicó la población completa y conservó todos los valores. La comprobación independiente rechazó numeración ordinal 1..400000 en lugar de filas físicas 5..400004. Ese FAIL y su duración de adquisición (55.947177 s) se conservan; no certifican la corrección. La revisión del lector mantiene números físicos y tiene regresiones de huecos y encabezados anteriores.

La segunda prueba de 400000 filas verificó valores, numeración física y perfil, pero el mapping del harness convirtió DATE a STRING. Preflight terminó técnicamente SUCCESS con decisión FAIL, y la publicación se bloqueó correctamente con VALIDATION_BINDING_MISMATCH. El harness ahora exige SUCCESS+PASS y respeta los tipos lógicos completos. Este intento permanece FAIL y separado de posteriores ejecuciones.

Las mediciones por etapa se obtienen por polling de 1 s y sondas de recursos de 2 s: son ventanas observadas, no relojes internos exactos. La lectura incluye indexación SST/XML. Los picos RSS/cgroup/temporales y deltas CPU se distinguen de las cotas de caché, lote y motor; una etapa no observada no se inventa.

Ver [ADR 0025](../adr/0025-corrections-c01-c06.md), [operación](operations.md), [parámetros](parameters-0.7.0.md) y [Spark](spark-volume-0.7.0.md).

La ejecución completa de desarrollo del 4 de octubre reunió 1696 PASS, 17 SKIP explícitos y un FAIL en cancelación durante preparación local de Delivery (286.67 s). Se conserva como intento intermedio y exige investigación/repetición; no es un gate aprobado. La revisión actual de frontend produjo 223 PASS (15.38 s), lint/types/build PASS. Mypy 63 módulos y Ruff del alcance CI PASS. Los fixes posteriores de setters de lectura tienen 28 pruebas focales PASS; sus resultados completos definitivos se registrarán por separado.
