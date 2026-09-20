# Validación del frontend 0.4.0

Fecha de verificación: 19 de septiembre de 2026, America/Bogota.
Proyecto: `frontend/`. Entorno: Windows, Node.js y pnpm locales; navegador contra
proyectos Docker aislados.

## Resultado actual de 0.4.0

| Comprobación | Resultado |
| --- | --- |
| `pnpm lint` | Correcto. |
| `pnpm typecheck` | Correcto. |
| `pnpm test` | **109 pruebas correctas en 12 archivos**. |
| `pnpm build` | Correcto. El bundle JavaScript principal mide 535,26 kB (`535.26 kB` en la salida de Vite); la advertencia de tamaño no es bloqueante. |
| Playwright `connections-final` | **23 escenarios correctos y 1 omitido**. El omitido es el escenario limpio opt-in, excluido deliberadamente de ese runner. |
| Playwright limpio separado | **1 escenario correcto** de acceso demo limpio. |

Son **24 escenarios Playwright distintos aprobados** entre las dos ejecuciones. El
escenario opt-in no se suma dos veces: aparece omitido en `connections-final` y se
ejecuta una vez en su ciclo limpio separado. Las pruebas de componentes simulan la
API; la suite Playwright usa el stack local y valida por separado el navegador.

## Cobertura actual de componentes

| Archivo | Alcance comprobado |
| --- | --- |
| `src/api/client.test.ts` | Sesión, CSRF, descargas seguras y propagación de errores. |
| `src/features/connections/Connections.test.tsx` | Prueba previa al guardado, edición optimista, cambio de endpoint, permisos, baja lógica, exploración, registro y refresh. |
| `src/features/datasets/Datasets.test.tsx` | Formatos, inspección, esquema, identificadores, versiones y ordenamiento. |
| `src/features/datasets/VersionIdentity.test.tsx` | Procedencia, snapshots, artifacts, permisos y compatibilidad histórica. |
| `src/features/identity/UsersPanel.test.tsx` | Alta y edición local, roles, activación, reset de contraseña, permisos y errores. |
| `src/features/runs/AdvancedRules.test.tsx` | Condiciones, claves compuestas, referencias inmutables, tipo/longitud, comparación de columnas, transforms y null policies. |
| `src/features/runs/ConfigDialog.test.tsx` | Esquema detectado, reglas Intake, Recon y Sentinel, publicación y errores del servicio. |
| `src/features/runs/MonitorSchedulePanel.test.tsx` | Carga, cadencia, pausa, revisión optimista, permisos, ocurrencias, alertas y series. |
| `src/features/runs/RuleBuilder.test.tsx` | Reglas declarativas, normalización y comparaciones. |
| `src/features/runs/Runs.test.tsx` | Estados, descargas, permisos, métricas, códigos y numeración de resultados. |
| `src/routes/Dashboard.test.tsx` | Filtros y presentación operativa del Centro de Control. |
| `src/routes/Operations.test.tsx` | Revisión optimista, asignación/SLA, adjuntos, permisos, reapertura y cierre técnicamente validado. |

Los E2E recorren los cinco formatos, formularios de reglas, Conexiones con motores
reales, ciclo avanzado de fuentes, scheduler Sentinel, usuarios/excepciones,
validación técnica de casos, navegación, permisos y limpieza opt-in. Esta lista se
limita a interacciones presentes en los tests; no presupone controles visuales para
opciones que siguen disponibles únicamente por API.

## Historial conservado de 0.3.0

El corte base del 16 de septiembre registró lint, typecheck y build correctos,
**67 pruebas Vitest en 8 archivos** y **13 escenarios Playwright** en 40,4 s. La
evolución Conexiones del 19 de septiembre amplió esa evidencia a **89 pruebas
Vitest en 9 archivos**, **15 escenarios Playwright** y un acceso limpio opt-in
aprobado por separado. Esos números son historia de 0.3.0 y no constituyen el total
actual de 0.4.0.

En aquel corte, `multiformat-navigation.spec.ts` añadió carga real de CSV, XLSX con
hoja, JSON, Parquet y TXT delimitado, inspección de columnas y navegación.
`rule-builders.spec.ts` publicó Intake, Recon y Sentinel desde formularios reales.
`exception-validation.spec.ts` verificó el bloqueo hasta una ejecución posterior
conforme, la resolución técnica y el cierre administrativo separado.

### Comportamiento validado en el corte 0.3.0

- **Reglas declarativas:** fecha con `not_future` y límites ISO; rango; valores permitidos que conservan espacios, mayúsculas y Unicode; regex portable; severidad y política de nulos. Sentinel incorpora controles de distintos, unicidad, tipo de esquema y banda histórica.
- **Conciliación:** normalización explícita de claves y de igualdad exacta; varias comparaciones exactas, numéricas y de fecha; tolerancia porcentual con denominador declarado; igualdad de nulos opcional; agregación 1:N por suma o conteo. La normalización de controles nuevos comienza en `NONE`.
- **Publicación:** los formularios envían `schema_version: 2`. Una edición publica una nueva versión sin cambiar el nombre ni los datasets del control existente. La adaptación de configuraciones históricas conserva `TRIM` de forma explícita. Los errores de validación del servicio permanecen visibles y permiten corregir antes de reintentar.
- **Biblioteca:** `EXACT_COMPARE` designa igualdad exacta; `NUMERIC_TOLERANCE` designa la comparación numérica. `EXACT_MATCH` se puede buscar como alias histórico de tolerancia numérica y permanece visible en la evidencia histórica. La biblioteca presenta los nombres actuales sin reescribir configuraciones almacenadas.
- **Valores y procedencia:** la muestra distingue `null`, texto vacío, espacios, Unicode y ceros iniciales. La carga envía el archivo recibido junto con las etiquetas explícitas `IDENTIFIER`. Una versión de salida de Intake presenta artefacto derivado, Parquet canónico, versión de entrada y ejecución de origen; no se etiqueta como una nueva carga original.
- **Perfil histórico:** solamente `profiling_policy: OBSERVED_EXACT_V2` muestra el aviso de perfil de valores observados. Los perfiles anteriores conservan su aviso histórico, que explica la posible normalización de espacios y texto vacío. No se recalculan ni se presentan como perfiles v2.
- **Evidencia y descarga:** la interfaz solicita Excel, conserva el código de motivo junto a su nombre legible y separa el estado técnico `SUCCESS` de la decisión de calidad. Durante una descarga se evitan solicitudes duplicadas. Se comprueban por separado los permisos de exportación y descarga de artefactos; ejecuciones pendientes, fallidas o canceladas no habilitan informes. Se muestran errores de servicio y referencias de solicitud.

### Numeración de resultados en 0.3.0

Los números recibidos se presentan sin sumar, restar o inferir desplazamientos. El encabezado depende de la declaración de cada fuente en `Run.metrics`:

| Tabla | Métrica | `RECORD_NUMBER` | `PHYSICAL_LINE` o métrica ausente |
| --- | --- | --- | --- |
| Intake | `source_row_numbering` | Registro de la versión | Línea del archivo |
| Recon, origen | `source_row_numbering` | Registro origen | Línea origen |
| Recon, destino | `target_row_numbering` | Registro destino | Línea destino |

Intake también adapta el texto introductorio de los hallazgos. Recon decide el encabezado de cada lado por separado: una versión derivada puede conciliarse con un CSV y viceversa. Las regresiones comprueban el primer registro derivado (`1`), las dos combinaciones de fuentes mixtas y la compatibilidad con ejecuciones que no contienen estas métricas. La correspondencia con líneas físicas del CSV y el conteo de registros derivados son responsabilidad del backend.

### Cobertura de componentes del corte base 0.3.0

| Archivo | Pruebas | Alcance principal |
| --- | ---: | --- |
| `src/api/client.test.ts` | 4 | CSRF, credenciales, nombres seguros de descargas y errores de red/permisos. |
| `src/features/datasets/Datasets.test.tsx` | 15 | Formatos de carga, inspección y corrección de esquema, identificadores, áreas, origen y ordenamiento. |
| `src/features/datasets/VersionIdentity.test.tsx` | 6 | Valores observados, procedencia, artefactos, permisos y perfil histórico. |
| `src/features/runs/RuleBuilder.test.tsx` | 11 | Reglas declarativas, normalización y comparaciones. |
| `src/features/runs/ConfigDialog.test.tsx` | 9 | Esquema detectado, selección múltiple y “Todos”, publicación, configuración histórica y errores. |
| `src/features/runs/Runs.test.tsx` | 13 | Descargas, estados, permisos, valores, códigos y numeración de resultados. |
| `src/routes/Dashboard.test.tsx` | 2 | Filtros globales y presentación operativa del Centro de Control. |
| `src/routes/Operations.test.tsx` | 7 | Flujo de excepción, bloqueo y validación técnica, cierre administrativo, compatibilidad histórica y taxonomía `EXACT_MATCH`. |

### Alcance del editor documentado en el corte base 0.3.0

Esta lista conserva los límites observados en aquel corte y no describe por sí sola
la interfaz 0.4.0, que añadió los componentes enumerados arriba. En 0.3.0:

- Los límites de rango que se pueden crear visualmente son inclusivos (`gte` y `lte`); los límites estrictos requieren una declaración por API.
- El formulario de valores permitidos crea valores de texto, uno por línea. Escalares de otro tipo y valores que contienen saltos de línea requieren API.
- El selector de regex expone la opción `i`; las otras opciones admitidas por el backend se configuran mediante API. La validez y portabilidad del patrón las decide el servicio al publicar.
- Las tolerancias de fecha se crean visualmente en días y UTC. La variante en horas admitida por API no dispone de selector propio.
- La banda histórica visual utiliza `row_count`, con ventana, mínimo de observaciones y multiplicador IQR. No hay editor genérico de `metric_threshold`.
- Las transformaciones de Intake, las propiedades avanzadas de una regla (`rule_id`, `scope`, `enabled`, mensaje personalizado) y las opciones no representadas por controles del formulario se administran por API. La interfaz no aplica transformaciones implícitas a los valores observados.
- El formulario ofrece tipo esperado fijo o comparación con la versión anterior. Para la segunda opción envía `schema_type` sin `expected_type`: su prueba de componente verifica ese cuerpo de solicitud; la resolución y aceptación de la línea base deben validarse con el backend y los flujos de integración.
- Los selectores de Data Intake usan el esquema detectado y permiten elegir todas las columnas elegibles. La validación definitiva de columnas, reglas y parámetros corresponde a la API.

En ese corte, la capa de acceso conservaba registros de evidencia heterogéneos
mediante `RecordData` y no generaba un cliente de tipos desde OpenAPI. Los controles
de permisos de la interfaz complementaban la autorización del backend.
