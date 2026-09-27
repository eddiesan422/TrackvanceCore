# Evidencia de ejecución 0.6.0

Baseline revisada `4519ed354202ea8f220682758da234e07b6df3ed`, rama
`feat/local-prototype`. Los JSON corresponden a ejecuciones nuevas de esta
versión. Las suites usan fixtures de proyectos desechables; `local-installation`
registra aparte conteos, identificadores y hashes de la instalación existente,
sin nombres de personas ni contenido de sus datos.
No se publican dumps, backups, claves, cuerpos de email, cookies ni tokens.

| Archivo / directorio | Alcance |
| --- | --- |
| backend | Pytest/scripts, Ruff, Mypy y migración PostgreSQL real. |
| frontend | Componentes/build y capturas sintéticas de UI sin credenciales. |
| identity-sso-e2e | SMTP Mailpit, primer login, RBAC, RS256 OIDC mock, expiración, fallo/regeneración y restart. |
| real-provider-results.json | Cuatro casos externos NOT_RUN_EXTERNAL_CREDENTIALS; no equivalen al mock. |
| compose-e2e | Smoke API, navegador general, health/doctor y persistencia. |
| connections-e2e | PostgreSQL/SQL Server reales y recorrido de adquisición por módulos. |
| delivery-e2e | Motores SQL reales, audit columns, política, SSO interno, drift y UNKNOWN. |
| native-recovery-identity | Backup 0.6.0 y restore nuevo con todas las clases de estado nuevas presentes. |
| legacy-restore-051 | Build auténtico 4519ed3/0009/state4 y restore 0012/state5 con proyección exacta. |
| legacy-restore-041-050 | Nuevos restores de backups auténticos 0.4.1 y 0.5.0; artifacts y secretos verificados. |
| clean-demo | Arranque vacío sin seed, acceso demo y navegador, con limpieza del proyecto desechable. |
| local-installation | Backup verificado y actualización no destructiva de 0.5.0 a 0.6.0, doctor y navegador. |
| ci-product.json | Los nueve jobs SUCCESS del commit de producto 35c88fa, intento 1. |
| ci-product-initial-979cc0f.json | Primer workflow: ocho SUCCESS y un FAIL del verificador identity, corregido en un commit nuevo. |
| pdf-verification.json | Revisión visual de todas las páginas antes de publicar, hashes y coincidencia con copia oficial. |
| benchmark-smoke | 1.000 filas, cuatro columnas, 1.074.923 bytes; harness acotado. |
| delivery-benchmark-smoke | Cuatro estrategias por motor; ocho mediciones, sin extrapolación productiva. |

La pérdida de confirmación Delivery se inyecta deliberadamente en el adapter
después de un commit SQL real; demuestra UNKNOWN y la recuperación explícita,
pero no certifica una ventana física de fallo de red. No hay replay automático.

Las pruebas nativas de recovery incluyen una identidad externa y un intento OIDC
consumido como fixtures declarados; los flujos OIDC reales de aplicación con mock
firmado se certifican por separado. SENT en SMTP significa aceptación del servidor.

Los detalles de fallos iniciales, omisiones, avisos de librerías y resultados
finales están en [validation.md](../../validation.md). Las capturas/trazas de
sesiones con contraseñas se deshabilitan; no se exportan credenciales temporales.
