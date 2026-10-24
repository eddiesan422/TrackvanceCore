import { useEffect, useState } from 'react'
import { ErrorState, Field, Notice } from '../../components/ui'

export type IncompleteSourceBinding = {
  source_sha256: string; expected_extraction_id: string; expected_payload_hash: string; expected_marker_hash: string; pages: number[]
}
export type IncompleteSourceAcknowledgment = IncompleteSourceBinding & { all_pages_confirmed: true; all_rows_confirmed: true }
export type IncompleteInference = {
  status: 'PARTIAL' | 'HUMAN_RESOLVED'; failed_stage: 'REFINE' | 'INTERPRET'; error_code: 'DOCUMENT_TIMEOUT'
  automatic_approval_blocked: true; requires_full_source_review: boolean; confirmation_binding?: IncompleteSourceBinding
  can_confirm: boolean; resolution?: { actor_id: string; reason: string; created_at: string }
}
const stages: Record<string, string> = { REFINE: 'refinamiento de regiones', INTERPRET: 'interpretación semántica' }

export function IncompleteSourceNotice({ review, compact = false }: { review?: IncompleteInference; compact?: boolean }) {
  if (!review) return null
  if (review.status === 'HUMAN_RESOLVED') return <p role="status">Fuente completa confirmada mediante revisión manual. El resultado automático parcial original se conserva.</p>
  return <div role="status" aria-label="Extracción parcial conservada"><strong>Extracción parcial: tiempo máximo en {stages[review.failed_stage] || 'una etapa local'}.</strong>
    {!compact && <><p>El candidato y sus evidencias se conservan. La publicación está bloqueada hasta completar una revisión manual del original completo; guardar valores por sí solo no resuelve este bloqueo.</p><p>Esta resolución genera una revisión manual verificada, nunca una aprobación automática. Código: DOCUMENT_TIMEOUT.</p></>}
  </div>
}

type Props = {
  review?: IncompleteInference; extractionId: string; payloadHash: string; sourceSha256: string
  cellsReviewed: boolean; canReview: boolean; canView: boolean; busy: boolean; pendingChanges: boolean
  sourceUrl: (page: number) => string; onConfirm: (value: IncompleteSourceAcknowledgment, reason: string) => Promise<void>
}

export default function IncompleteSourceConfirmation(props: Props) {
  const { review } = props
  if (!review || review.status !== 'PARTIAL') return null
  if (!props.canReview) return <Notice>Se requiere permiso de revisión documental para confirmar la fuente completa.</Notice>
  const binding = review.confirmation_binding
  const valid = binding && binding.source_sha256 === props.sourceSha256 && binding.expected_extraction_id === props.extractionId
    && binding.expected_payload_hash === props.payloadHash && typeof binding.expected_marker_hash === 'string' && !!binding.expected_marker_hash
    && !!props.sourceSha256 && !!props.payloadHash && !!props.extractionId && Array.isArray(binding.pages)
    && binding.pages.length > 0 && binding.pages.length <= 50
    && binding.pages.every((page, index) => Number.isInteger(page) && page >= 1 && page <= 50 && (!index || page > binding.pages[index - 1]))
  if (!binding || !valid) return <Notice>La vinculación con la revisión actual no está disponible o cambió. Recarga el documento antes de confirmar la fuente completa.</Notice>
  // These opaque server fields identify UI consent. No browser source/payload/marker hash is computed.
  const bindingKey = JSON.stringify([binding.source_sha256, binding.expected_extraction_id, binding.expected_payload_hash, binding.expected_marker_hash, binding.pages, props.extractionId, props.payloadHash, props.sourceSha256, props.canView])
  return <FullSourceReview key={bindingKey} {...props} review={review} binding={binding}/>
}

function FullSourceReview({ review, binding, cellsReviewed, canView, busy, pendingChanges, sourceUrl, onConfirm }: Props & { review: IncompleteInference; binding: IncompleteSourceBinding }) {
  const [page, setPage] = useState<number | null>(null)
  const [loadedPages, setLoadedPages] = useState<number[]>([])
  const [allPages, setAllPages] = useState(false)
  const [allRows, setAllRows] = useState(false)
  const [reason, setReason] = useState('')
  const [error, setError] = useState<unknown>(null)
  const eligible = review.can_confirm === true && review.requires_full_source_review === true && cellsReviewed && !pendingChanges && canView && !busy
  const loaded = binding.pages.every(value => loadedPages.includes(value))
  useEffect(() => { if (pendingChanges) { setAllPages(false); setAllRows(false) } }, [pendingChanges])
  const submit = async () => {
    if (!eligible || !loaded || !allPages || !allRows || !reason.trim()) return
    try {
      await onConfirm({ source_sha256: binding.source_sha256, expected_extraction_id: binding.expected_extraction_id,
        expected_payload_hash: binding.expected_payload_hash, expected_marker_hash: binding.expected_marker_hash,
        pages: [...binding.pages], all_pages_confirmed: true, all_rows_confirmed: true }, reason.trim())
      setAllPages(false); setAllRows(false); setReason(''); setLoadedPages([]); setPage(null)
    } catch (error) { setError(error); setAllPages(false); setAllRows(false); setLoadedPages([]); setPage(null) }
  }
  return <section aria-label="Confirmar fuente completa de la extracción parcial">
    <h3>Revisar el original completo</h3>
    <p>Comprueba cada página, todos los valores y la totalidad de filas, incluidas las que no aparezcan en el candidato. La extracción automática original permanece inmutable.</p>
    {(!review.can_confirm || !cellsReviewed || pendingChanges) && <Notice>Guarda primero todos los valores válidos y confirmados, y resuelve las incidencias de tablas, evidencia y aritmética. Después confirma por separado la fuente completa de la nueva revisión.</Notice>}
    {!canView && <Notice>Se requiere permiso de descarga documental para comprobar todas las páginas del original.</Notice>}
    <p>Páginas consultadas: {loadedPages.length} de {binding.pages.length}.</p>
    <div>{binding.pages.map(value => <button type="button" className="button secondary small" key={value} disabled={!canView || busy} onClick={() => { setPage(value); setAllPages(false); setAllRows(false); setError(null) }}>Ver página {value} del original completo</button>)}</div>
    {page !== null && canView && <div className="document-image"><img key={page} alt={`Original completo, página ${page}`} src={sourceUrl(page)} onLoad={() => setLoadedPages(values => values.includes(page) ? values : [...values, page])} onError={() => { setLoadedPages(values => values.filter(value => value !== page)); setAllPages(false); setAllRows(false) }}/></div>}
    <label><input type="checkbox" checked={allPages} disabled={!eligible || !loaded} onChange={event => { setAllPages(event.target.checked); setAllRows(false) }}/> He revisado completas todas las páginas del original.</label>
    <label><input type="checkbox" checked={allRows} disabled={!eligible || !allPages} onChange={event => setAllRows(event.target.checked)}/> He comprobado la totalidad de filas y tablas contra el original, incluidas posibles omisiones.</label>
    <Field label="Motivo de la confirmación de fuente completa"><textarea value={reason} maxLength={2000} disabled={!eligible} onChange={event => setReason(event.target.value)}/></Field>
    <button type="button" className="button primary" disabled={!eligible || !loaded || !allPages || !allRows || !reason.trim()} onClick={() => void submit()}>Confirmar fuente completa mediante revisión manual</button>
    {!!error && <ErrorState error={error}/>}
  </section>
}
