# ADR 0005: validación técnica para resolver excepciones

- Estado: aceptado
- Fecha: 2026-09-16

## Contexto

Una excepción nace de un hallazgo producido por una ejecución de Data Intake,
ReconOps o Sentinel. La ejecución y el hallazgo de origen son evidencia
histórica: corregir los datos o cambiar la gestión del caso no puede modificar
su resultado.

El flujo anterior permitía seleccionar `RESOLVED` y documentar una causa y una
resolución sin comprobar si el mismo control había vuelto a ejecutarse ni si el
problema había desaparecido. Así, el estado mezclaba una decisión humana con
una verificación técnica.

## Decisión

La excepción conserva tanto `origin_run_id` como el identificador y la versión
de la configuración que originó el hallazgo. La referencia es al snapshot
inmutable de la configuración, no solo al nombre visible del control.

El flujo operativo es:

```text
OPEN -> INVESTIGATING -> PENDING_VALIDATION -> RESOLVED
```

`RESOLVED` solo se permite cuando una ejecución cumple todas estas condiciones:

1. terminó con estado técnico `SUCCESS`;
2. es posterior a la ejecución de origen;
3. usa exactamente el mismo `configuration_id`;
4. demuestra, según el módulo, que el problema ya no está presente.

La verificación por módulo es determinística:

- **Data Intake:** la regla y columna identificadas por el fingerprint del
  hallazgo ya no fallan en la ejecución posterior;
- **ReconOps:** el resultado queda conforme o ya no aparece la clasificación
  asociada al hallazgo;
- **Sentinel:** el monitor termina con decisión `HEALTHY`.

La evaluación, automática al terminar un run o solicitada mediante la API,
registra `validation_run_id`, `validated_at` y evidencia estructurada. La
respuesta expone `technical_validation` con el estado, la
elegibilidad, el motivo de bloqueo y las referencias a la ejecución candidata y
a la que confirmó la corrección. El botón de resolución permanece inactivo
mientras `technical_validation.can_resolve` sea falso.

El usuario también puede cerrar un caso mediante una decisión administrativa:
`DISCARDED`, `ACCEPTED` o `NOT_APPLICABLE`. Estas decisiones exigen
`administrative_reason`, quedan auditadas y no establecen validación técnica ni
se presentan como resolución.

Los estados históricos `WAITING_EXTERNAL` y `FALSE_POSITIVE` continúan siendo
legibles. No se reescriben excepciones, ejecuciones ni eventos anteriores.

## Consecuencias

- La causa raíz y la explicación de la corrección siguen siendo evidencia
  humana, pero no sustituyen la comprobación técnica.
- Una nueva versión publicada de un control es una configuración distinta y no
  valida automáticamente excepciones creadas por la versión anterior.
- La relación entre ejecución de origen y ejecución de validación puede
  consultarse y auditarse sin alterar ninguno de los dos runs.
- La evaluación automática está separada de la transición final. Por ahora una
  persona confirma `RESOLVED`; esta separación permite incorporar más adelante
  resolución automática conservando la misma política y evidencia.
