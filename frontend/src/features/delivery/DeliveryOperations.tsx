import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileCheck2, ShieldAlert } from 'lucide-react'
import { api, post } from '../../api/client'
import { usePermission } from '../../app/session'
import { date, Empty, ErrorState, Field, Loading, Modal, Notice } from '../../components/ui'
import type { DeliveryReview, DeliveryReviewOutcome, EvidenceRepair } from './types'

const outcomes: Record<DeliveryReviewOutcome, string> = {
  REMOTE_COMMIT_OBSERVED: 'Commit observado en el destino',
  REMOTE_NOT_COMMITTED_OBSERVED: 'Ausencia de commit observada en el destino',
  INCONCLUSIVE: 'Verificación no concluyente',
}

export function DeliveryOperations({ runId, attemptId, pendingRepair, unknown }: {
  runId: string; attemptId?: string; pendingRepair: boolean; unknown: boolean
}) {
  const cache = useQueryClient(), canExecute = usePermission('runs:execute')
  const [reviewOpen, setReviewOpen] = useState(false)
  const [outcome, setOutcome] = useState<DeliveryReviewOutcome>('INCONCLUSIVE')
  const [note, setNote] = useState(''), [verifiedAt, setVerifiedAt] = useState('')
  const reviews = useQuery({ queryKey: ['delivery-reviews', runId], queryFn: () => api<{ items: DeliveryReview[]; total: number }>(`/delivery/runs/${runId}/reviews`), enabled: unknown })
  async function refresh() {
    await Promise.all([
      cache.invalidateQueries({ queryKey: ['run', runId] }),
      cache.invalidateQueries({ queryKey: ['delivery-attempts', runId] }),
      cache.invalidateQueries({ queryKey: ['delivery-audit', runId] }),
      cache.invalidateQueries({ queryKey: ['delivery-reviews', runId] }),
      cache.invalidateQueries({ queryKey: ['delivery-receipt', runId] }),
    ])
  }
  const repair = useMutation({ mutationFn: () => post<EvidenceRepair>(`/delivery/runs/${runId}/repair-evidence`), onSuccess: refresh })
  const review = useMutation({
    mutationFn: () => post<DeliveryReview>(`/delivery/runs/${runId}/reviews`, {
      delivery_attempt_id: attemptId, outcome, note: note.trim(),
      ...(verifiedAt ? { verified_at: new Date(verifiedAt).toISOString() } : {}),
    }),
    onSuccess: async () => { setReviewOpen(false); setNote(''); setVerifiedAt(''); await refresh() },
  })
  return <>
    {pendingRepair && <div className="delivery-unknown delivery-operation-notice"><FileCheck2 size={22}/><div><strong>Entrega confirmada. La evidencia local está pendiente de reparación.</strong><p>La reparación reconstruye únicamente receipt y manifest desde información persistida verificable. No conecta con el destino ni vuelve a enviar datos.</p>{canExecute && <button className="button secondary small" disabled={repair.isPending} onClick={() => repair.mutate()}>{repair.isPending ? 'Reparando evidencia…' : 'Reparar evidencia'}</button>}</div></div>}
    {repair.error && <ErrorState error={repair.error}/>}
    {repair.isSuccess && <Notice success>{repair.data.status === 'ALREADY_VALID' ? 'La evidencia local ya era válida.' : 'Evidencia local reparada.'} No se repitió la entrega.</Notice>}
    {unknown && <>
      <div className="delivery-unknown delivery-operation-notice"><ShieldAlert size={22}/><div><strong>Confirmación remota desconocida</strong><p>Trackvance perdió la confirmación de la transacción. Verifica el destino antes de decidir cualquier nueva ejecución.</p><p>UNKNOWN no equivale a FAILED ni COMMITTED y no se reintentará automáticamente. La revisión sólo documenta lo observado externamente: no cambia el intento histórico, no reenvía datos y no crea una Run.</p>{canExecute && attemptId && <button className="button secondary small" onClick={() => { review.reset(); setReviewOpen(true) }}>Revisar resultado</button>}</div></div>
      <section className="panel delivery-operational-reviews"><div className="panel-heading"><div><h2>Revisiones operacionales</h2><p>Observaciones externas añadidas al historial; no son confirmaciones de commit de Trackvance.</p></div></div>
        {reviews.isPending ? <Loading text="Cargando revisiones…"/> : reviews.error ? <ErrorState error={reviews.error} retry={() => reviews.refetch()}/> : !reviews.data?.items.length ? <Empty title="Sin revisiones registradas" description="Verifica el destino y documenta el resultado antes de considerar una nueva Run deliberada."/> : <div className="table-scroll"><table><thead><tr><th>Resultado observado externamente</th><th>Verificado por</th><th>Fecha de verificación</th><th>Fecha de registro</th><th>Nota / motivo</th></tr></thead><tbody>{reviews.data.items.map(item => <tr key={item.id}><td>{outcomes[item.outcome]}<small className="table-subtitle mono">{item.outcome}</small><small className="table-subtitle mono">Intento: {item.delivery_attempt_id}</small></td><td>{item.reviewer_name}<small className="table-subtitle mono">{item.reviewer_id}</small></td><td>{date(item.verified_at)}</td><td>{date(item.created_at)}</td><td className="delivery-review-note">{item.note}</td></tr>)}</tbody></table></div>}
      </section>
    </>}
    {reviewOpen && <Modal open onOpenChange={open => { if (!review.isPending) setReviewOpen(open) }} title="Revisar resultado" description="Registra una verificación externa. El intento conserva UNKNOWN y no se ejecutará otra entrega.">
      <form className="form-stack" onSubmit={event => { event.preventDefault(); if (canExecute && attemptId && note.trim()) review.mutate() }}>
        <Field label="Resultado observado externamente"><select disabled={review.isPending} value={outcome} onChange={event => setOutcome(event.target.value as DeliveryReviewOutcome)}>{Object.entries(outcomes).map(([value, text]) => <option value={value} key={value}>{text}</option>)}</select></Field>
        <Field label="Fecha de verificación externa (opcional)" hint="Hora local. Si se omite, se registra la hora actual del servidor."><input type="datetime-local" disabled={review.isPending} value={verifiedAt} onChange={event => setVerifiedAt(event.target.value)}/></Field>
        <Field label="Nota / motivo" hint="Describe cómo verificaste el destino. No incluyas secretos ni filas de negocio."><textarea required maxLength={4000} rows={4} disabled={review.isPending} value={note} onChange={event => setNote(event.target.value)}/></Field>
        <Notice>Guardar sólo añade una revisión auditable. Para volver a entregar debes crear deliberadamente una nueva Run desde Data Delivery, después de verificar el destino.</Notice>
        {review.error && <ErrorState error={review.error}/>}
        <div className="modal-footer"><button type="button" className="button secondary" disabled={review.isPending} onClick={() => setReviewOpen(false)}>Cancelar</button><button className="button primary" disabled={!canExecute || !attemptId || !note.trim() || review.isPending}>{review.isPending ? 'Guardando revisión…' : 'Guardar revisión'}</button></div>
      </form>
    </Modal>}
  </>
}
