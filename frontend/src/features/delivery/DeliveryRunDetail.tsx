import { useEffect, useRef } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, FileCheck2, ShieldAlert, Square, Timer } from 'lucide-react'
import { api, download, post } from '../../api/client'
import type { RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Loading, number, PageHeading } from '../../components/ui'
import { collectionItems } from './types'
import { DeliveryOperations } from './DeliveryOperations'
import './delivery.css'

// A persisted null is authoritative: do not replace it with another DTO's count.
function metricValue(attempt: RecordData, metrics: RecordData, key: string) {
  return Object.prototype.hasOwnProperty.call(attempt, key) ? attempt[key] : metrics[key]
}
function metricNumber(value: unknown) {
  return value == null || value === '' ? 'N/D' : number(value)
}

const activeRun = (status?: string) => status === 'QUEUED' || status === 'RUNNING'

export function DeliveryRunDetail({ data }: { data: RecordData }) {
  const cache = useQueryClient(), canDownload = usePermission('artifacts:download'), canExecute = usePermission('runs:execute'), canAudit = usePermission('audit:read')
  const pending = activeRun(String(data.status)), id = String(data.id)
  const attempts = useQuery({ queryKey: ['delivery-attempts', id], queryFn: () => api<unknown>(`/delivery/runs/${id}/attempts`), refetchInterval: pending ? 1500 : false })
  const rows = collectionItems<RecordData>(attempts.data), latest = rows.at(-1) || data.delivery_attempt || {}
  const committed = rows.some(item => item.status === 'COMMITTED') || latest.status === 'COMMITTED'
  const pendingRepair = data.status === 'SUCCESS' && data.decision === 'COMMITTED' && data.metrics?.evidence_status === 'PENDING_REPAIR'
  const receipt = useQuery({ queryKey: ['delivery-receipt', id], queryFn: () => api<RecordData>(`/delivery/runs/${id}/receipt`), enabled: !pending && committed && canDownload && !pendingRepair })
  const audits = useQuery({ queryKey: ['delivery-audit', id], queryFn: () => api<{ items: RecordData[]; total: number }>('/audit-events'), enabled: canAudit, refetchInterval: pending ? 2000 : false })
  const wasPending = useRef(pending)
  useEffect(() => {
    if (wasPending.current && !pending) {
      const refresh = async (queryKey: string[]) => {
        await cache.cancelQueries({ queryKey })
        await cache.invalidateQueries({ queryKey })
      }
      void refresh(['delivery-attempts', id])
      if (canAudit) void refresh(['delivery-audit', id])
    }
    wasPending.current = pending
  }, [cache, canAudit, id, pending])
  const auditRows = (audits.data?.items || []).filter(event => event.run_id === id || (event.subject_type === 'run' && event.subject_id === id))
  const cancel = useMutation({ mutationFn: () => post(`/runs/${id}/cancel`), onSuccess: () => { cache.invalidateQueries({ queryKey: ['run', id] }); cache.invalidateQueries({ queryKey: ['runs'] }) } })
  const evidence = useMutation({ mutationFn: (kind: 'manifest' | 'receipt') => download(kind === 'manifest' ? `/runs/${id}/evidence` : `/delivery/runs/${id}/receipt`, kind === 'manifest' ? `trackvance_delivery_${id}.json` : `trackvance_delivery_receipt_${id}.json`) })
  const metrics = data.metrics || {}, delivery = data.delivery || data.execution_plan || {}, target = delivery.target || data.target || {}
  const destinationVersionId = delivery.destination_version_id || data.destination_version_id
  const destinationRevision = delivery.destination_version || data.destination_version
  const destinationRevisionLabel = destinationRevision ? `v${destinationRevision} · ${destinationVersionId || '—'}` : destinationVersionId || '—'
  const receiptTarget = receipt.data?.target as RecordData | undefined
  const receiptTargetLabel = receipt.data?.target_locator || (receiptTarget ? `${receiptTarget.schema_name || ''}.${receiptTarget.table_name || ''}` : undefined)
  const unknown = data.status === 'UNKNOWN' || latest.status === 'UNKNOWN'
  const durationSeconds = data.started_at && data.finished_at ? Math.max(0, (new Date(String(data.finished_at)).getTime() - new Date(String(data.started_at)).getTime()) / 1000) : undefined
  return <><PageHeading back="/delivery" eyebrow="DATA DELIVERY / EVIDENCIA DE ENTREGA" title={data.name || 'Entrega de datos'} description={`${data.dataset_name || 'Dataset'} · ${date(data.created_at)}`} action={<><button className="button secondary" disabled={!canDownload || pending || pendingRepair || evidence.isPending} onClick={() => evidence.mutate('manifest')}><FileCheck2 size={16}/> Manifiesto</button><button className="button primary" disabled={!canDownload || !committed || pendingRepair || evidence.isPending} onClick={() => evidence.mutate('receipt')}><Download size={16}/> Receipt</button></>}/>
    <div className="detail-summary"><span>Estado técnico</span><Badge value={data.status}/><span>DeliveryAttempt</span><Badge value={latest.status || (pending ? 'STARTED' : 'NOT_REQUESTED')}/><span className="mono">{String(data.run_id || data.id)}</span><span><Timer size={14}/> {durationSeconds == null ? 'En procesamiento' : `Duración ${number(durationSeconds)} s`}</span>{data.finished_at && <span>Finalizada {date(data.finished_at)}</span>}</div>
    {pending && <section className="panel run-progress"><div><div className="live-mark"/><div><h2>{data.status === 'QUEUED' ? 'La entrega está en la lane DELIVERY' : 'Publicando la DatasetVersion'}</h2><p>{data.progress_stage || 'El delivery-worker prepara el preflight final y la transacción remota.'}</p></div><strong>{number(data.progress_percent || 0)}%</strong><button className="button secondary small" disabled={!canExecute || cancel.isPending} onClick={() => cancel.mutate()}><Square size={13}/> Cancelar</button></div><div className="progress-track"><i style={{ width: `${Math.min(100, Number(data.progress_percent || 0))}%` }}/></div></section>}
    <DeliveryOperations runId={id} attemptId={latest.status === 'UNKNOWN' ? latest.id : undefined} pendingRepair={pendingRepair} unknown={unknown}/>
    {['FAILED', 'FAILED_PRECONDITION'].includes(String(data.status)) && !unknown && <ErrorState error={new Error(typeof data.error === 'string' ? data.error : data.error?.message || latest.error_message || 'La entrega falló.')}/>} {evidence.error && <ErrorState error={evidence.error}/>} {cancel.error && <ErrorState error={cancel.error}/>}
    <div className="run-metrics delivery-run-metrics" aria-describedby="delivery-metric-help"><div><span title="Filas preparadas por Trackvance para la operación remota.">Filas preparadas</span><strong>{metricNumber(metricValue(latest, metrics, 'rows_attempted'))}</strong></div><div><span title="Filas enviadas exitosamente en una operación confirmada, según el adaptador; no es el total físico final del destino.">Filas enviadas</span><strong>{metricNumber(metricValue(latest, metrics, 'rows_written'))}</strong></div><div><span title="Conteos del adaptador sólo cuando pueden determinarse con fiabilidad; N/D significa no disponible.">Insertadas / actualizadas</span><strong>{metricNumber(metricValue(latest, metrics, 'rows_inserted'))} <small>/ {metricNumber(metricValue(latest, metrics, 'rows_updated'))}</small></strong></div><div><span title="Tamaño lógico del payload preparado por el adaptador; no equivale al tráfico de red ni al espacio físico del destino.">Bytes enviados</span><strong>{metricNumber(metricValue(latest, metrics, 'bytes_sent'))}</strong></div></div>
    <p className="delivery-metric-help" id="delivery-metric-help">Filas enviadas no significa filas físicas finales: triggers, rules o políticas del destino pueden modificar, redirigir o suprimir DML. Insertadas y actualizadas se muestran sólo cuando el adaptador puede determinarlas con fiabilidad. N/D significa dato no disponible, nunca cero.</p>
    <section className="panel delivery-run-overview"><div className="panel-heading"><div><h2>Entrega publicada</h2><p>Identidad funcional y revisión exacta usadas por el Run.</p></div><Badge value={delivery.write_strategy || data.write_strategy}/></div><div className="delivery-review-grid"><div><span>DatasetVersion</span><strong>{data.dataset_name || 'Dataset'}</strong><code>{data.dataset_version_id || delivery.dataset_version_id}</code></div><div><span>Destino / revisión</span><strong>{delivery.destination_name || data.destination_name || 'Destino'}</strong><code>{destinationRevisionLabel}</code></div><div><span>Schema / tabla</span><strong>{target.schema_name || delivery.schema_name || '—'}.{target.table_name || delivery.table_name || '—'}</strong></div><div><span>Estrategia</span><Badge value={delivery.write_strategy || data.write_strategy}/></div></div></section>
    <section className="panel delivery-attempts"><div className="panel-heading"><div><h2>DeliveryAttempts</h2><p>Cada intento remoto conserva su estado sin reinterpretar resultados inciertos.</p></div><span className="muted">{number(rows.length)} intentos</span></div>{attempts.isPending ? <Loading text="Cargando intentos…"/> : attempts.error ? <ErrorState error={attempts.error} retry={() => attempts.refetch()}/> : !rows.length ? <Empty title="Sin intentos remotos" description="El delivery-worker todavía no inició una transacción con el destino."/> : <div className="table-scroll"><table><thead><tr><th>Intento</th><th>Estado</th><th>Target</th><th>Enviadas / preparadas</th><th>Inicio</th><th>Finalización</th><th>Detalle</th></tr></thead><tbody>{rows.map(attempt => <tr key={attempt.id}><td><strong>#{attempt.attempt_number}</strong><small className="table-subtitle mono">{String(attempt.idempotency_key || '').slice(0, 18)}</small></td><td><Badge value={attempt.status}/></td><td className="mono">{attempt.target_locator || '—'}</td><td>{metricNumber(attempt.rows_written)} / {metricNumber(attempt.rows_attempted)}</td><td>{date(attempt.started_at)}</td><td>{date(attempt.finished_at)}</td><td>{attempt.error_message || attempt.remote_reference || '—'}{attempt.error_code && <small className="reason-code">{attempt.error_code}</small>}</td></tr>)}</tbody></table></div>}</section>
    {committed && <section className="panel delivery-receipt"><div className="panel-heading"><div><h2>Receipt inmutable</h2><p>Evidencia de la publicación, sin secretos ni filas completas del dataset.</p></div>{receipt.data && <Badge value="DELIVERY_RECEIPT"/>}</div>{!canDownload ? <div className="delivery-receipt-access"><ShieldAlert size={18}/><span>No tienes permiso para consultar artifacts de evidencia.</span></div> : pendingRepair ? <p className="delivery-receipt-access">El commit está confirmado. Repara la evidencia local para consultar el receipt.</p> : receipt.isPending ? <Loading text="Cargando receipt…"/> : receipt.error ? <ErrorState error={receipt.error} retry={() => receipt.refetch()}/> : receipt.data && <><div className="delivery-receipt-grid">{[['Run', receipt.data.run_id || id], ['DatasetVersion', receipt.data.dataset_version_id], ['SHA-256 origen', receipt.data.source_sha256], ['DestinationVersion', receipt.data.destination_version_id], ['Target', receiptTargetLabel], ['Estrategia', receipt.data.write_strategy], ['Resultado', receipt.data.result || receipt.data.status], ['Filas enviadas', metricNumber(receipt.data.rows_written)]].map(([title, value]) => <div key={title}><span>{title}</span><strong className={title.includes('Version') || title === 'Run' || title.includes('SHA') ? 'mono' : ''}>{String(value ?? '—')}</strong></div>)}</div><div className="panel-bottom"><FileCheck2 size={15}/><span>Manifest y receipt son evidencia separada del estado técnico del Run.</span></div></>}</section>}
    <section className="panel delivery-audit"><div className="panel-heading"><div><h2>Auditoría de la entrega</h2><p>Eventos saneados vinculados con esta Run y su intento remoto.</p></div><span className="muted">{canAudit ? `${number(auditRows.length)} eventos` : 'Acceso restringido'}</span></div>{!canAudit ? <div className="delivery-receipt-access"><ShieldAlert size={18}/><span>No tienes permiso para consultar el registro de auditoría.</span></div> : audits.isPending ? <Loading text="Cargando auditoría…"/> : audits.error ? <ErrorState error={audits.error} retry={() => audits.refetch()}/> : !auditRows.length ? <Empty title="Sin eventos asociados todavía" description="Los eventos aparecerán conforme avance la entrega."/> : <div className="table-scroll"><table><thead><tr><th>Fecha</th><th>Evento</th><th>Responsable</th><th>Detalle</th></tr></thead><tbody>{auditRows.map(event => <tr key={event.id}><td className="no-wrap">{date(event.created_at)}</td><td><strong>{event.message}</strong><small className="table-subtitle mono">{event.event_type}</small></td><td>{event.actor || 'Sistema'}</td><td className="mono">{String(event.request_id || event.subject_id || '—').slice(0, 24)}</td></tr>)}</tbody></table></div>}</section>
  </>
}
