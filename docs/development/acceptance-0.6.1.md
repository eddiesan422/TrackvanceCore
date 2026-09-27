# Aceptación funcional 0.6.1

Los 22 criterios del encargo se vinculan a comprobaciones concretas. Los
resultados ejecutados y su procedencia se consolidan en
[validation.md](validation.md); esta matriz no sustituye esos resultados.

| Criterio | Comprobación y evidencia |
| --- | --- |
| 1. Alta sin email | Adaptador/servicio SMTP retirados; prueba de ausencia de filas/eventos nuevos. |
| 2. Temporal una sola vez | DTO separado en POST; modal obligatorio; GET sin secreto. |
| 3. Sin persistencia plaintext | Argon2; scans PostgreSQL/logs/artifacts/backups y pruebas de errores. |
| 4. Copiar temporal | Tres acciones del modal; componentes y navegador. |
| 5. Cierre impide recuperación | Estado eliminado, desmontaje/pagehide, cachés, DOM y storage inspeccionados. |
| 6. GET sin temporal | Pruebas de usuario individual, listado, /me y auditoría. |
| 7. Regenerar produce otra temporal | Endpoint nuevo + CAS, expiración renovada, envelope efímero. |
| 8. Password anterior inválida | API y navegador prueban login rechazado tras regeneración/cambio. |
| 9. Sesiones revocadas | API y navegador conservan una sesión previa y verifican rechazo. |
| 10. Primer acceso obligatorio | Sesión limitada; cambio local/SSO; rotación de sesión y CSRF. |
| 11. Expiración 24h | Pruebas unitarias/API y fixture vencida en proyecto desechable. |
| 12. RBAC conservado | 41 permisos, 105 rutas protegidas; rol dinámico, propagación y último admin. |
| 13. SSO mock conservado | Microsoft personal/organizacional y Gmail/Workspace, PKCE/JOSE/state/nonce. |
| 14. SSO disabled por defecto | Compose/.env y UI real de Autenticación; local habilitado. |
| 15. Delivery sin regresión | PostgreSQL16/18 y SQL Server; estrategias/audit/policy/drift/UNKNOWN/linaje. |
| 16. Cero nuevas notificaciones | Conteos antes/después de alta/regeneración y restore060. |
| 17. Historia060 preservada | Restore auténtico060 y comparación state5; copia PDF060 archivada. |
| 18. Alembic0012 | Paridad ORM, ciclo real PostgreSQL y hashes0001..0012; sin0013. |
| 19. Principal en061 | Backup previo, upgrade con volúmenes conservados, cinco servicios y footer. |
| 20. CI final completo | Nueve jobs sobre SHA final, registrados en informe de entrega posterior al commit. |
| 21. Docs/PDF coherentes | Fuente v1.1, capítulo35, OpenAPI, resultados y revisión de todas las páginas. |
| 22. SMTP no requerido | Código/configuración/UI retirados; referencias históricas explícitas y backlog. |

La instalación principal se valida con navegación de lectura. Las pruebas de
alta, regeneración y datos de negocio se ejecutan exclusivamente en proyectos
desechables. El informe final contiene el SHA documental y su workflow observado
después de publicar, sin introducir una referencia circular en el PDF.
