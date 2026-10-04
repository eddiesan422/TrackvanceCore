# ADR 0024: Automatización, outbox y bandeja personal

- Estado: aceptada
- Fecha: 2026-10-03

## Contexto

Programar o encadenar Delivery no debe perder eventos tras un reinicio, repetir efectos remotos, atribuir permisos al proceso SYSTEM ni publicar resultados ajenos en una bandeja. El polling del scheduler tampoco debe ejecutar conectores o bloquear los workers de otras lanes.

## Decisión

Un scheduler separado despacha ocurrencias con cursor transaccional, clave única y revisión inmutable. ONCE, INTERVAL, DAILY y WEEKLY usan fechas UTC y calendario IANA; una hora inexistente se omite y una hora ambigua usa su primera aparición. Si el proceso estuvo detenido se coalescen los slots vencidos en una ocurrencia con su cantidad registrada. CHAINED consume exclusivamente eventos nuevos de un Intake concreto, después de activarse la revisión.

La revisión fija configuración, destino y responsable. Cada despacho y cada inicio de Run revalida la cuenta activa, organización, permisos vigentes y disponibilidad del destino. Las programaciones heredadas sin actor verificable requieren asignación explícita. SYSTEM identifica el origen de una ejecución automática; su responsable real suministra la autorización.

El resultado encadenado debe ser SUCCESS con APPROVED, o APPROVED_WITH_WARNINGS cuando se ha optado expresamente. Se utiliza la DatasetVersion de salida exacta, con perfil publicado, evidencia y artifact verificable. REJECTED, salida vacía por defecto, bytes corruptos, permisos revocados o destino deshabilitado generan una ocurrencia bloqueada sin una escritura remota. Una reclamación por automatización y versión impide repetir entradas; la repetición manual es explícita y auditable.

El cambio terminal de Run o adquisición crea outbox en la misma transacción de metadata. Cada evento tiene una deduplicación persistente y dos consumos independientes, NOTIFICATIONS y CHAINING. Los consumidores reclaman mediante compare-and-swap, lease con propietario y límite de cinco intentos. Su heartbeat corre independientemente de la verificación de archivos. La publicación de sus efectos y la confirmación DONE se realizan en una transacción; una lease vencida pierde la autorización para confirmar. Un fallo de consumidor no reescribe la ejecución original.

Los flushes intermedios de esa transacción completan un único evento con el estado,
decisión, responsable y versión publicada finales. Después del commit su payload
queda congelado; una modificación posterior no reescribe el hecho histórico. Los
consumidores nunca observan una salida todavía incompleta de otra transacción.

Todas las entregas manuales y automáticas comparten una guarda por destino físico, incluyendo host, puerto, base, esquema y tabla. PostgreSQL usa advisory lock transaccional y bloqueo de la fila. UNKNOWN mantiene el destino bloqueado, incluyendo intentos históricos. Una revisión concluyente y una decisión posterior explícita liberan esa guarda; los Runs, intentos y efectos históricos permanecen sin modificación. La liberación no reejecuta automáticamente el Run desconocido.

La bandeja guarda sólo el destinatario verificable de la ejecución. Sus consultas, contadores y marcas de lectura filtran usuario, organización y permisos actuales sobre el recurso. Un administrador no obtiene las notificaciones de otros usuarios. El texto distingue éxito técnico y decisión funcional: Intake rechazado, Recon con hallazgos, alertas Sentinel, Delivery confirmado, evidencia pendiente y confirmación UNKNOWN. Los preflights mantienen semántica de validación y enlace a su detalle personal, aunque se agrupen en Delivery.

Las listas paginadas permiten filtrar módulo, origen, estado técnico y lectura
ALL/READ/UNREAD. `read_at` persiste el estado personal; el parámetro legado
`unread=true` conserva su comportamiento cuando no se indica `read_state`.

## Consecuencias y verificación

La corrección C05 del 4 de octubre (ADR 0025) hace efectivo el despacho sólo
metadata: no abre descriptores/partes ni calcula hashes en scheduler, CHAINING,
manual o enqueue transitivo. Worker verifica bytes e identidades congeladas antes
de STARTED fuera de locks prolongados. Corrupción después de encolar produce
FAILED_PRECONDITION, sin escritura ni cambio de versión. C04 valida timezone
draft y conserva starts_at. C06 añade setter idempotente read_at=NULL con la
misma autorización personal y permisos actuales, sin generar nuevos eventos.

Los registros terminales históricos no se recorren ni generan notificaciones retroactivas. La metadata permite recuperar consumidores y demostrar idempotencia sin deducir efectos a partir de un mensaje de transporte. Las pruebas ejercitan rollback de outbox, consumidores independientes, recuperación y agotamiento de lease, salida Intake exacta, no repetición, revocación, aislamiento entre usuarios y organizaciones, DST, integridad de artifacts, serialización y revisión de UNKNOWN. El runner `scripts/tests/automation_cycle.py` exige un proyecto y una base PostgreSQL desechables identificados explícitamente antes de certificar scheduler, SQL real y bandeja mediante API.
