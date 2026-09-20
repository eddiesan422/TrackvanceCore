# Trackvance Core 0.4.0 — entrega del roadmap local

El ciclo conserva Conexiones y completa las capacidades funcionales locales de
los puntos 3 a 9. La certificación final y su CI se registran en
[validación](validation.md). La productización continúa fuera del alcance.

## Cambios funcionales

| Bloque | Capacidad |
| --- | --- |
| Conexiones | PostgreSQL y SQL Server recertificados: exploración, preview, lectura con cuenta SELECT, snapshots Parquet, versiones, reconexión y SecretStore. |
| Data Intake | Required/Not Null, Unique, unicidad compuesta, Numeric, Positive, Type, Range, Length, Allowed Values, Regex, fechas, comparación de columnas, condiciones declarativas y referencias a DatasetVersion fijo. Selección por esquema, métricas evaluadas/fallidas/excluidas, evidencia y Excel. |
| ReconOps | Claves simples/compuestas, múltiples comparaciones exactas/numéricas/fechas, tolerancias absolutas/porcentuales, null policies, normalización, transforms ordenados, 1:N y N:1 con SUM/COUNT y evidencia por comparación. |
| Sentinel | Periodicidad local versionada, intervalos idempotentes, fechas planificadas/reales, último snapshot registrado, historial separado por método/versión, evolución con escala temporal real, alertas internas y Centro de Control. |
| Excepciones | Asignación estable, prioridad, SLA, vencimientos, comentarios, adjuntos StorageProvider, filtros, reapertura y política de resolución automática deshabilitada por defecto. RESOLVED requiere prueba posterior suficiente; cierre administrativo separado. |
| Identidad | Crear/editar/activar/desactivar usuarios, cinco roles base, permisos efectivos, reset local Argon2, revocación de sesiones, protección del último administrador y auditoría. |
| Operación | Plan y confirmación literal para reset, backup coordinado de PostgreSQL/artifacts/credenciales/claves, hashes, restore a proyecto nuevo, verificación SQL/linaje/secretos y doctor. |
| Volumen | Framework determinista con motores reales, watchdog, métricas CPU/RAM/disco/artifacts y preflight. Máximo ejecutado: 106.194.531 bytes, 50.000 filas y cuatro columnas. |

## Arquitectura y persistencia

Se mantiene el monolito modular, PostgreSQL interno para metadata y los puertos
StorageProvider, DatasetSource, ExecutionEngine y JobQueue. Los motores de calidad
trabajan con snapshots normalizados y no acceden directamente a bases externas.
No se incorpora escritura hacia las fuentes ni servicios cloud obligatorios.

| Migración aditiva | Tablas/cambios |
| --- | --- |
| `0005_external_connections` | `external_connections`, `external_connection_versions`, `dataset_source_bindings`; desarrollada antes del roadmap, incluida en esta publicación. |
| `0006_local_identity_exceptions` | Campos de administración en users, asignación/SLA/política en exceptions y tabla `exception_attachments`. |
| `0007_monitor_scheduling` | `monitor_schedules`, `monitor_schedule_versions`, `monitor_occurrences`. |

El modelo tiene 21 tablas. No se editan las migraciones previamente aplicadas.
Las versiones, runs, snapshots y cierres históricos conservan su evidencia.

## API y frontend

El [OpenAPI 0.4.0](../../backend/openapi.json) contiene 67 rutas. El
[contrato API](../../backend/API_CONTRACT.md) detalla payloads, permisos y errores.
Las adiciones principales son:

- `/connections`: pruebas, configuración versionada, schemas, objetos, preview y registro de datasets; `/datasets/{id}/refresh-source`.
- `/monitors/{id}/schedule`, `/occurrences`, `/series` y `/alerts`.
- `/users`, `/users/roles`, `/users/{id}` y `/users/{id}/reset-password`.
- `/exceptions/assignees`, filtros operativos, comentarios, adjuntos y descarga; PATCH amplía asignación, prioridad, SLA, política y reapertura.

Componentes nuevos: Conexiones y explorador de fuente, UsersPanel y
MonitorSchedulePanel. ConfigDialog/RuleBuilder añaden reglas avanzadas basadas en
esquema; Operations incorpora la gestión de casos. La tabla de resultados por
regla y los XLSX conservan métricas y razones técnicas.

## Incidencias detectadas y corregidas

- Colisión de hallazgos con varias reglas del mismo tipo/columna: identidad estable por regla, manteniendo lectura histórica.
- Filas ignoradas/condicionales contadas como evaluadas: población aplicable y métricas separadas; cero evaluadas no demuestra corrección.
- Conciliación agregada que podía elegir un valor arbitrario no agregado: validación explícita y propagación INVALID de SUM inválido.
- Reapertura que podía reutilizar prueba anterior: corte temporal de nuevas ejecuciones y CAS compartido por worker/UI.
- Edición rápida perdida al refrescar la excepción guardada: bloqueo durante mutación y actualización de revisión, con regresión.
- Colisiones de intervalos programados y configuración inválida: idempotencia, revisión inmutable y aislamiento por savepoint.
- Limpieza de runners que podía alcanzar un proyecto preexistente: propiedad acreditada únicamente después de comprobar inventario vacío.
- Primer benchmark bloqueado por proxy y prueba con timeout fuera de contrato: overrides sólo del entorno de benchmark y contrato de conexión válido.
- Backup que pasaba en Windows pero fallaba en el runner Linux por ownership del bind: transferencia tar por streaming host/contenedor, sin elevar el usuario, con directorios 0700 y archivos 0600 en POSIX.
- Tipo EXCEPTION omitido en el verificador de linaje: soporte del vínculo EXCEPTION_EVIDENCE, regresiones de destino inexistente y cruce de organización; restore con adjunto real.
- Selector E2E ambiguo al aparecer la nueva tabla de métricas: verificación del código en la tabla de hallazgos correspondiente.
- Carrera del test de gestión de excepciones: un estado confirmado por API podía adelantarse al formulario de la nueva revisión. El test espera PATCH 200, versión visible y selector habilitado antes de volver a editar, sin ampliar timeouts ni añadir retries.

## Límites y evidencia

El benchmark principal usa datos variados deterministas; el caso compresible se
conserva como antecedente. 500 MiB, 1 GiB, 2 GiB y 5 GiB no se ejecutaron: el
preflight excede el presupuesto local. No son fallos medidos ni éxitos. Los
defaults de upload, snapshot y planner no aumentan a partir de una sola carga.
Ver [política de volumen](../adr/0014-volume-benchmark-policy.md) y [resultados](volume-benchmark.md).

La restauración certificada destruye el proyecto temporal de origen antes de
recuperar el destino, verifica siete artifacts, un secreto y 124 relaciones,
reutiliza la credencial PostgreSQL restaurada y genera un nuevo run APPROVED
manteniendo el original REJECTED. Conserva también la excepción con SLA, comentario y adjunto, su validación posterior y la programación/histórico Sentinel (31 métricas). El drill usa HTTP para la UI; Playwright se
certifica en los ciclos de navegador separados.

IMPLEMENTADO: los bloques funcionales anteriores y sus adaptadores locales.
PREPARADO: puertos para fuentes, storage, ejecución, colas, secretos y entrega de
notificaciones. OBJETIVO: conectores futuros, Data Delivery/DataSink y
productización (PySpark operativo, OIDC, cloud storage, Redis/Celery productivo,
Vault, Kubernetes, Terraform, Helm y observabilidad distribuida).

No se incorpora ninguna de esas capacidades objetivo en este ciclo. El detalle
de archivos está en [archivos modificados](changed-files.md); las decisiones se
documentan en ADR 0007–0014 y la especificación editable/PDF versionada.

## Ejemplos locales y capturas

Los informes reales descargados por Playwright están en `outputs/0.4.0/intake.xlsx`,
`recon.xlsx` y `sentinel.xlsx`. La misma carpeta contiene capturas de exportación,
programación/histórico Sentinel, caso con SLA/adjunto y Centro de Control. Son
evidencia local de fixtures aislados; los archivos de runtime no se incluyen en Git.

## Resultado consolidado

533 pruebas backend/scripts, 109 pruebas frontend y 24 escenarios distintos de
Playwright aprobados; Ruff, Mypy (33 archivos), ESLint, TypeScript y build correctos.
Migraciones PostgreSQL hasta 0007, 84 comprobaciones smoke API, 62 comprobaciones
PostgreSQL/SQL Server, restart con 206 artifacts y recuperación integral: PASS.

Los [seis jobs del tag v0.4.0](https://github.com/eddiesan422/TrackvanceCore/actions/runs/35489379287) terminaron SUCCESS sobre `69d48a0f`, incluida la publicación de documentación/PDF.
La ejecución simultánea de la rama detectó la carrera del test descrita arriba;
su corrección posterior sólo sincroniza la prueba con la revisión visible y
conserva intactos el código funcional y el tag publicado. El historial de ambas
ejecuciones está en [la evidencia CI](evidence/0.4.0/ci.json); el estado de cada
commit posterior se consulta en [los workflows de la rama](https://github.com/eddiesan422/TrackvanceCore/actions/workflows/ci.yml?query=branch%3Afeat%2Flocal-prototype).

La instalación principal está en 0.4.0/0007, con sus cuatro servicios healthy,
backup previo verificado, registros anteriores preservados y restart=no.
No se sembraron datos de prueba en ella. Se entrega operativa en http://localhost:3100.

El máximo de volumen certificado es el fixture variado de 106.194.531 bytes;
500 MiB–5 GiB no se ejecutaron por preflight. No hay pruebas fallidas pendientes
del alcance local certificado. Los objetivos de productización y conectores
adicionales continúan fuera del ciclo, sin anunciar capacidades inexistentes.
