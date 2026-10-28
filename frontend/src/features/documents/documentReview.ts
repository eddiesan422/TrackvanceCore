import type { RecordData } from '../../api/client'

export type ReviewCell = { path: string; name: string; value: RecordData }

export const extractionStrategies: Record<string, string> = {
  LEGACY: 'Compatibilidad histórica',
  NATIVE_FIRST: 'Lectura nativa y Qwen visual cuando sea necesario',
  QWEN_VISUAL: 'Qwen visual directo',
  PADDLE_STRUCTURED: 'Paddle estructurado',
  SELECTIVE: 'Combinación selectiva con controles',
}

export function originalPages(item: RecordData): number[] {
  const pages = item.execution?.expected_pages?.length ? item.execution.expected_pages : item.extraction?.payload?.pages
  return Array.isArray(pages) && pages.length ? [...new Set<number>(pages.filter((page: unknown) => typeof page === 'number' && Number.isInteger(page) && page > 0))].sort((a, b) => a - b) : [1]
}

export function sourceGeometryKind(value: RecordData): string {
  const kind = String(value.geometry_kind || value.origin?.geometry_kind || (value.origin ? 'MEASURED' : 'UNKNOWN')).toUpperCase()
  if (['MEASURED', 'MEASURED_SOURCE'].includes(kind)) return 'MEASURED'
  if (['QUERY_REGION', 'TABLE_REGION', 'MEASURED_BLOCK'].includes(kind)) return 'QUERY_REGION'
  return 'UNKNOWN'
}

export function sourceSelection(value: RecordData, name: string): RecordData {
  const origin = value.origin || value.observed_origin
  if (!origin) return { name, geometry_kind: 'UNKNOWN' }
  const kind = sourceGeometryKind({ ...value, origin })
  return { ...origin, name, geometry_kind: kind,
    region: kind === 'MEASURED' && Array.isArray(origin.region) && origin.region.length === 4 ? origin.region : undefined }
}

export function sourceDescription(selection: RecordData): string {
  if (!selection.page) return `${selection.name}: sin referencia demostrable`
  if (selection.geometry_kind === 'QUERY_REGION') return `${selection.name}: página ${selection.page}. Región consultada; la ubicación exacta del valor no está medida.`
  if (selection.geometry_kind === 'UNKNOWN') return `${selection.name}: página ${selection.page}. Ubicación exacta no demostrada.`
  return `${selection.name}: página ${selection.page}${selection.region ? `, región medida ${selection.region.map((number: number) => number.toFixed(3)).join(', ')}` : ', sin región medida'}${selection.method ? ` · ${selection.method}` : ''}`
}

export function publicationBlockReason(item: RecordData): string | null {
  if (item.is_example) return 'Las referencias de plantilla no se publican como documentos operativos.'
  if (item.duplicate_of_id) return 'El archivo es un duplicado; consulta el documento original.'
  if (item.incomplete_inference?.status === 'PARTIAL' || item.execution?.recognition_complete === false) return 'El reconocimiento está incompleto. Resuelve la ejecución y las páginas pendientes antes de publicar.'
  if (item.execution?.resources_released === false) return 'La ejecución todavía no ha liberado sus recursos. Espera su cierre antes de publicar.'
  if (!item.extraction) return 'Todavía no hay una extracción para publicar.'
  if (item.issues?.length) return `Hay ${item.issues.length} controles pendientes. Resuelve las excepciones antes de publicar.`
  if (!['REVIEWED', 'AUTO_VERIFIED', 'PUBLISHED'].includes(item.status)) return 'La revisión o la validación todavía no habilita la publicación.'
  if (item.publications?.some((publication: RecordData) => publication.extraction_id === item.extraction.id)) return 'Esta revisión ya tiene una publicación. Una corrección necesita una revisión nueva.'
  return null
}

export function extractionCells(payload?: RecordData): ReviewCell[] {
  if (!payload) return []
  return [
    ...Object.entries(payload.fields || {}).map(([name, value]) => ({ path: `headers.0.${name}`, name, value: value as RecordData })),
    ...Object.entries(payload.tables || {}).flatMap(([table, rows]) => (rows as RecordData[]).flatMap((row, index) => Object.entries(row).map(([name, value]) => ({ path: `${table}.${index}.${name}`, name: `${table} · fila ${index + 1} · ${name}`, value: value as RecordData })))),
  ]
}

// Only the server validator decides which valid candidates need human review.
// A missing issue list is treated conservatively for historical API payloads.
export function needsReview(path: string, value: RecordData, issues?: string[]): boolean {
  if (value.recognition_status === 'NOT_APPLICABLE') return false
  if (value.validity !== 'VALID') return true
  return issues ? issues.some(issue => issue === `REVIEW_REQUIRED:${path}` || issue === `VALUE_INVALID:${path}`) : !value.reviewed
}

export function cellSummary(cells: ReviewCell[], issues?: string[]) {
  const result = { valid: 0, ambiguous: 0, missing: 0, invalid: 0, pending: 0 }
  for (const { path, value } of cells) {
    if (value.recognition_status === 'NOT_APPLICABLE') continue
    if (value.validity === 'AMBIGUOUS') result.ambiguous++
    else if (value.validity === 'MISSING') result.missing++
    else if (value.validity !== 'VALID') result.invalid++
    else if (needsReview(path, value, issues)) result.pending++
    else result.valid++
  }
  return result
}

export function issueDescription(issue: string): string {
  const [code, ...target] = issue.split(':')
  const description: Record<string, string> = {
    REVIEW_REQUIRED: 'Comprueba y confirma este valor contra el original', VALUE_INVALID: 'Completa o corrige este valor',
    TABLE_REQUIRED: 'No se encontró una tabla obligatoria', TABLE_ROWS_UNRECOGNIZED: 'Hay filas o regiones sin interpretar',
    QUALITY_ROW_COVERAGE: 'La cantidad de filas no está demostrada', QUALITY_DUPLICATE_KEY: 'Hay claves duplicadas en el detalle',
    QUALITY_INDEPENDENT_SOURCE_REQUIRED: 'El valor necesita corroboración de un mecanismo distinto',
    QUALITY_OBSERVATION_UNCERTAINTY: 'La fuente conserva incertidumbre o alternativas que requieren resolución',
    EXTRACTION_PENDING: 'La extracción sigue pendiente',
    QUALITY_INCOMPLETE_SOURCE: 'Faltan páginas o etapas de reconocimiento por resolver',
    QUALITY_CRITICAL_CORROBORATION: 'Un dato crítico necesita evidencia independiente de la fuente',
    QUALITY_TABLE_STRUCTURE: 'La estructura de la tabla necesita revisión',
    NORMALIZATION_INVALID: 'La normalización declarada no pudo aplicarse de forma segura',
  }
  const message = description[code] || (code.startsWith('ARITHMETIC_') ? 'Revisa la coherencia de los importes' : 'El control independiente requiere atención')
  return `${message}${target.length ? `: ${target.join(' · ')}` : ''}`
}

export function engineErrorDescription(code: string): string {
  const descriptions: Record<string, string> = {
    DOCUMENT_LOCAL_INFERENCE_UNAVAILABLE: 'El servicio local de IA no responde. Comprueba su disponibilidad y reintenta cuando se recupere.',
    DOCUMENT_LOCAL_INFERENCE_UNCONFIGURED: 'El motor local necesita sus servicios y modelos configurados antes de procesar.',
    DOCUMENT_TIMEOUT: 'El procesamiento superó el tiempo permitido. Revisa la carga y los límites antes de reintentar.',
    DOCUMENT_EXTERNAL_PROVIDER_DISABLED: 'La ejecución real de Textract está bloqueada en esta versión.',
    DOCUMENT_SANDBOX_UNAVAILABLE: 'El aislamiento de documentos no está disponible. El administrador debe restablecerlo antes de reintentar.',
    DOCUMENT_LLM_EVIDENCE_INVALID: 'La interpretación semántica no pudo justificarse con el original. El resultado necesita revisión.',
    DOCUMENT_ARTIFACT_INTEGRITY: 'La integridad del original no coincide con su huella guardada. Revisa el almacenamiento antes de reintentar.',
    DOCUMENT_LOCAL_INFERENCE_CONTRACT: 'La respuesta del motor no cumple el contrato de extracción. Revisa el diagnóstico del servicio local.',
  }
  return descriptions[code] || 'No se pudo completar la extracción. Revisa el diagnóstico, la plantilla y los límites antes de reintentar.'
}
