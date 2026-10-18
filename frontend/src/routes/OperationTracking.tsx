import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowRight, ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react'
import { api } from '../api/client'
import type { RecordData } from '../api/client'
import { Badge, date, Empty, ErrorState, Loading, Modal, Notice, number, PageHeading, SearchBox } from '../components/ui'
import { AcquisitionStatus } from '../features/datasets/Acquisitions'
import { PersonalUpdates } from '../features/notifications/Notifications'
import { useOperationRead, usePermission } from '../app/session'

export const operationQueues = [
  ['all', 'Toda la operación'], ['intervention', 'Necesita mi intervención'],
  ['running', 'En ejecución'], ['waiting', 'Esperando'], ['finished', 'Finalizados'],
] as const

export default function OperationTracking() {
  const [params, setParams] = useSearchParams()
  const canOperate = useOperationRead(), canReadCases = usePermission('exceptions:read'), canReadNews = usePermission('notifications:read')
  const view = params.get('view') === 'news' || !canOperate ? 'news' : 'operation'
  const [search, setSearch] = useState(''), [cursors, setCursors] = useState([0])
  const [module, setModule] = useState(''), [origin, setOrigin] = useState(''), [status, setStatus] = useState(''), [result, setResult] = useState(''), [fromDate, setFromDate] = useState(''), [untilDate, setUntilDate] = useState('')
  const requested = params.get('queue') || 'all'
  const queue = operationQueues.some(([value]) => value === requested) ? requested : 'all'
  const offset = cursors.at(-1) || 0, acquisition = params.get('acquisition')
  const query = new URLSearchParams({ queue, search, offset: String(offset), limit: '25', grouped: 'true' })
  for (const [key, value] of Object.entries({ module, origin, status, result, from_date: fromDate ? new Date(`${fromDate}T00:00:00`).toISOString() : '', until_date: untilDate ? new Date(`${untilDate}T23:59:59.999`).toISOString() : '', process: params.get('process') || '', dataset_id: params.get('dataset_id') || '', version_id: params.get('version_id') || '' })) if (value) query.set(key, value)
  const feed = useQuery({ queryKey: ['operations', query.toString()], queryFn: () => api<RecordData>(`/operations?${query}`), refetchInterval: 5000, enabled: canOperate && view === 'operation' })
  const rows: RecordData[] = feed.data?.items || []
  const chooseQueue = (value: string) => { setCursors([0]); const next = new URLSearchParams(params); next.set('queue', value); setParams(next) }
  const chooseView = (value: string) => { const next = new URLSearchParams(params); next.set('view', value); setParams(next) }
  return <><PageHeading eyebrow="SEGUIMIENTO OPERATIVO" title="Operación" description="Recorre adquisiciones, versiones, controles, entregas y excepciones mediante sus identidades y evidencia reales." action={view === "operation" && canOperate ? <button className="button secondary" onClick={() => feed.refetch()} disabled={feed.isFetching}><RefreshCw size={16}/>Actualizar</button> : undefined}/>
    <div className="tabs" role="group" aria-label="Contenido de operación">{canOperate && <button aria-pressed={view === 'operation'} className={view === 'operation' ? 'active' : ''} onClick={() => chooseView('operation')}>Seguimiento operativo</button>}{canReadNews && <button aria-pressed={view === 'news'} className={view === 'news' ? 'active' : ''} onClick={() => chooseView('news')}>Novedades personales</button>}</div>
    {view === 'news' ? <PersonalUpdates key={`${params.get('read_state')}:${params.get('process')}`} initialReadState={params.get('read_state') || 'ALL'} process={params.get('process') || undefined}/> : <>
    <div className="workflow-queues" role="group" aria-label="Vistas de operación">{operationQueues.map(([value, title]) => <button key={value} aria-pressed={queue === value} onClick={() => chooseQueue(value)}>{title}</button>)}</div>
    <div className="panel table-toolbar"><SearchBox value={search} onChange={value => { setSearch(value); setCursors([0]) }} placeholder="Buscar por nombre, diagnóstico o identidad de seguimiento…"/>{canReadCases && <Link className="text-link" to="/exceptions">Gestionar excepciones <ArrowRight size={14}/></Link>}</div>
    <div className="notification-filters">{[['Módulo', module, setModule, ['acquisition', 'intake', 'recon', 'sentinel', 'DELIVERY', 'documents']], ['Origen', origin, setOrigin, ['MANUAL', 'SCHEDULED', 'EVENT', 'CHAINED', 'AUTOMATIC']], ['Estado técnico', status, setStatus, ['QUEUED', 'RUNNING', 'SUCCESS', 'FAILED', 'BLOCKED', 'UNKNOWN']], ['Resultado funcional', result, setResult, ['PUBLISHED', 'NO_CHANGES', 'NOT_PUBLISHED', 'NOT_EXECUTED', 'REJECTED', 'APPROVED', 'COMMITTED', 'ALERT']]].map(([title, value, setter, values]) => <label key={title as string}>{title as string}<select value={value as string} onChange={event => { (setter as (value: string) => void)(event.target.value); setCursors([0]) }}><option value="">Todos</option>{(values as string[]).map(option => <option key={option}>{option}</option>)}</select></label>)}<label>Desde<input type="date" value={fromDate} onChange={event => { setFromDate(event.target.value); setCursors([0]) }}/></label><label>Hasta<input type="date" value={untilDate} onChange={event => { setUntilDate(event.target.value); setCursors([0]) }}/></label></div>
    <p className="muted" style={{ margin: '14px 0', fontSize: 12 }}>Las etapas provienen de registros existentes. Cada enlace conserva la identidad de origen; una excepción resuelta no confirma por sí sola una entrega.</p>
    {feed.isPending ? <Loading/> : feed.error ? <ErrorState error={feed.error} retry={() => feed.refetch()}/> : !rows.length ? <Empty title="Sin operaciones en esta vista" description={feed.data?.has_more ? 'Puedes continuar a los registros anteriores. Los filtros se aplican únicamente a recursos autorizados.' : 'Prueba otra vista o inicia una operación desde su módulo.'}/> : <div className="operation-list">{rows.map(item => <article className="panel operation-card" key={item.id}>
      <header><div><h2>{item.title}</h2><p>{date(item.created_at)} · {item.owner || 'Responsable no registrado'}{item.duration_seconds != null && <> · {number(item.duration_seconds)} s</>}{item.revision && <> · Revisión {item.revision}</>}</p></div><div className="detail-summary"><span>Estado técnico <Badge value={item.technical_state || item.state}/></span><span>Resultado funcional <Badge value={item.functional_result || 'PENDING'}/></span><span>{item.intervention_required ? 'Requiere intervención' : 'Sin intervención registrada'}</span>{item.priority && <Badge value={item.priority}/>}</div></header>
      <div className="resolution-context"><div><strong>Qué ocurrió y diagnóstico</strong><p>{item.diagnosis || 'No se ha registrado un diagnóstico adicional.'}{item.error_code && <> <code>{item.error_code}</code></>}</p></div><div><strong>Impacto</strong><p>{item.impact}</p></div></div>
      <details><summary>{item.stages.length} etapas registradas · Ver etapas, intentos y evidencias</summary><ol className="operation-stages" aria-label="Etapas registradas">{item.stages.map((stage: RecordData) => <li key={stage.id}>{stage.href ? <Link className="text-link" to={stage.href}>{stage.title}</Link> : <span>{stage.title}</span>}<small>{stage.state}{stage.decision && ` · ${stage.decision}`}{stage.attempts != null && ` · ${stage.attempts} intentos`}{stage.closure_type && ` · ${stage.closure_type === 'VERIFIED' ? 'Resolución verificada' : 'Cierre administrativo'}`}</small></li>)}</ol></details>
      {item.more_stages && <p className="muted">Se muestran las primeras 20 ejecuciones vinculadas. Consulta la versión para continuar por el historial completo.</p>}
      <div><strong>Siguiente acción permitida</strong><p>{item.next_action}</p>{item.due_at && <small className="muted">Plazo: {date(item.due_at)}</small>}</div>
      <div className="operation-actions">{item.actions.map((action: RecordData) => <Link key={action.href} to={action.href} className="text-link">{action.title}<ArrowRight size={14}/></Link>)}</div>
      <small className="mono muted">Seguimiento: {item.tracking_id}</small>
    </article>)}</div>}
    {!feed.isPending && !feed.error && <nav className="pagination" aria-label="Paginación de operación"><span role="status">{number(rows.length)} registros autorizados en esta página</span><div><button className="icon-button" aria-label="Operaciones más recientes" disabled={cursors.length === 1} onClick={() => setCursors(previous => previous.slice(0, -1))}><ChevronLeft size={18}/></button><button className="icon-button" aria-label="Operaciones anteriores" disabled={!feed.data?.has_more} onClick={() => setCursors(previous => [...previous, Number(feed.data?.next_offset)])}><ChevronRight size={18}/></button></div></nav>}
    <Notice>Corregir una fuente externa requiere una nueva adquisición. Los estados UNKNOWN de Delivery requieren revisión del destino y conservan la prohibición de repetir automáticamente un POST incierto.</Notice></>}
    <Modal open={!!acquisition} onOpenChange={open => { if (!open) { const next = new URLSearchParams(params); next.delete('acquisition'); setParams(next) } }} title="Seguimiento de adquisición" description="Identidad, progreso, diagnóstico y publicación de esta adquisición." wide>{acquisition && <AcquisitionStatus id={acquisition}/>}</Modal>
  </>
}
