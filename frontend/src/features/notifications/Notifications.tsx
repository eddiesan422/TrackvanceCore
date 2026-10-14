import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate } from 'react-router-dom'
import { Bell, Check } from 'lucide-react'
import { api, post } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Notice, PageHeading, Pagination } from '../../components/ui'
import './notifications.css'

type Notification = { id: string; module: string; origin: string; status: string; decision: string | null; description: string; created_at: string; read_at: string | null; detail_url: string; error?: { code: string; message: string; details: Record<string, unknown> | null; reference: string | null } | null }
const moduleNames: Record<string, string> = { acquisition: 'Adquisición', intake: 'Data Intake', recon: 'ReconOps', sentinel: 'Sentinel', DELIVERY: 'Data Delivery' }

export function NotificationIndicator() {
  const allowed = usePermission('notifications:read')
  const count = useQuery({ queryKey: ['notification-count'], queryFn: () => api<{ unread_count: number }>('/notifications/unread-count'), enabled: allowed, refetchInterval: 15000 })
  if (!allowed) return null
  if (count.error) return <Link to="/notifications" className="icon-button notification" aria-label="Ver notificaciones, contador no disponible" title="No se pudo actualizar el contador"><Bell size={19}/><span aria-hidden="true">?</span></Link>
  const unread = count.data?.unread_count || 0
  return <Link to="/notifications" className="icon-button notification" aria-label={`Ver notificaciones${unread ? `, ${unread} sin leer` : ''}`} title="Notificaciones"><Bell size={19}/>{unread > 0 && <span className="tiny-label">{unread > 99 ? '99+' : unread}</span>}</Link>
}

export function NotificationsPage() {
  const allowed = usePermission('notifications:read'), cache = useQueryClient(), navigate = useNavigate()
  const [offset, setOffset] = useState(0), [readState, setReadState] = useState('ALL'), [module, setModule] = useState(''), [origin, setOrigin] = useState(''), [status, setStatus] = useState('')
  const query = useQuery({ queryKey: ['notification-inbox', offset, readState, module, origin, status], enabled: allowed,
    queryFn: () => api<{ items: Notification[]; total: number }>(`/notifications/inbox?offset=${offset}&limit=25&read_state=${readState}&module=${module}&origin=${origin}&status=${status}`), refetchInterval: 15000 })
  const refresh = () => Promise.all([cache.invalidateQueries({ queryKey: ['notification-inbox'] }), cache.invalidateQueries({ queryKey: ['notification-count'] })])
  const lock = useRef(false)
  const readStateMutation = useMutation({
    mutationFn: ({ id, read }: { id: string; read: boolean }) => post<Notification>(`/notifications/inbox/${id}/${read ? 'read' : 'unread'}`),
    onMutate: () => Promise.all([cache.cancelQueries({ queryKey: ['notification-inbox'] }), cache.cancelQueries({ queryKey: ['notification-count'] })]),
    onSuccess: async (confirmed, variables) => {
      await cache.cancelQueries({ queryKey: ['notification-inbox'] })
      if (confirmed?.id === variables.id && (confirmed.read_at === null || typeof confirmed.read_at === 'string')) {
        for (const [key, previous] of cache.getQueriesData<{ items: Notification[]; total: number }>({ queryKey: ['notification-inbox'] })) {
          if (!previous?.items.some(item => item.id === confirmed.id)) continue
          const filter = key[2]
          const included = filter === 'ALL' || (filter === 'READ' ? !!confirmed.read_at : !confirmed.read_at)
          const items = included ? previous.items.map(item => item.id === confirmed.id ? confirmed : item) : previous.items.filter(item => item.id !== confirmed.id)
          cache.setQueryData(key, { ...previous, items, total: Math.max(0, previous.total - (included ? 0 : 1)) })
        }
      }
      void refresh()
    },
  })
  const all = useMutation({ mutationFn: () => post('/notifications/inbox/read-all'),
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
    all.mutate(undefined, { onSettled: () => { lock.current = false } })
  }
  if (!allowed) return <Notice>Tu rol no permite consultar las notificaciones.</Notice>
  return <><PageHeading title="Notificaciones" description="Resultados de tus procesos y automatizaciones. La bandeja muestra recursos que puedes consultar actualmente." action={<button className="button secondary" title="Toda tu bandeja accesible, incluidas otras páginas y filtros" disabled={changing} onClick={markAll}><Check size={16}/> Marcar todas como leídas</button>}/>
    <section className="panel notifications-panel"><p className="muted">La casilla Leída cambia únicamente el estado de lectura. Abrir Ver detalle también marca la notificación como leída. Marcar todas abarca toda tu bandeja accesible, incluidas otras páginas y filtros.</p><div className="notification-filters"><Field label="Módulo"><select value={module} onChange={event => { setModule(event.target.value); setOffset(0) }}><option value="">Todos</option>{Object.entries(moduleNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></Field><Field label="Origen"><select value={origin} onChange={event => { setOrigin(event.target.value); setOffset(0) }}><option value="">Todos</option><option value="MANUAL">Manual</option><option value="SCHEDULED">Programado</option><option value="CHAINED">Encadenado</option><option value="AUTOMATIC">Automático</option></select></Field><Field label="Lectura"><select value={readState} onChange={event => { setReadState(event.target.value); setOffset(0) }}><option value="ALL">Todas</option><option value="READ">Leídas</option><option value="UNREAD">Sin leer</option></select></Field>
      <Field label="Estado técnico"><select value={status} onChange={event => { setStatus(event.target.value); setOffset(0) }}><option value="">Todos</option>{[['SUCCESS', 'Completada'], ['FAILED', 'Fallida'], ['FAILED_PRECONDITION', 'Precondiciones incumplidas'], ['PARTIAL', 'Entrega parcial'], ['UNKNOWN', 'Confirmación desconocida'], ['CANCELLED', 'Cancelada'], ['BLOCKED', 'Bloqueada'], ['SKIPPED', 'Omitida']].map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></Field></div>
      {query.isPending ? <Loading/> : query.error && (!query.data || all.isSuccess) ? <ErrorState error={query.error} retry={() => query.refetch()}/> : !query.data?.items.length ? <Empty title="Sin notificaciones" description="Los próximos resultados aparecerán aquí. Ajusta los filtros para consultar otras notificaciones."/> : <div className="table-scroll"><table><thead><tr><th>Fecha y origen</th><th>Resultado</th><th>Estado técnico</th><th>Decisión de negocio</th><th>Detalle</th><th>Leída</th></tr></thead><tbody>{query.data.items.map(item => <tr key={item.id} className={!item.read_at ? 'notification-unread' : ''}><td>{date(item.created_at)}<small className="reason-code">{moduleNames[item.module] || item.module} · {{ MANUAL: 'Manual', SCHEDULED: 'Programado', CHAINED: 'Encadenado', AUTOMATIC: 'Automático' }[item.origin] || item.origin}</small></td><td>{!item.read_at && <strong>Sin leer · </strong>}{item.description}{item.error && <small className="reason-code">{item.error.code}{item.error.reference && <> · Referencia: {item.error.reference}</>}</small>}</td><td><Badge value={item.status}/></td><td>{item.decision ? <Badge value={item.decision}/> : '—'}</td><td><Link to={item.detail_url} aria-disabled={changing} onClick={event => { if (changing || lock.current) { event.preventDefault(); return } if (!item.read_at) { const modified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey; if (!modified) event.preventDefault(); setRead(item.id, true, modified ? undefined : item.detail_url) } }}>Ver detalle</Link></td><td><input type="checkbox" aria-label={`Leída: ${item.description}`} checked={!!item.read_at} disabled={changing} onChange={event => setRead(item.id, event.target.checked)}/></td></tr>)}</tbody></table></div>}
      {query.error && query.data && <ErrorState error={query.error} retry={() => query.refetch()}/>}<Pagination offset={offset} total={query.data?.total || 0} limit={25} onChange={setOffset}/>{(readStateMutation.error || all.error) && <ErrorState error={readStateMutation.error || all.error}/>}
    </section></>
}
