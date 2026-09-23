import { useEffect, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, Route, Routes, useNavigate } from 'react-router-dom'
import { ArrowRight, ArrowUpRight, Database, Download, FileCheck2, Play, Plus, Send, Server, ShieldAlert, Square, Timer } from 'lucide-react'
import { api, download, post } from '../../api/client'
import type { RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Loading, number, PageHeading, SearchBox } from '../../components/ui'
import { DestinationDetail, DestinationsPage } from './Destinations'
import { DeliveryBuilder } from './DeliveryBuilder'
import { DeliveryNavigation } from './DeliveryNavigation'
import type { DeliveryConfiguration, DeliveryDestination } from './types'
import { collectionItems } from './types'
import './delivery.css'

const activeRun = (status?: string) => status === 'QUEUED' || status === 'RUNNING'

function configValue(configuration: DeliveryConfiguration, key: string) {
  return configuration.config?.[key]
}

export function DeliveriesPage() {
  const canConfigure = usePermission('configurations:write'), canExecute = usePermission('runs:execute')
  const [search, setSearch] = useState('')
  const navigate = useNavigate(), cache = useQueryClient(), keys = useRef(new Map<string, string>())
  const configurations = useQuery({ queryKey: ['delivery-configurations'], queryFn: () => api<{ items: DeliveryConfiguration[]; total: number }>('/delivery/configurations'), refetchInterval: 15000 })
  const destinations = useQuery({ queryKey: ['delivery-destinations'], queryFn: () => api<{ items: DeliveryDestination[]; total: number }>('/delivery/destinations') })
  const run = useMutation({
    mutationFn: async (configuration: DeliveryConfiguration) => {
      let key = keys.current.get(configuration.id)
      if (!key) { key = crypto.randomUUID(); keys.current.set(configuration.id, key) }
      return api<{ id: string }>('/delivery/runs', { method: 'POST', headers: { 'Idempotency-Key': key }, body: JSON.stringify({ configuration_id: configuration.id, dataset_version_id: configuration.dataset_version_id || configValue(configuration, 'dataset_version_id') }) })
    },
    onSuccess: (data, configuration) => { keys.current.delete(configuration.id); cache.invalidateQueries({ queryKey: ['runs'] }); cache.invalidateQueries({ queryKey: ['dashboard'] }); navigate(`/runs/${data.id}`) },
  })
  const destinationNames = new Map((destinations.data?.items || []).map(item => [item.id, item.name]))
  const superseded = new Set((configurations.data?.items || []).map(item => String(item.previous_version_id || '')).filter(Boolean))
  const items = (configurations.data?.items || []).filter(item => {
    const destinationId = String(item.destination_id || configValue(item, 'destination_id') || '')
    return `${item.name} ${item.dataset_name || ''} ${item.destination_name || destinationNames.get(destinationId) || ''} ${configValue(item, 'write_strategy') || ''}`.toLocaleLowerCase().includes(search.toLocaleLowerCase())
  })
  return <><DeliveryNavigation/><PageHeading eyebrow="04 / PUBLICACIÓN CONTROLADA" title="Data Delivery" description="Entrega versiones inmutables de tus datasets hacia PostgreSQL y SQL Server con preflight, evidencia y trazabilidad." action={<><Link className="button secondary" to="/delivery/destinations"><Server size={16}/> Administrar destinos</Link><Link className={`button primary ${canConfigure ? '' : 'disabled-link'}`} aria-disabled={!canConfigure} to={canConfigure ? '/delivery/new' : '#'}><Plus size={16}/> Nueva entrega</Link></>}/>
    <div className="module-intro delivery"><Send size={28}/><div><strong>De una DatasetVersion a un destino externo, sin transformar datos.</strong><p>Cada configuración fija artifact, destino, target, mapping técnico y estrategia. La escritura ocurre en un delivery-worker separado.</p></div><span className="version-pill">{number(configurations.data?.total || 0)} entregas</span></div>
    <section className="panel"><div className="tabs module-tabs"><button className="active">Configuraciones publicadas<span>{number(configurations.data?.total || 0)}</span></button></div><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar entrega, dataset o destino…"/><span className="muted">Snapshots inmutables</span></div>
      {configurations.isPending ? <Loading text="Cargando entregas…"/> : configurations.error ? <ErrorState error={configurations.error} retry={() => configurations.refetch()}/> : !items.length ? <Empty title={search ? 'Sin coincidencias' : 'Prepara tu primera entrega'} description={search ? 'Prueba otro nombre, dataset o destino.' : 'Selecciona una DatasetVersion, valida el target y publica una configuración antes de ejecutar.'} action={<Link className="button primary" to={search ? '/delivery' : '/delivery/new'}>{search ? 'Limpiar búsqueda' : 'Nueva entrega'}</Link>}/> : <div className="config-list delivery-config-list">{items.map(configuration => {
        const target = (configuration.config?.target || {}) as RecordData, strategy = String(configValue(configuration, 'write_strategy') || 'APPEND')
        const destinationId = String(configuration.destination_id || configValue(configuration, 'destination_id') || '')
        const destinationName = configuration.destination_name || destinationNames.get(destinationId) || (destinationId ? `Destino ${destinationId.slice(0, 8)}` : 'Destino')
        const latest = configuration.latest_run as RecordData | undefined
        return <article className="config-card" key={configuration.id}><div className="config-icon delivery"><Send size={23}/></div><div className="config-info"><div className="config-title"><h3>{configuration.name}</h3><span className="version-pill">v{configuration.version}</span><Badge value={configuration.status || 'PUBLISHED'}/></div><p>{configuration.description || 'Publicación versionada de una DatasetVersion.'}</p><div className="config-meta"><span><Database size={12}/> {configuration.dataset_name || 'Dataset'} · v{configuration.dataset_version || String(configuration.dataset_version_id || configValue(configuration, 'dataset_version_id') || '').slice(0, 8)}</span><ArrowRight size={12}/><span><Server size={12}/> {destinationName}</span><span>·</span><span className="mono">{String(target.schema_name || '')}.{String(target.table_name || '')}</span></div><div className="delivery-config-badges"><Badge value={target.mode}/><Badge value={strategy}/><span>{number(Array.isArray(configuration.config?.columns) ? configuration.config.columns.length : 0)} columnas</span></div></div><div className="config-actions">{superseded.has(configuration.id) ? <span className="muted">Versión supersedida</span> : <Link className="text-button" to={`/delivery/new?configuration=${configuration.id}`}>Nueva versión</Link>}{latest ? <Link to={`/runs/${latest.id}`} className="last-run"><small>Última entrega</small><Badge value={latest.delivery_status || latest.status}/></Link> : <span className="muted">Sin ejecuciones</span>}<button className="button secondary small" disabled={!canExecute || run.isPending} onClick={() => run.mutate(configuration)}><Play size={14}/> Ejecutar</button></div></article>
      })}</div>}
      {run.error && <ErrorState error={run.error}/>}
    </section>
  </>
}

export function DeliveryRunDetail({ data }: { data: RecordData }) {
  const cache = useQueryClient(), canDownload = usePermission('artifacts:download'), canExecute = usePermission('runs:execute'), canAudit = usePermission('audit:read')
  const pending = activeRun(String(data.status)), id = String(data.id)
  const attempts = useQuery({ queryKey: ['delivery-attempts', id], queryFn: () => api<unknown>(`/delivery/runs/${id}/attempts`), refetchInterval: pending ? 1500 : false })
  const rows = collectionItems<RecordData>(attempts.data), latest = rows.at(-1) || data.delivery_attempt || {}
  const committed = rows.some(item => item.status === 'COMMITTED') || latest.status === 'COMMITTED'
  const receipt = useQuery({ queryKey: ['delivery-receipt', id], queryFn: () => api<RecordData>(`/delivery/runs/${id}/receipt`), enabled: !pending && committed && canDownload })
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
  const unknown = latest.status === 'UNKNOWN'
  const durationSeconds = data.started_at && data.finished_at ? Math.max(0, (new Date(String(data.finished_at)).getTime() - new Date(String(data.started_at)).getTime()) / 1000) : undefined
  return <><PageHeading back="/delivery" eyebrow="DATA DELIVERY / EVIDENCIA DE ENTREGA" title={data.name || 'Entrega de datos'} description={`${data.dataset_name || 'Dataset'} · ${date(data.created_at)}`} action={<><button className="button secondary" disabled={!canDownload || evidence.isPending} onClick={() => evidence.mutate('manifest')}><FileCheck2 size={16}/> Manifiesto</button><button className="button primary" disabled={!canDownload || !committed || evidence.isPending} onClick={() => evidence.mutate('receipt')}><Download size={16}/> Receipt</button></>}/>
    <div className="detail-summary"><span>Estado técnico</span><Badge value={data.status}/><span>DeliveryAttempt</span><Badge value={latest.status || (pending ? 'STARTED' : 'NOT_REQUESTED')}/><span className="mono">{String(data.run_id || data.id)}</span><span><Timer size={14}/> {durationSeconds == null ? 'En procesamiento' : `Duración ${number(durationSeconds)} s`}</span>{data.finished_at && <span>Finalizada {date(data.finished_at)}</span>}</div>
    {pending && <section className="panel run-progress"><div><div className="live-mark"/><div><h2>{data.status === 'QUEUED' ? 'La entrega está en la lane DELIVERY' : 'Publicando la DatasetVersion'}</h2><p>{data.progress_stage || 'El delivery-worker prepara el preflight final y la transacción remota.'}</p></div><strong>{number(data.progress_percent || 0)}%</strong><button className="button secondary small" disabled={!canExecute || cancel.isPending} onClick={() => cancel.mutate()}><Square size={13}/> Cancelar</button></div><div className="progress-track"><i style={{ width: `${Math.min(100, Number(data.progress_percent || 0))}%` }}/></div></section>}
    {unknown && <div className="delivery-unknown"><ShieldAlert size={22}/><div><strong>Confirmación remota desconocida</strong><p>Trackvance no puede demostrar si el destino confirmó la transacción. UNKNOWN no equivale a FAILED ni COMMITTED y no se reintentará automáticamente.</p></div></div>}
    {['FAILED', 'FAILED_PRECONDITION'].includes(String(data.status)) && !unknown && <ErrorState error={new Error(typeof data.error === 'string' ? data.error : data.error?.message || latest.error_message || 'La entrega falló.')}/>} {evidence.error && <ErrorState error={evidence.error}/>} {cancel.error && <ErrorState error={cancel.error}/>}
    <div className="run-metrics delivery-run-metrics"><div><span>Filas procesadas</span><strong>{number(latest.rows_attempted ?? metrics.rows_attempted ?? metrics.total_rows)}</strong></div><div><span>Filas escritas</span><strong>{number(latest.rows_written ?? metrics.rows_written)}</strong></div><div><span>Insertadas / actualizadas</span><strong>{number(latest.rows_inserted ?? metrics.rows_inserted)} <small>/ {number(latest.rows_updated ?? metrics.rows_updated)}</small></strong></div><div><span>Bytes enviados</span><strong>{number(latest.bytes_sent ?? metrics.bytes_sent)}</strong></div></div>
    <section className="panel delivery-run-overview"><div className="panel-heading"><div><h2>Entrega publicada</h2><p>Identidad funcional y revisión exacta usadas por el Run.</p></div><Badge value={delivery.write_strategy || data.write_strategy}/></div><div className="delivery-review-grid"><div><span>DatasetVersion</span><strong>{data.dataset_name || 'Dataset'}</strong><code>{data.dataset_version_id || delivery.dataset_version_id}</code></div><div><span>Destino / revisión</span><strong>{delivery.destination_name || data.destination_name || 'Destino'}</strong><code>{destinationRevisionLabel}</code></div><div><span>Schema / tabla</span><strong>{target.schema_name || delivery.schema_name || '—'}.{target.table_name || delivery.table_name || '—'}</strong></div><div><span>Estrategia</span><Badge value={delivery.write_strategy || data.write_strategy}/></div></div></section>
    <section className="panel delivery-attempts"><div className="panel-heading"><div><h2>DeliveryAttempts</h2><p>Cada intento remoto conserva su estado sin reinterpretar resultados inciertos.</p></div><span className="muted">{number(rows.length)} intentos</span></div>{attempts.isPending ? <Loading text="Cargando intentos…"/> : attempts.error ? <ErrorState error={attempts.error} retry={() => attempts.refetch()}/> : !rows.length ? <Empty title="Sin intentos remotos" description="El delivery-worker todavía no inició una transacción con el destino."/> : <div className="table-scroll"><table><thead><tr><th>Intento</th><th>Estado</th><th>Target</th><th>Filas</th><th>Inicio</th><th>Finalización</th><th>Detalle</th></tr></thead><tbody>{rows.map(attempt => <tr key={attempt.id}><td><strong>#{attempt.attempt_number}</strong><small className="table-subtitle mono">{String(attempt.idempotency_key || '').slice(0, 18)}</small></td><td><Badge value={attempt.status}/></td><td className="mono">{attempt.target_locator || '—'}</td><td>{number(attempt.rows_written)} / {number(attempt.rows_attempted)}</td><td>{date(attempt.started_at)}</td><td>{date(attempt.finished_at)}</td><td>{attempt.error_message || attempt.remote_reference || '—'}{attempt.error_code && <small className="reason-code">{attempt.error_code}</small>}</td></tr>)}</tbody></table></div>}</section>
    {committed && <section className="panel delivery-receipt"><div className="panel-heading"><div><h2>Receipt inmutable</h2><p>Evidencia de la publicación, sin secretos ni filas completas del dataset.</p></div>{receipt.data && <Badge value="DELIVERY_RECEIPT"/>}</div>{!canDownload ? <div className="delivery-receipt-access"><ShieldAlert size={18}/><span>No tienes permiso para consultar artifacts de evidencia.</span></div> : receipt.isPending ? <Loading text="Cargando receipt…"/> : receipt.error ? <ErrorState error={receipt.error} retry={() => receipt.refetch()}/> : receipt.data && <><div className="delivery-receipt-grid">{[['Run', receipt.data.run_id || id], ['DatasetVersion', receipt.data.dataset_version_id], ['SHA-256 origen', receipt.data.source_sha256], ['DestinationVersion', receipt.data.destination_version_id], ['Target', receiptTargetLabel], ['Estrategia', receipt.data.write_strategy], ['Resultado', receipt.data.result || receipt.data.status], ['Filas', receipt.data.rows_written]].map(([title, value]) => <div key={title}><span>{title}</span><strong className={title.includes('Version') || title === 'Run' || title.includes('SHA') ? 'mono' : ''}>{String(value ?? '—')}</strong></div>)}</div><div className="panel-bottom"><FileCheck2 size={15}/><span>Manifest y receipt son evidencia separada del estado técnico del Run.</span></div></>}</section>}
    <section className="panel delivery-audit"><div className="panel-heading"><div><h2>Auditoría de la entrega</h2><p>Eventos saneados vinculados con esta Run y su intento remoto.</p></div><span className="muted">{canAudit ? `${number(auditRows.length)} eventos` : 'Acceso restringido'}</span></div>{!canAudit ? <div className="delivery-receipt-access"><ShieldAlert size={18}/><span>No tienes permiso para consultar el registro de auditoría.</span></div> : audits.isPending ? <Loading text="Cargando auditoría…"/> : audits.error ? <ErrorState error={audits.error} retry={() => audits.refetch()}/> : !auditRows.length ? <Empty title="Sin eventos asociados todavía" description="Los eventos aparecerán conforme avance la entrega."/> : <div className="table-scroll"><table><thead><tr><th>Fecha</th><th>Evento</th><th>Responsable</th><th>Detalle</th></tr></thead><tbody>{auditRows.map(event => <tr key={event.id}><td className="no-wrap">{date(event.created_at)}</td><td><strong>{event.message}</strong><small className="table-subtitle mono">{event.event_type}</small></td><td>{event.actor || 'Sistema'}</td><td className="mono">{String(event.request_id || event.subject_id || '—').slice(0, 24)}</td></tr>)}</tbody></table></div>}</section>
  </>
}

export function DeliveryRoutes() {
  return <Routes><Route index element={<DeliveriesPage/>}/><Route path="new" element={<DeliveryBuilder/>}/><Route path="destinations" element={<DestinationsPage/>}/><Route path="destinations/:id" element={<DestinationDetail/>}/><Route path="*" element={<div className="empty-state"><h1>Sección de Data Delivery no encontrada</h1><Link className="button primary" to="/delivery">Volver a Entregas <ArrowUpRight size={15}/></Link></div>}/></Routes>
}

export { DeliveryNavigation }
