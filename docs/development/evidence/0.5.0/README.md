# Evidencia local 0.5.0

Resultados saneados de los runners aislados. No contienen backups, claves,
payloads de credenciales ni filas completas del negocio. Los IDs pertenecen a
fixtures desechables; los entornos de prueba fueron eliminados al terminar.

- `delivery.json`: 92 comprobaciones reales, PostgreSQL/SQL Server y Playwright
  focal PASS; UNKNOWN real no ejecutado por falta de reproducción determinista.
- `connections.json`: 62 comprobaciones reales, dos escenarios Playwright y
  smoke API PASS, incluido el contrato de temporales sin zona.
- `recovery.json`: origen destruido, restauración 0.5.0 y uso efectivo de la
  credencial de destino recuperada; siete artifacts, dos secretos y 127 relaciones.
- `compatibility-0.4.1.json`: baseline exacto `92c58eae9a1ddef653c0c9c888cfd7698ee1af3a`,
  restore de manifest 1/state 2/0007 en 0008 y proyección legacy idéntica.
- `ci.json`: resumen verificado del workflow de producto, siete jobs SUCCESS y
  cruce de los 25 escenarios distintos de navegador entre jobs.

Las pruebas locales se ejecutaron durante la implementación, antes del commit
de producto. El informe de compatibilidad conserva expresamente sus límites:
target construido desde el árbol de trabajo, ausencia de hash estructural en
state 2 y fixture legacy sin credenciales de fuente. La recuperación 0.5.0
separada sí verificó secretos de fuente y destino. La compatibilidad con el
baseline 0.4.1 se certificó localmente y no constituye un job adicional de CI.

El estado remoto, commit certificado y conteos consolidados están en
../../validation.md y en los logs del workflow allí enlazado.
