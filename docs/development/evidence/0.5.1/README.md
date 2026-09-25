# Evidencia de Trackvance Core 0.5.1

Ejecuciones nuevas del 25 de septiembre de 2026 sobre la baseline
`e7838c3c86b2117605a889f23242e75d9b09577d`. Ningún PASS de 0.5.0 se hereda.
La consolidación y el estado de cada gate están en [validación](../../validation.md).

| Evidencia | Qué demuestra |
| --- | --- |
| [backend-tests.json](backend-tests.json) | Pytest backend/scripts, Ruff, Mypy, snapshot OpenAPI y migraciones históricas sin cambios. |
| [frontend-tests.json](frontend-tests.json) | ESLint, TypeScript, 154 Vitest y siete escenarios Playwright con API interceptada; no se presentan como SQL real. |
| [frontend-bundle.json](frontend-bundle.json) | Build medido antes/después, archivos, bytes minificados/gzip y advertencia Vite. |
| [connections-e2e.json](connections-e2e.json) | PostgreSQL/SQL Server reales; adquisición, temporales, refresh, historia, consumidores y entrega sin coerción. |
| [delivery-e2e.json](delivery-e2e.json) | Cuatro estrategias, permisos, guards, métricas PG16/18, UI focal, restart y secretos. |
| [recovery-operations.json](recovery-operations.json) | Backup/restore real con reparación concurrente y revisión UNKNOWN persistida; conserva también la procedencia de los fallos corregidos. |
| [compose-e2e.json](compose-e2e.json) | Doctor, Alembic, 84 smoke, 24 Playwright y seis opt-in omitidos, huella exacta después de restart. Ejecución local anterior a los dos escenarios de publicación tardía, cubiertos por el CI final. |
| [benchmark-smoke.json](benchmark-smoke.json) | Smoke general nuevo y acotado; no se presenta como prueba de Delivery ni certificación de volumen. |
| [legacy-restore.json](legacy-restore.json) | Restauraciones auténticas 0.4.1 y 0.5.0, contratos/bytes conservados y comparación de backup nuevo/antiguo sobre runtime 0008. |
| [ci-attempts.json](ci-attempts.json) | Primeros workflows y fallos observados, sin presentarlos como CI de cierre. |
| [ci.json](ci.json) | Ocho jobs SUCCESS sobre código final 8927ea0; resultados y 32 escenarios de navegador únicos, sin sumar repeticiones. |
| [pdf-verification.json](pdf-verification.json) | Revisión visual completa de 52 páginas, hashes y comparación de 52 PNG candidato/publicado idénticos. |
| [delivery-benchmark-smoke/result.json](delivery-benchmark-smoke/result.json) | Primer smoke FAIL por mapping del fixture, antes de escribir; no se oculta ni se cuenta como éxito. |
| [delivery-benchmark-smoke-retry/result.json](delivery-benchmark-smoke-retry/result.json) | Smoke repetido, ocho casos reales PASS y limpieza propia. |
| [delivery-benchmark-representative/result.json](delivery-benchmark-representative/result.json) | Ocho casos reales de 20.000 filas; fases, throughput, recursos y comprobación SQL independiente. |

Los resultados de runners separados no se suman como escenarios únicos. Los
IDs pertenecen a fixtures sintéticos y proyectos aislados; no son datos del
usuario. No se publican backups, dumps, claves, contraseñas, `.env` ni archivos
de población. Los planes/recibos de limpieza del benchmark contienen inventarios
de recursos desechables, no material de autenticación.

UNKNOWN por pérdida exacta de confirmación post-commit mantiene
`NOT_RUN_NONDETERMINISTIC`. El fixture de recuperación es
`SIMULATED_UNKNOWN_NO_REMOTE_IO`: verifica persistencia e inmutabilidad, no esa
ventana de red. La pérdida local de evidencia se inyecta sólo tras un commit
PostgreSQL confirmado; la reparación no contacta al DataSink.

Los tiers Delivery de 100/500 MiB y 1/2/5 GiB mantienen
`NOT_RUN_RESOURCE_LIMIT` por el límite de entrada vigente, sin inferir que el
hardware no podría ejecutarlos. Los límites del producto no aumentaron.
Recursos muestreados no certifican máximos universales ni percentiles. Ver el
[método completo](../../delivery-benchmark-results-0.5.1.md).
