import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { Bell, Check } from 'lucide-react'
import { api, post } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Notice, PageHeading, Pagination } from '../../components/ui'
import type { RecordData } from '../../api/client'
import './notifications.css'

type Notification = { id: string; module: string; origin: string; status: string; decision: string | null; description: string; created_at: string; read_at: string | null; detail_url: string; error?: { code: string; message: string; details: Record<string, unknown> | null; reference: string | null } | null }
type NotificationGroup = { process_id: string; operation: RecordData | null; items: Notification[]; unread_count: number; more_notifications?: boolean; notification_count?: number }
const moduleNames: Record<string, string> = { acquisition: 'Adquisición', intake: 'Data Intake', recon: 'ReconOps', sentinel: 'Sentinel', DELIVERY: 'Data Delivery' }

export function NotificationIndicator() {
  const allowed = usePermission('notifications:read')
  const count = useQuery({ queryKey: ['notification-count'], queryFn: () => api<{ unread_count: number }>('/notifications/unread-count'), enabled: allowed, refetchInterval: 15000 })
  if (!allowed) return null
  if (count.error) return <Link to="/operations?view=news&read_state=UNREAD" className="icon-button notification" aria-label="Ver notificaciones, contador no disponible" title="No se pudo actualizar el contador"><Bell size={19}/><span aria-hidden="true">?</span></Link>
  const unread = count.data?.unread_count || 0
  return <Link to="/operations?view=news&read_state=UNREAD" className="icon-button notification" aria-label={`Ver notificaciones${unread ? `, ${unread} sin leer` : ''}`} title="Novedades de Operación"><Bell size={19}/>{unread > 0 && <span className="tiny-label">{unread > 99 ? '99+' : unread}</span>}</Link>
}

export function PersonalUpdates({ initialReadState = 'ALL', process }: { initialReadState?: string; process?: string }) {
  const allowed = usePermission('notifications:read'), cache = useQueryClient(), navigate = useNavigate()
  const [offset, setOffset] = useState(0), [readState, setReadState] = useState(initialReadState), [module, setModule] = useState(''), [origin, setOrigin] = useState(''), [status, setStatus] = useState('')
  const [selectedProcess, setSelectedProcess] = useState(process), [notificationOffset, setNotificationOffset] = useState(0)
  const [decision, setDecision] = useState(''), [fromDate, setFromDate] = useState(''), [untilDate, setUntilDate] = useState('')
  const dates = `${fromDate ? `&from_date=${encodeURIComponent(new Date(`${fromDate}T00:00:00`).toISOString())}` : ''}${untilDate ? `&until_date=${encodeURIComponent(new Date(`${untilDate}T23:59:59.999`).toISOString())}` : ''}`
  const query = useQuery({ queryKey: ['notification-inbox', offset, readState, module, origin, status, decision, dates, selectedProcess, notificationOffset], enabled: allowed,
    queryFn: () => api<{ items: Notification[]; groups?: NotificationGroup[]; total: number }>(`/notifications/inbox?offset=${offset}&limit=25&read_state=${readState}&module=${module}&origin=${origin}&status=${status}&decision=${decision}&grouped=true${dates}${selectedProcess ? `&process=${encodeURIComponent(selectedProcess)}` : ''}&notification_offset=${notificationOffset}`), refetchInterval: 15000 })
  const refresh = () => Promise.all([cache.invalidateQueries({ queryKey: ['notification-inbox'] }), cache.invalidateQueries({ queryKey: ['notification-count'] })])
  const lock = useRef(false)
  const readStateMutation = useMutation({
    mutationFn: ({ id, read }: { id: string; read: boolean }) => post<Notification>(`/notifications/inbox/${id}/${read ? 'read' : 'unread'}`),
    onMutate: () => Promise.all([cache.cancelQueries({ queryKey: ['notification-inbox'] }), cache.cancelQueries({ queryKey: ['notification-count'] })]),
    onSuccess: async (confirmed, variables) => {
      await cache.cancelQueries({ queryKey: ['notification-inbox'] })
      if (confirmed?.id === variables.id && (confirmed.read_at === null || typeof confirmed.read_at === 'string')) {
        for (const [key, previous] of cache.getQueriesData<{ items: Notification[]; groups?: NotificationGroup[]; total: number }>({ queryKey: ['notification-inbox'] })) {
          if (!previous || !(previous.items.some(item => item.id === confirmed.id) || previous.groups?.some(group => group.items.some(item => item.id === confirmed.id)))) continue
          const filter = key[2]
          const included = filter === 'ALL' || (filter === 'READ' ? !!confirmed.read_at : !confirmed.read_at)
          const items = included ? previous.items.map(item => item.id === confirmed.id ? confirmed : item) : previous.items.filter(item => item.id !== confirmed.id)
          const groups = previous.groups?.map(group => ({ ...group, items: included ? group.items.map(item => item.id === confirmed.id ? confirmed : item) : group.items.filter(item => item.id !== confirmed.id), unread_count: group.unread_count + (group.items.some(item => item.id === confirmed.id && !item.read_at) ? -1 : 0) + (group.items.some(item => item.id === confirmed.id) && !confirmed.read_at ? 1 : 0) })).filter(group => group.items.length)
          cache.setQueryData(key, { ...previous, items, groups, total: groups ? previous.total - ((previous.groups?.length || 0) - groups.length) : Math.max(0, previous.total - (included ? 0 : 1)) })
        }
      }
      void refresh()
    },
  })
  const all = useMutation({ mutationFn: (ids: string[]) => post('/notifications/inbox/read-all', { ids }),
    onMutate: () => Promise.all([cache.cancelQueries({ queryKey: ['notification-inbox'] }), cache.cancelQueries({ queryKey: ['notification-count'] })]),
    onSuccess: refresh })
  const changing = readStateMutation.isPending || all.isPending
  function setRead(id: string, read: boolean, detailUrl?: string) {
    if (lock.current) return
    lock.current = true
    readStateMutation.mutate({ id, read }, { onSuccess: () => { if (detailUrl) navigate(detailUrl) }, onSettled: () => { lock.current = false } })
  }
  function markAll() {
    if (lock.current) return
    lock.current = true
    const ids = (query.data?.groups?.flatMap(group => group.items) || query.data?.items || []).map(item => item.id)
    all.mutate(ids, { onSettled: () => { lock.current = false } })
  }
  if (!allowed) return <Notice>Tu rol no permite consultar las notificaciones.</Notice>
  const groups = query.data?.groups
  const displayGroups: NotificationGroup[] = groups || [{ process_id: 'legacy', operation: null, items: query.data?.items || [], unread_count: 0 }]
  const hasItems = displayGroups.some(group => group.items.length)
  return <><PageHeading title="Novedades personales" description="Novedades accesibles de cada proceso. El contador de la campana suma eventos personales sin leer, aunque compartan un proceso." action={<button className="button secondary" title="Sólo las novedades mostradas en esta página y con estos filtros; excluye llegadas posteriores" disabled={changing || query.isPending || !hasItems} onClick={markAll}><Check size={16}/> Marcar esta página como leída</button>}/>
    <section className="panel notifications-panel"><p className="muted">Leída cambia únicamente tu lectura personal. No resuelve excepciones, aprueba datos ni confirma entregas. La acción de página congela las identidades mostradas; una novedad posterior vuelve a indicar sin leer.</p><div className="notification-filters"><Field label="Módulo"><select value={module} onChange={event => { setModule(event.target.value); setOffset(0) }}><option value="">Todos</option>{Object.entries(moduleNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></Field><Field label="Origen"><select value={origin} onChange={event => { setOrigin(event.target.value); setOffset(0) }}><option value="">Todos</option><option value="MANUAL">Manual</option><option value="SCHEDULED">Programado</option><option value="CHAINED">Encadenado</option><option value="AUTOMATIC">Automático</option><option value="EVENT">Publicación</option></select></Field><Field label="Lectura"><select value={readState} onChange={event => { setReadState(event.target.value); setOffset(0) }}><option value="ALL">Todas</option><option value="READ">Leídas</option><option value="UNREAD">Sin leer</option></select></Field>
      <Field label="Estado técnico"><select value={status} onChange={event => { setStatus(event.target.value); setOffset(0) }}><option value="">Todos</option>{[['SUCCESS', 'Completada'], ['FAILED', 'Fallida'], ['FAILED_PRECONDITION', 'Precondiciones incumplidas'], ['PARTIAL', 'Entrega parcial'], ['UNKNOWN', 'Confirmación desconocida'], ['CANCELLED', 'Cancelada'], ['BLOCKED', 'Bloqueada'], ['SKIPPED', 'Omitida']].map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></Field><Field label="Resultado funcional"><select value={decision} onChange={event => { setDecision(event.target.value); setOffset(0) }}><option value="">Todos</option>{['APPROVED', 'APPROVED_WITH_WARNINGS', 'REJECTED', 'COMMITTED', 'ALERT', 'HEALTHY', 'WITH_FINDINGS'].map(value => <option key={value}>{value}</option>)}</select></Field><Field label="Desde"><input type="date" value={fromDate} onChange={event => { setFromDate(event.target.value); setOffset(0) }}/></Field><Field label="Hasta"><input type="date" value={untilDate} onChange={event => { setUntilDate(event.target.value); setOffset(0) }}/></Field></div>
      {query.isPending ? <Loading/> : query.error && (!query.data || all.isSuccess) ? <ErrorState error={query.error} retry={() => query.refetch()}/> : !hasItems ? <Empty title="Sin notificaciones" description="Los próximos resultados aparecerán aquí. Ajusta los filtros para consultar otras novedades."/> : displayGroups.map(group => <div key={group.process_id}>{groups && <div className="panel-heading"><div><h2>{group.operation?.title || group.items[0]?.description}</h2><p>{group.unread_count} novedades sin leer · {group.items.length} novedades en estos filtros</p>{group.operation?.impact && <p>{group.operation.impact}</p>}</div></div>}<details open={!groups || !!selectedProcess}><summary>{groups ? 'Ver etapas, novedades y enlaces originales' : 'Novedades y lectura personal'}</summary>{group.operation && <><ol className="operation-stages">{group.operation.stages?.map((stage: RecordData) => <li key={stage.id}>{stage.href ? <Link to={stage.href}>{stage.title}</Link> : stage.title}<small>{stage.state}</small></li>)}</ol><p>{group.operation.next_action}</p></>}<div className="table-scroll"><table><thead><tr><th>Fecha y origen</th><th>Resultado</th><th>Estado técnico</th><th>Decisión de negocio</th><th>Detalle</th><th>Leída</th></tr></thead><tbody>{group.items.map(item => <tr key={item.id} className={!item.read_at ? 'notification-unread' : ''}><td>{date(item.created_at)}<small className="reason-code">{moduleNames[item.module] || item.module} · {{ MANUAL: 'Manual', SCHEDULED: 'Programado', CHAINED: 'Encadenado', AUTOMATIC: 'Automático' }[item.origin] || item.origin}</small></td><td>{!item.read_at && <strong>Sin leer · </strong>}{item.description}{item.error && <small className="reason-code">{item.error.code}{item.error.reference && <> · Referencia: {item.error.reference}</>}</small>}</td><td><Badge value={item.status}/></td><td>{item.decision ? <Badge value={item.decision}/> : '—'}</td><td><Link to={item.detail_url} aria-disabled={changing} onClick={event => { if (changing || lock.current) { event.preventDefault(); return } if (!item.read_at) { const modified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey; if (!modified) event.preventDefault(); setRead(item.id, true, modified ? undefined : item.detail_url) } }}>Ver detalle</Link></td><td><input type="checkbox" aria-label={`Leída: ${item.description}`} checked={!!item.read_at} disabled={changing} onChange={event => setRead(item.id, event.target.checked)}/></td></tr>)}</tbody></table></div>{group.more_notifications && <button className="button secondary" onClick={() => { setSelectedProcess(group.process_id); setNotificationOffset(notificationOffset + 25); setOffset(0) }}>Novedades anteriores de este proceso</button>}</details></div>)}
      {selectedProcess && <nav aria-label="Novedades del proceso">{notificationOffset > 0 && <button className="button secondary" onClick={() => setNotificationOffset(Math.max(0, notificationOffset - 25))}>Novedades más recientes del proceso</button>}<button className="button secondary" onClick={() => { setSelectedProcess(undefined); setNotificationOffset(0); setOffset(0) }}>Todos los procesos</button></nav>}
      {query.error && query.data && <ErrorState error={query.error} retry={() => query.refetch()}/>}<Pagination offset={offset} total={query.data?.total || 0} limit={25} onChange={setOffset}/>{(readStateMutation.error || all.error) && <ErrorState error={readStateMutation.error || all.error}/>}
    </section></>
}

// Kept as an import compatibility alias; the historical route redirects to
// Operación and no independent Notifications screen is mounted.
export const NotificationsPage = PersonalUpdates
