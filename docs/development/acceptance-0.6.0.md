# Matriz de aceptación 0.6.0

Corresponde a los 36 criterios del encargo, sobre la baseline de repositorio
`4519ed354202ea8f220682758da234e07b6df3ed`. Es una matriz de trazabilidad, no una
declaración anticipada de cierre. `PASS_LOCAL` significa implementación y pruebas
locales observadas; no certifica el HEAD remoto final. Los gates de publicación se comprueban sobre su artefacto/commit definitivo. `PASS_MOCK` valida el flujo real de la aplicación con un proveedor
OIDC local firmado; no equivale a probar Microsoft/Google externos.

Las cifras de cada ejecución permanecen en sus JSON; no se suman casos repetidos
ni se convierten SKIP/NOT_RUN en PASS. La congelación final de código, la publicación
del PDF y GitHub Actions se registran por separado en [validación](validation.md).

| Nº | Criterio | Estado al corte | Implementación y evidencia verificable |
| --- | --- | --- | --- |
| 1 | Roles persistentes y dinámicos | PASS_LOCAL | Role/RolePermission, consultas por petición en permissions.py; [tests de identidad][TI] y [E2E identidad][I], con roles/grants persistidos tras restart. |
| 2 | Administrator tiene todo y está protegido | PASS_LOCAL | system_key ADMINISTRATOR resuelve el catálogo completo, bloquea nombre/actividad/grants y protege al último administrador; tests protected_administrator y user_disable_delete en [TI]. |
| 3 | Roles custom configurables | PASS_LOCAL | API CRUD por organización, editor de catálogo y dependencias; [TI], [componentes y revisión UI][F], [I]. |
| 4 | Roles asociados no se desactivan/eliminan | PASS_LOCAL | assert_role_can_retire incluye usuarios inactivos no eliminados; test retire_role y recorrido UI [TI][I]. |
| 5 | Cambio de permisos afecta cuentas existentes | PASS_LOCAL | effective_permissions consulta rol vigente; UI refresca /me sin exigir logout; test dynamic_role_changes y E2E permisos [TI][I]. |
| 6 | Sin escalación fácil vía roles | PASS_LOCAL | Catálogo cerrado de 41 permisos, 104 asignaciones explícitas de ruta, fail-closed, no delegación de users:manage/roles:manage; [revisión independiente][S], tests de corrupción, IDOR y rutas desconocidas [TI]. |
| 7 | Login por username/email | PASS_LOCAL | Identificador normalizado, exactamente uno de username/email; usernames persistidos y reservados tras baja; [TI], [I], [migración histórica][M]. |
| 8 | Alta genera temporal | PASS_LOCAL | CSPRNG de 32 caracteres, expiración 24 h, hash Argon2; no password elegido por admin; tests generated_credentials y [I]. |
| 9 | Email mediante NotificationDelivery | PASS_LOCAL / SMTP externo NOT_RUN | NotificationService → NotificationDelivery → SMTPNotificationDelivery; SMTP Mailpit real y lectura del correo en [I]; SMTP externo sin credenciales en [X]. |
| 10 | Password sin persistencia plaintext | PASS_LOCAL | Sólo hash local, body en memoria, metadata de entrega saneada; escaneo de dump/logs y error de SMTP en [I], tests [TI]. |
| 11 | Primer login obliga a cambiar password | PASS_LOCAL | Sesión restringida a /me/logout/change-password; expiración, password distinto, rotación y revocación bajo lock; [TI][I]. |
| 12 | SSO primer login también define password local | PASS_MOCK | Callback conserva must_change_password; recorrido restringido y cambio local en [tests OIDC][TS] y [I]. No confundir con acceso a proveedor externo. |
| 13 | Microsoft work/school y personal | PASS_MOCK / NOT_RUN externo | Autoridad common, issuer por tenant y autoridad de email para primer vínculo; perfiles personal y organizacional firmados en [TS][I]. Ambos proveedores externos quedan NOT_RUN_EXTERNAL_CREDENTIALS en [X]; [guía manual](sso-setup.md). |
| 14 | Google Gmail y Workspace | PASS_MOCK / NOT_RUN externo | Gmail verificado o Workspace con hd correspondiente; dos issuer oficiales normalizados a identidad estable; perfiles firmados en [TS][I]. Google real y Workspace real: [X]. |
| 15 | SSO no auto-provisiona | PASS_MOCK | link_user exige User activo existente y rechaza correos no preprovisionados; número de cuentas invariante en [TS], recorrido negativo [I]. |
| 16 | ExternalIdentity usa subject estable | PASS_MOCK | Único provider/issuer/subject y vínculo único user/provider; cambio de email externo conserva identidad; [TS][I]. |
| 17 | Disabled/deleted no entra por SSO | PASS_MOCK | Middleware, link_user y revalidación bajo lock; tests oidc_inactive_or_deleted y [I]. |
| 18 | Módulo de notificaciones reutilizable | PASS_LOCAL | Puerto, servicio, adaptador y records separados; sólo template USER_TEMPORARY_CREDENTIALS activo. ROLE/DOMAIN/GROUP preparados conceptualmente, sin routing implementado; [identidad](identity-060.md), [TI]. |
| 19 | Delivery activa fechaIngesta/usuario | PASS_LOCAL | Checkbox conjunto, audit_columns_enabled, preflight y policy; [tests de auditoría][TD], [SQL real y navegador][D]. |
| 20 | Tabla nueva crea campos | PASS_LOCAL | TIMESTAMPTZ(6)/VARCHAR(128) y DATETIMEOFFSET(6)/NVARCHAR(128), ambos NOT NULL en CREATE; [TD][D]. |
| 21 | Tabla existente conserva históricos NULL | PASS_LOCAL | ALTER nullable sin DEFAULT/backfill, junto con DML transaccional; inspección de datos antiguos y adopción parcial/completa en ambos motores [D], rollback [TD]. |
| 22 | Entregas posteriores no desactivan auditoría | PASS_LOCAL | Policy por fingerprint sin credencial/Destination-version; CHECK true, publicación transaccional, UI bloqueada y API manipulada rechazada; destinos duplicados y restart [TD][D]. |
| 23 | APPEND/OVERWRITE/UPSERT respetan auditoría | PASS_LOCAL | Valores internos parametrizados; ambos caminos UPSERT y todas las filas nuevas/actualizadas comparten timestamp del intento; SQL real [D] y [TD]. |
| 24 | Audit username procede de Trackvance | PASS_LOCAL | Run.execution_plan.initiated_by_username fijado al encolar, independiente de display name/email/usuario SQL; renombrado posterior no altera snapshot [TD]. |
| 25 | SSO usa el mismo username interno | PASS_MOCK + SQL real | OIDC mock Microsoft personal/Google Gmail, primer login y entrega con el username del User a PostgreSQL/SQL Server; [D]. Proveedores externos siguen [X]. |
| 26 | Drift detectable | PASS_LOCAL | Pérdida de columna materializada → AUDIT_COLUMNS_DRIFT; tipo incompatible → AUDIT_COLUMNS_INCOMPATIBLE; revalidación bajo lock y ninguna recreación automática; [TD][D]. |
| 27 | UNKNOWN conserva sus garantías | PASS_LOCAL / fallo físico NOT_RUN | Commit SQL real seguido de pérdida de confirmación inyectada en el adapter en ambos motores; policy permanece requerida, UNKNOWN intacto y sin replay, siguiente Run deliberada inspecciona/adopta. [D][TD]; ventana física no determinista de fallo de red sigue NOT_RUN_NONDETERMINISTIC. |
| 28 | Backup/restore conserva estado | PASS_LOCAL | Nativo 0.6.0: 9 artifacts, 2 secretos SQL, 269 relaciones y todas las clases nuevas no vacías [R]. Fuentes auténticas 0.5.1 [R51] y 0.4.1/0.5.0 [RL] conservan proyecciones y evidencia exactas. Fixture OIDC de recovery es sintética de persistencia, distinta de [I]. |
| 29 | Migraciones históricas sin modificar | PASS_LOCAL | Sin cambios respecto a HEAD en 0001..0009; sólo nuevas 0010/0011/0012. [M] verifica SQLite poblado y [P] PostgreSQL real, preservación de 24 tablas/8 enlaces de 0008 y roundtrip aislado. |
| 30 | E2E completos | PASS_LOCAL / CI_PRODUCTO_SUCCESS | 41 escenarios distintos de navegador cubiertos en entornos dedicados; todas las suites locales y nueve jobs del producto PASS. [Validación](validation.md) y [CI producto](evidence/0.6.0/ci-product.json). |
| 31 | Docker local actualizado a 0.6.0 | PASS | trackvance-certification: cinco servicios saludables, API/ambos workers 0.6.0, footer 0.6.0, Alembic 0012, doctor y restart=no. [Resultado](evidence/0.6.0/local-installation/result.json) y [navegador](evidence/0.6.0/local-installation/browser.json). |
| 32 | Datos existentes permanecen | PASS | Backup verificado, proyección state3/0008 exacta antes del login y snapshot state5 exacto tras restart. 3 datasets, 1 usuario, 1 conexión, 1 destino, 4 Runs, 9 artifacts y 2 secretos SQL conservados. [Resultado](evidence/0.6.0/local-installation/result.json). |
| 33 | OpenAPI coincide | PASS | 95 paths / 116 operaciones / 87 schemas; test exacto del runtime, snapshot vigente y contrato actualizado. [Backend](evidence/0.6.0/backend/result.json). |
| 34 | Documentación coincide | PASS | ADRs 0016..0019, identidad, permisos, SSO/SMTP, Delivery audit, arquitectura, operación y especificación contrastados con contratos reales. Históricos separados y límites explícitos. |
| 35 | PDF ampliado y verificado visualmente | PASS | 77 páginas y 151 marcadores; todas las páginas revisadas antes de publicar, bytes candidato/publicado idénticos. Hashes y procedencia en [pdf-verification.json](evidence/0.6.0/pdf-verification.json), generado después de la revisión y antes del commit documental. |
| 36 | GitHub Actions del HEAD final | GATE DE ENTREGA POSTERIOR AL COMMIT | El informe de entrega identifica el SHA final y su workflow de nueve jobs SUCCESS observado después de publicar. Esta tabla no anticipa un resultado ni utiliza el éxito de otro SHA como sustituto. El producto ya tiene [CI SUCCESS](evidence/0.6.0/ci-product.json). |

## Procedencia de la publicación

Las filas 35 y 36 remiten al registro visual y al informe final: el PDF no puede
contener el hash de su propio commit sin crear una referencia circular. El
informe final se entrega una vez observado SUCCESS del HEAD documental exacto.
Los proveedores externos conservan su NOT_RUN aunque el cliente y mock aprueben.
El backup no sustituye la copia privada de .env/external.env ni de secretos de
integración; ver [operaciones](operations.md).

[TI]: ../../backend/tests/test_local_identity.py
[TS]: ../../backend/tests/test_sso_identity.py
[TD]: ../../backend/tests/test_delivery_system_audit.py
[M]: ../../backend/tests/test_identity_migration.py
[S]: security-review-0.6.0.md
[I]: evidence/0.6.0/identity-sso-e2e/result.json
[X]: evidence/0.6.0/real-provider-results.json
[F]: evidence/0.6.0/frontend/checks.json
[D]: evidence/0.6.0/delivery-e2e/result.json
[R]: evidence/0.6.0/native-recovery-identity/result.json
[R51]: evidence/0.6.0/legacy-restore-051/result.json
[RL]: evidence/0.6.0/legacy-restore-041-050/result.json
[P]: evidence/0.6.0/backend/postgres-result.json
