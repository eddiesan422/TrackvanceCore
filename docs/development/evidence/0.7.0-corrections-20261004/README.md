# Evidencia intermedia C01–C06 — 4 de octubre de 2026

[formal-393b7e2-browser-fail.json](formal-393b7e2-browser-fail.json) conserva el **FAIL global** del formal con implementación `393b7e25e413bf641d5483c25c53951642611f51` y árbol limpio. Los ocho tiers, cadena API/SQL, cancelación, crash/lease, exceso de filas y dispatch aprobados no convierten el formal en PASS. Su primer navegador de 400.000 filas falló; navegadores restantes, recuperación nativa y gates finales tienen resultados pendientes y reportes separados.

La copia conserva todas las métricas, hashes, identidades propias, etapas muestreadas y valores null/censurados. Se sustituyeron exactamente 16 campos de rutas de fixtures por sus filenames; el archivo privado original no se modificó. No contiene contraseñas, tokens, CSRF, DSN ni URLs con credenciales. Las fixtures son sintéticas con oráculo independiente; no representan originales del usuario que no estaban disponibles.

- SHA-256 del informe privado original: `e908476ec15682de63457a2e41879d84cc3523ec59766be6c1fd57f22598546d`.
- SHA-256 de la copia pública saneada: `c3bd9e8b994fe1076da024b939abf9b47ea0b4ff886229a56bf65c0f53fa563b`.

La recuperación registrada en `recovery` es **crash/lease de adquisición**, no backup/restore nativo. Los intervalos de stage y recursos son observaciones nominales de 1/2 s. Las esperas C05 están censuradas y la contabilidad de CPU/eventos en crash/lease es incompleta tras un reinicio. [Validación](../../validation.md) explica alcances y gates pendientes; [corrections_results](../../../specification/corrections_results_0.7.0.json) resume el ciclo todavía OPEN.
