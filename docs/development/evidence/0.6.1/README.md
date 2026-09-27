# Evidencia ejecutada 0.6.1

Baseline 0.6.0: `587909b`; producto 0.6.1: `4d3c656`. Cada resultado corresponde a una
ejecución observada. No se versionan backups, secretos ni evidencias crudas.
Los intentos fallidos se preservan como agregados saneados y no cuentan como PASS.

| Directorio/archivo | Alcance |
| --- | --- |
| backend | 1.192 pytest, 54 focales, Ruff, Mypy 42, OpenAPI 96/117/89, 41 permisos/105 rutas; PostgreSQL y hashes de 12 migraciones |
| frontend | 174 Vitest/20 archivos, lint, tipos, build y privacidad de cachés/estado |
| compose | 26 Playwright, 84 smoke, copia real, scan de 14 secretos RAM, restart; attempts.json con dos fallos previos |
| identity | 9 Playwright, 4 OIDC mock, scan de 15 secretos RAM, expiración/regeneración, cero nuevas notificaciones |
| clean-demo | 1 Playwright, seed=false, demoaccess=true, colecciones vacías |
| connections | 96 comprobaciones, 30 Playwright, 84 smoke, dos motores SQL y preservación temporal |
| delivery | 308 comprobaciones, 1 Playwright, PostgreSQL 16/18 + SQL Server, auditoría/policy/UNKNOWN/linaje |
| native-recovery | 9 artifacts, dos secretos SQL, 269 relaciones y backup decodificado sin plaintext |
| restore-0.6.0 | Build auténtico 587909b, state 5 exacto, historia SMTP preservada, 82 archivos escaneados |
| restore-0.5.1 | Build auténtico 4519ed3, proyección state 4 exacta, 80 archivos escaneados |
| benchmark | Smoke general file-only, 1.000 filas; sin extrapolar capacidad |
| delivery-benchmark | 8 casos smoke, misma entrada, versión 0.6.1 |
| local-installation | Backup 0.6.0, upgrade 0.6.1, estado 5 exacto, cinco healthy, doctor/restart/UI sin mutaciones de negocio |
| ci-product.json | Workflow del SHA de producto, nueve jobs SUCCESS |
| real-provider-results.json |Microsoft/Google reales NOT_RUN_EXTERNAL_CREDENTIALS; SMTP retirado |
| pdf-verification.json | Publicación posterior a revisión de todas las páginas, hashes/procedencia/archivos oficiales |

Las garantías, límites y fallos se explican en [validation.md](../../validation.md).
El HEAD documental y su workflow final se registran después de ejecutar CI en
el informe externo de entrega para evitar la referencia circular del commit.
