# Validación del frontend 0.2.0

Fecha de verificación: 13 de septiembre de 2026, 21:09, America/Bogota.
Proyecto: `frontend/`. Entorno: Windows, Node.js y pnpm locales.

## Resultado

| Comprobación | Resultado |
| --- | --- |
| `pnpm lint` | Correcto, sin advertencias. |
| `pnpm typecheck` | Correcto. |
| `pnpm test` | **42 pruebas correctas en 7 archivos**. Vitest 3.2.0, entorno jsdom. |
| `pnpm build` | Correcto. Vite 7.3.6 genera `frontend/dist/`. |

El paquete identifica esta entrega como `0.2.0` y el pie de la aplicación muestra `v0.2`.
Esta verificación comprueba componentes, estados de interfaz, permisos de acciones y cuerpos de las solicitudes. Las pruebas de componentes simulan la API: no sustituyen las pruebas de los motores, la exportación Excel real ni el flujo completo con servidor y navegador. Las pruebas `frontend/tests-e2e/corrections.spec.ts` se validan por separado y no se modificaron en este cierre.

Se agregó `frontend/tests-e2e/rule-builders.spec.ts` con tres escenarios de publicación desde los formularios reales: Intake con fecha no futura, Recon con igualdad exacta y normalización declarada, y Sentinel con tipo de esquema respecto de la versión anterior. Cada escenario exige respuesta HTTP 201, comprueba la configuración persistida mediante GET y ejecuta el control para verificar su resultado. Las cargas de preparación usan la API; los contratos, controles y monitores se crean desde la interfaz. Playwright descubre los tres escenarios con `pnpm exec playwright test --list rule-builders.spec.ts`; su ejecución contra el servidor integrado queda pendiente del rebuild final. No se incluyen como pruebas aprobadas en la tabla anterior.

## Comportamiento validado

- **Reglas declarativas:** fecha con `not_future` y límites ISO; rango; valores permitidos que conservan espacios, mayúsculas y Unicode; regex portable; severidad y política de nulos. Sentinel incorpora controles de distintos, unicidad, tipo de esquema y banda histórica.
- **Conciliación:** normalización explícita de claves y de igualdad exacta; varias comparaciones exactas, numéricas y de fecha; tolerancia porcentual con denominador declarado; igualdad de nulos opcional; agregación 1:N por suma o conteo. La normalización de controles nuevos comienza en `NONE`.
- **Publicación:** los formularios envían `schema_version: 2`. Una edición publica una nueva versión sin cambiar el nombre ni los datasets del control existente. La adaptación de configuraciones históricas conserva `TRIM` de forma explícita. Los errores de validación del servicio permanecen visibles y permiten corregir antes de reintentar.
- **Biblioteca:** `EXACT_COMPARE` designa igualdad exacta; `NUMERIC_TOLERANCE` designa la comparación numérica. `EXACT_MATCH` se puede buscar como alias histórico de tolerancia numérica y permanece visible en la evidencia histórica. La biblioteca presenta los nombres actuales sin reescribir configuraciones almacenadas.
- **Valores y procedencia:** la muestra distingue `null`, texto vacío, espacios, Unicode y ceros iniciales. La carga envía el archivo recibido junto con las etiquetas explícitas `IDENTIFIER`. Una versión de salida de Intake presenta artefacto derivado, Parquet canónico, versión de entrada y ejecución de origen; no se etiqueta como una nueva carga original.
- **Perfil histórico:** solamente `profiling_policy: OBSERVED_EXACT_V2` muestra el aviso de perfil de valores observados. Los perfiles anteriores conservan su aviso histórico, que explica la posible normalización de espacios y texto vacío. No se recalculan ni se presentan como perfiles v2.
- **Evidencia y descarga:** la interfaz solicita Excel, conserva el código de motivo junto a su nombre legible y separa el estado técnico `SUCCESS` de la decisión de calidad. Durante una descarga se evitan solicitudes duplicadas. Se comprueban por separado los permisos de exportación y descarga de artefactos; ejecuciones pendientes, fallidas o canceladas no habilitan informes. Se muestran errores de servicio y referencias de solicitud.

## Numeración de resultados

Los números recibidos se presentan sin sumar, restar o inferir desplazamientos. El encabezado depende de la declaración de cada fuente en `Run.metrics`:

| Tabla | Métrica | `RECORD_NUMBER` | `PHYSICAL_LINE` o métrica ausente |
| --- | --- | --- | --- |
| Intake | `source_row_numbering` | Registro de la versión | Línea del archivo |
| Recon, origen | `source_row_numbering` | Registro origen | Línea origen |
| Recon, destino | `target_row_numbering` | Registro destino | Línea destino |

Intake también adapta el texto introductorio de los hallazgos. Recon decide el encabezado de cada lado por separado: una versión derivada puede conciliarse con un CSV y viceversa. Las regresiones comprueban el primer registro derivado (`1`), las dos combinaciones de fuentes mixtas y la compatibilidad con ejecuciones que no contienen estas métricas. La correspondencia con líneas físicas del CSV y el conteo de registros derivados son responsabilidad del backend.

## Cobertura de pruebas de componentes

| Archivo | Pruebas | Alcance principal |
| --- | ---: | --- |
| `src/api/client.test.ts` | 4 | CSRF, credenciales, nombres seguros de descargas y errores de red/permisos. |
| `src/features/datasets/Datasets.test.tsx` | 1 | Archivo recibido e identificadores explícitos en multipart. |
| `src/features/datasets/VersionIdentity.test.tsx` | 5 | Valores observados, procedencia, artefactos, permisos y perfil histórico. |
| `src/features/runs/RuleBuilder.test.tsx` | 11 | Reglas declarativas, normalización y comparaciones. |
| `src/features/runs/ConfigDialog.test.tsx` | 6 | Publicación y nueva versión, configuración histórica, errores y cuerpo de solicitud de cambio de tipo. |
| `src/features/runs/Runs.test.tsx` | 13 | Descargas, estados, permisos, valores, códigos y numeración de resultados. |
| `src/routes/Operations.test.tsx` | 2 | Taxonomía y búsqueda del alias histórico `EXACT_MATCH`. |

## Alcance del editor y opciones avanzadas de API

El formulario cubre las reglas descritas arriba; no es un editor completo de todas las declaraciones que admite la API. En particular:

- Los límites de rango que se pueden crear visualmente son inclusivos (`gte` y `lte`); los límites estrictos requieren una declaración por API.
- El formulario de valores permitidos crea valores de texto, uno por línea. Escalares de otro tipo y valores que contienen saltos de línea requieren API.
- El selector de regex expone la opción `i`; las otras opciones admitidas por el backend se configuran mediante API. La validez y portabilidad del patrón las decide el servicio al publicar.
- Las tolerancias de fecha se crean visualmente en días y UTC. La variante en horas admitida por API no dispone de selector propio.
- La banda histórica visual utiliza `row_count`, con ventana, mínimo de observaciones y multiplicador IQR. No hay editor genérico de `metric_threshold`.
- Las transformaciones de Intake, las propiedades avanzadas de una regla (`rule_id`, `scope`, `enabled`, mensaje personalizado) y las opciones no representadas por controles del formulario se administran por API. La interfaz no aplica transformaciones implícitas a los valores observados.
- El formulario ofrece tipo esperado fijo o comparación con la versión anterior. Para la segunda opción envía `schema_type` sin `expected_type`: su prueba de componente verifica ese cuerpo de solicitud; la resolución y aceptación de la línea base deben validarse con el backend y los flujos de integración.
- Los nombres de columnas se escriben de manera explícita. La lista de columnas de origen ayuda a configurar; la validación definitiva de columnas, reglas y parámetros corresponde a la API.

La capa de acceso conserva registros de evidencia heterogéneos mediante `RecordData`; todavía no se genera un cliente de tipos a partir de OpenAPI. Los controles de permisos de la interfaz mejoran el flujo de uso y se complementan con la autorización del backend.
