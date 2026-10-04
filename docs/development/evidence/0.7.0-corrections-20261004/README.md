# Evidencia intermedia C01–C06 — 4 de octubre de 2026

[formal-393b7e2-browser-fail.json](formal-393b7e2-browser-fail.json) conserva el **FAIL global** del formal con implementación `393b7e25e413bf641d5483c25c53951642611f51` y árbol limpio. Los ocho tiers, cadena API/SQL, cancelación, crash/lease, exceso de filas y dispatch aprobados no convierten el formal en PASS. Su primer navegador de 400.000 filas falló; navegadores restantes, recuperación nativa y gates finales tienen resultados pendientes y reportes separados.

La copia conserva todas las métricas, hashes, identidades propias, etapas muestreadas y valores null/censurados. Se sustituyeron exactamente 16 campos de rutas de fixtures por sus filenames; el archivo privado original no se modificó. No contiene contraseñas, tokens, CSRF, DSN ni URLs con credenciales. Las fixtures son sintéticas con oráculo independiente; no representan originales del usuario que no estaban disponibles.

- SHA-256 del informe privado original: `e908476ec15682de63457a2e41879d84cc3523ec59766be6c1fd57f22598546d`.
- SHA-256 de la copia pública saneada: `c3bd9e8b994fe1076da024b939abf9b47ea0b4ff886229a56bf65c0f53fa563b`.

La recuperación registrada en `recovery` es **crash/lease de adquisición**, no backup/restore nativo. Los intervalos de stage y recursos son observaciones nominales de 1/2 s. Las esperas C05 están censuradas y la contabilidad de CPU/eventos en crash/lease es incompleta tras un reinicio. [Validación](../../validation.md) explica alcances y gates pendientes; [corrections_results](../../../specification/corrections_results_0.7.0.json) resume el ciclo todavía OPEN.

## Formal e75e1038 — repetición completa

[formal-e75e1038-pass.json](formal-e75e1038-pass.json) conserva PASS del nuevo formal con árbol limpio en `e75e1038f845979da8cdf7cc38de91bfe359d349`, y producto congelado `393b7e25e413bf641d5483c25c53951642611f51`. Ocho tiers, cadena íntegra, controles, ambos navegadores y backup/restore nativo se ejecutaron de nuevo; las métricas pertenecen sólo a este reporte. El FAIL 393 y sus focales permanecen arriba como antecedentes independientes.

- SHA-256 privado original: `3a21e5ca4f82dfa4b1e9f002789d0f2724e389cf7dcc5afa27952333ee829258`.
- SHA-256 público saneado: `03a7f6bd9c2533b0237e1e83b5216b8350a95f1c344f6b8ad59201d13a4a0eff`.
- Rutas de fixture sustituidas por filename: 16. No se publican dumps, secretos, DSN ni archivos de entorno.

El ciclo continúa OPEN: CI del HEAD final y upgrade real de main tienen gates externos pendientes. El [CI 393 completado](ci-393b7e2-failures.json) conserva 11 SUCCESS/4 FAIL sin heredar aprobaciones posteriores. Los null, ventanas aproximadas, presión de memoria y contadores incompletos/censurados permanecen exactos en el informe.

## PDF final corregido

[pdf-verification.json](pdf-verification.json) acredita revisión fresca de las 120 páginas, 246 marcadores/38 secciones, once inputs congelados, segunda generación byte idéntica y copias oficiales completas. SHA del PDF: `493c0d37fd087c70d667832d896b90ade4202d618e99874e73b23bb90812b476`. Los doce antecedentes archivados y la prueba histórica inicial permanecen íntegros. Los gates CI/main se registrarán en el informe externo fechado del SHA final.
