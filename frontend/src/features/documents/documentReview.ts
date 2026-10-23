import type { RecordData } from '../../api/client'

export type ReviewCell = { path: string; name: string; value: RecordData }

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
    EXTRACTION_PENDING: 'La extracción sigue pendiente',
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
