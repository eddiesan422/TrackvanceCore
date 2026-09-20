# ADR 0011: gestión operativa y resolución automática de excepciones

- Estado: implementado en el prototipo local 0.4.0.
- Fecha: 2026-09-19.
- Amplía ADR 0005; conserva la validación técnica de la configuración original.

## Decisión

Los casos incorporan asignación estable a un usuario activo con permiso de gestión
de la misma organización, prioridad independiente de la severidad del hallazgo,
SLA en horas, fecha objetivo, comentarios y adjuntos inmutables. El propietario
en texto se conserva para históricos; no se convierte automáticamente un nombre
ambiguo en identidad de usuario. Cambiar el nombre o desactivar al usuario no
elimina su asignación histórica.

El flujo añade `ASSIGNED` y `REOPENED`:

```text
OPEN -> ASSIGNED -> INVESTIGATING -> PENDING_VALIDATION -> RESOLVED
           |              |                   |
           +--------------+-------------------+-> cierre administrativo
RESOLVED / cierre administrativo -> REOPENED -> ASSIGNED / INVESTIGATING
```

Se conserva `OPEN -> INVESTIGATING` y la reapertura histórica hacia `OPEN` para
clientes existentes. `WAITING_EXTERNAL` y `FALSE_POSITIVE` siguen siendo legibles.
Reabrir exige comentario y registra `reopened_at`. Una ejecución encolada antes de
esa reapertura no puede volver a justificar el cierre; la evidencia del cierre
anterior permanece en el timeline. Se reinicia el plazo SLA desde la reapertura.

El SLA se calcula desde creación/reapertura; una fecha objetivo explícita prevalece.
La condición de vencimiento se calcula con UTC, fecha objetivo y estado abierto.
No se introduce un calendario laboral implícito. Prioridad y SLA son decisiones
operativas; nunca alteran severidad, resultados o ejecuciones originales.

`RESOLVED` exige la ejecución `SUCCESS` más reciente creada después del origen
o reapertura, de la misma configuración inmutable. Las nuevas reglas Intake usan
su `rule_id`: la regla debe estar presente, evaluar al menos una fila y tener cero
fallos. Una condición sin filas elegibles no demuestra corrección. Configuraciones
históricas sin `rule_id` identifican sus métricas por código/columna o por el
fingerprint `SHA256(code:column)`. También deben demostrar filas evaluadas y cero
fallos; si ese fingerprint agrupaba varias reglas, todas sus métricas deben pasar.
La ausencia de un Finding, por sí sola, no demuestra corrección: `IGNORE`, una
condición sin filas elegibles o un dataset vacío quedan bloqueados con
`INTAKE_RULE_NOT_EVALUATED`. Si una ejecución histórica no conserva métricas
identificables y contadores suficientes, se informa
`INTAKE_RULE_EVIDENCE_INSUFFICIENT`: se requiere otra ejecución de la misma
configuración con el motor actual. No se inventan contadores ni se modifican
ejecuciones o cierres `RESOLVED` históricos. ReconOps conserva la ausencia del hallazgo asociado/conformidad; Sentinel
exige `HEALTHY`.

`auto_resolve_enabled` es una política explícita por caso, falsa por defecto.
Cambiarla requiere `exceptions:close`. El worker solo resuelve automáticamente
casos `PENDING_VALIDATION` habilitados que superan la misma comprobación técnica.
Registra actor `SYSTEM`, evento `EXCEPTION_AUTO_RESOLVED`, política `CASE_OPT_IN`,
run confirmatorio y evidencia. No se inventa causa raíz: la resolución automática
puede usar el motivo técnico y conserva cualquier explicación humana existente.
Reintentar una ejecución o validación no duplica eventos ni cierres.

Los cierres `DISCARDED`, `ACCEPTED` y `NOT_APPLICABLE` exigen motivo, requieren
permiso de cierre, eliminan la elegibilidad técnica actual y conservan los intentos
anteriores en el timeline. Nunca se presentan como resolución técnica.

## Persistencia, seguridad y concurrencia

La migración aditiva `0006_local_identity_exceptions` añade campos operativos y
`exception_attachments`. Cada adjunto se guarda con `StorageProvider.put_file`,
hash, Artifact `EXCEPTION_ATTACHMENT`, actor y ArtifactLink `EXCEPTION_EVIDENCE`.
El nombre recibido es solo una etiqueta saneada. Límite 10 MiB por adjunto y lista
de formatos permitidos; no se interpretan adjuntos como scripts o reglas. Descarga
forzada como attachment, verificación SHA-256, `nosniff` y auditoría.

Todos los endpoints validan organización, CSRF y RBAC. `/exceptions/assignees`
expone solo id/nombre/rol de candidatos válidos, sin requerir acceso a la gestión
completa de usuarios. El filtrado por estado, módulo, severidad, prioridad,
responsable, vencimiento y búsqueda se realiza en backend, con límites de página.

PATCH, comentarios, adjuntos y validación worker/manual usan el mismo compare and
swap sobre `exceptions.version`. Un conflicto humano durante validación automática
deja la modificación humana y permite reintentar; no convierte una ejecución de
calidad correcta en fallo técnico ni sobrescribe eventos.

## Verificación

Pruebas en `test_exception_operations.py`, `test_exception_validation.py`,
`test_migrations.py`, `Operations.test.tsx` y
`local-identity-exceptions.spec.ts`: alcance por organización, permisos, adjuntos,
hashes, límites, CAS, SLA, reapertura y política automática. Los resultados de la
certificación completa se registran en el informe de validación de la versión.
