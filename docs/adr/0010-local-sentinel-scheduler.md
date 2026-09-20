# ADR 0010 — Programación local de Sentinel

Estado: implementado en 0.4.0; la certificación se registra en `docs/development/validation.md`.

## Decisión

El worker local consulta `monitor_schedules` antes de procesar la cola existente. No se incorpora un servicio externo ni un motor de ejecución diferente. La programación está asociada a una Configuration inmutable de Sentinel. Publicar otra versión del monitor no cambia una programación existente: el usuario debe pausar la anterior y configurar la nueva.

`monitor_schedule_versions` conserva las revisiones inmutables de periodicidad, habilitación, inicio y autor. `monitor_schedules` mantiene únicamente la revisión actual y el cursor de despacho. `monitor_occurrences` registra revisión, Configuration, DatasetVersion, hora prevista, hora de despacho y Run. El inicio real, finalización, resultado, métricas y artifacts provienen del Run enlazado.

## Semántica

- Intervalos de 1 minuto a 31 días, definidos en segundos; UI en minutos y hora local convertida a UTC. La API exige zona horaria explícita si se proporciona fecha.
- Cada edición inicia una nueva periodicidad y conserva la revisión anterior. Deshabilitar no cancela trabajos ya encolados.
- Entrada: última DatasetVersion registrada del dataset al despachar. Se evalúa su snapshot canónico. El scheduler no se conecta a la fuente externa ni dispone de credenciales. Refrescar una fuente es una acción separada de adquisición.
- Atrasos: `COALESCE_LATEST` agrupa los intervalos omitidos y ejecuta el último vencido; registra cuántos se agruparon. Tras reiniciar Docker no se genera una ráfaga histórica.
- Solapamiento: `SKIP_WHILE_ACTIVE` registra un intervalo omitido si existe un run del monitor en cola o ejecución. Sin DatasetVersion registra `NO_DATASET_VERSION`.
- Cursor y revisión se reclaman con compare-and-swap. Cursor, ocurrencia, Run, Job y auditoría se confirman en la misma transacción. Restricción única `(schedule_id, planned_at)`. Un rollback permite reintentar sin duplicar trabajos.
- El actor es `SYSTEM / trackvance:local-scheduler`. La metadata de programación se incorpora a `execution_plan.schedule` y al manifiesto del Run. Ningún resultado histórico se reescribe.
- Los permisos backend son `configurations:write` y, al habilitar, `runs:execute`; aislamiento por organización y CSRF siguen en el middleware común.

## Histórico y alertas

`GET /monitors/{id}/series` reutiliza `metric_history` y la cadena explícita de versiones del monitor. Separa series por clave, dimensiones, método y versión de definición. No mezcla métodos incompatibles. Devuelve como máximo 2000 muestras recientes, 500 por defecto. Cada muestra referencia su Configuration, DatasetVersion y Run.

Las alertas internas son Findings persistidos del monitor; sus enlaces se muestran en Sentinel y en el Centro de Control existente. `NotificationDelivery` define únicamente el contrato futuro para entregas externas. Email, Teams, Slack y Webhook no están implementados ni configurados en este ciclo.

## Límites y recuperación

El scheduler depende del worker local y su heartbeat. Mientras Docker esté detenido no hay ejecución. Un trabajo largo puede retrasar el siguiente tick; el retraso queda visible comparando hora prevista e inicio real. Las garantías son transaccionales para el despacho y las leases existentes gobiernan la ejecución. No se afirma disponibilidad distribuida ni procesamiento en tiempo real.

Pruebas: idempotencia, rollback/reinicio, cursor obsoleto, pausas, revisiones, atrasos, no solapamiento, ausencia de snapshot, actor/manifiesto, RBAC/CSRF/organización, métricas separadas, componentes y E2E con worker real.
