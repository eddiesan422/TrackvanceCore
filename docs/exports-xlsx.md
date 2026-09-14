# Informes Excel de Trackvance Core

Los tres informes son workbooks `.xlsx` construidos con **openpyxl** desde
la configuración y los resultados efectivos de una ejecución. Un Excel es
un artifact de evidencia; el almacenamiento canónico de datasets y resultados
sigue siendo Parquet.

## Diseño compartido

Encabezados navy, acentos teal, rojo para incumplimientos y amber para
advertencias. Todas las hojas tienen encabezados, autofiltros y paneles
inmovilizados. Las tablas de detalle empiezan en la fila 5 y sus encabezados
quedan fijos; en Recon también se fijan clave/clasificación. Hay anchos,
ajuste de texto, formato de impresión, porcentajes, decimales y fechas UTC.
Los labels de negocio conviven con los códigos técnicos.

Estado técnico `SUCCESS` se muestra como Completada y la decisión se muestra
por separado: Rechazado, Con hallazgos, Alerta, etc. Las tasas se guardan como
números con formato porcentual. Los identificadores se conservan como texto.
Si un decimal excede 15 cifras significativas se mantiene texto para evitar
la pérdida de precisión de Excel.

## Intake

1. **Resumen**: contrato/version, dataset/version, run, inicio/fin, estado
   técnico/decisión, filas totales/válidas/error/advertencia, aceptación,
   umbral, motor/version y tabla de regla/columna/estado/evaluadas/fallidas.
2. **Errores**: línea del archivo, regla, columna, valor recibido, severidad,
   mensaje y clasificación. Para una entrada interna sin CSV original se usa
   Registro de la versión. Una fila puede incumplir varias reglas.
3. **Reglas**: configuración efectiva completa del contrato, con parámetros
   y transformaciones declaradas.
4. **Trazabilidad**: IDs, hashes, inputs, actor, configuración, plan/motor y
   metadatos de artifacts resultantes.

## ReconOps

1. **Resumen**: control/version, ambos datasets/versiones, claves,
   comparaciones, tolerancias, normalización declarada, conteos de las siete
   clasificaciones, match rate y metadatos de ejecución.
2. **Resultados**: clave, clasificación, valor origen/destino, diferencia,
   tolerancia, línea/registro de cada lado, mensaje y detalle de todas las
   comparaciones. Una fila por resultado; color según clasificación.
3. **Hallazgos**: sólo resultados distintos de MATCH, ordenados por
   clasificación y clave. Conserva el mismo detalle verificable.
4. **Trazabilidad**: ambos inputs, configuración/normalización, actor,
   versiones, hashes, plan y artifacts.

## Sentinel

1. **Resumen**: monitor/version, dataset/version, filas, checks/fallos,
   health score, HEALTHY/ALERT y estado técnico/run/motor.
2. **Controles**: control, code, estado, observado, esperado y detalle.
3. **Hallazgos**: sólo checks fallidos.
4. **Trazabilidad**: identidad del input, hashes, configuración, actor,
   motor y artifacts. Las métricas conservan método/version en evidencia.

## Seguridad, autorización y compatibilidad

`GET /api/v1/runs/{id}/export.xlsx` comprueba sesión, permiso de exportación,
organización, estado SUCCESS e integridad SHA-256 de resultados/manifiesto.
Entrega `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`,
`Content-Disposition: attachment` con basename sanitizado
`trackvance_<modulo>_<run>.xlsx` y `X-Artifact-ID`. Registra el artifact
`EXPORT_XLSX`, su linaje `EXPORT_OF` y auditoría `EXPORT_DOWNLOADED`.

Todos los valores de negocio se serializan como strings OOXML cuando son
texto. Un valor que empieza por `=`, `+`, `-`, `@`, tabulación o espacios no
se convierte en fórmula ni link externo. Las fórmulas de formato condicional
son constantes internas. Metadata con claves de passwords/tokens/secrets
se excluye. Controles XML inválidos se escapan visiblemente.

No se trunca silenciosamente evidencia: valores mayores que 32767 caracteres
o tablas fuera del límite de Excel producen 422 `EXPORT_LIMIT_EXCEEDED`.
Corrupción de archivos produce 409 `ARTIFACT_INTEGRITY_ERROR`; no cambia el
run histórico. La API CSV permanece **deprecated** para consumidores antiguos
del prototipo; conserva autorización, protección y auditoría. La UI ofrece
únicamente Exportar Excel. Las pruebas CSV de compatibilidad se mantienen por
esta razón junto a las nuevas pruebas XLSX.

## Generar los tres ejemplos

Con Compose preparado y el demo habilitado:

```powershell
backend/.venv/Scripts/python.exe scripts/generate_example_reports.py --base-url http://localhost:3100
```

Crea datos sintéticos nuevos mediante la API, ejecuta el ciclo certificado
120/110/90, comprueba métricas y descarga tres reportes en
`outputs/corrections/`, con IDs reales del entorno. No borra ni sobreescribe
datasets, runs o configuraciones existentes.
