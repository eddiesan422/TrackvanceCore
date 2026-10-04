import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Bell, Check } from 'lucide-react'
import { api, post } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Notice, PageHeading, Pagination } from '../../components/ui'

type Notification = { id: string; module: string; origin: string; status: string; decision: string | null; description: string; created_at: string; read_at: string | null; detail_url: string }

export function NotificationIndicator() {
  const allowed = usePermission('notifications:read')
  const count = useQuery({ queryKey: ['notification-count'], queryFn: () => api<{ unread_count: number }>('/notifications/unread-count'), enabled: allowed, refetchInterval: 15000 })
  if (!allowed) return null
  const unread = count.data?.unread_count || 0
  return <Link to="/notifications" className="icon-button notification" aria-label={`Ver notificaciones${unread ? `, ${unread} sin leer` : ''}`} title="Notificaciones"><Bell size={19}/>{unread > 0 && <span className="tiny-label">{unread > 99 ? '99+' : unread}</span>}</Link>
}

export function NotificationsPage() {
  const allowed = usePermission('notifications:read'), cache = useQueryClient()
  const [offset, setOffset] = useState(0), [readState, setReadState] = useState('ALL'), [module, setModule] = useState(''), [origin, setOrigin] = useState(''), [status, setStatus] = useState('')
  const query = useQuery({ queryKey: ['notification-inbox', offset, readState, module, origin, status], enabled: allowed,
    queryFn: () => api<{ items: Notification[]; total: number }>(`/notifications/inbox?offset=${offset}&limit=25&read_state=${readState}&module=${module}&origin=${origin}&status=${status}`), refetchInterval: 15000 })
  const refresh = () => { void cache.invalidateQueries({ queryKey: ['notification-inbox'] }); void cache.invalidateQueries({ queryKey: ['notification-count'] }) }
  const read = useMutation({ mutationFn: (id: string) => post(`/notifications/inbox/${id}/read`), onSuccess: refresh })
  const all = useMutation({ mutationFn: () => post('/notifications/inbox/read-all'), onSuccess: refresh })
  if (!allowed) return <Notice>Tu rol no permite consultar las notificaciones.</Notice>
  return <><PageHeading title="Notificaciones" description="Resultados de tus procesos y automatizaciones. La bandeja muestra recursos que puedes consultar actualmente." action={<button className="button secondary" disabled={all.isPending || !query.data?.total} onClick={() => all.mutate()}><Check size={16}/> Marcar todas como leídas</button>}/>
    <section className="panel"><div className="table-toolbar"><Field label="Módulo"><select value={module} onChange={event => { setModule(event.target.value); setOffset(0) }}><option value="">Todos</option>{[['acquisition', 'Adquisición'], ['intake', 'Intake'], ['recon', 'ReconOps'], ['sentinel', 'Sentinel'], ['DELIVERY', 'Data Delivery']].map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></Field><Field label="Origen"><select value={origin} onChange={event => { setOrigin(event.target.value); setOffset(0) }}><option value="">Todos</option><option value="MANUAL">Manual</option><option value="SCHEDULED">Programado</option><option value="CHAINED">Encadenado</option></select></Field><Field label="Lectura"><select value={readState} onChange={event => { setReadState(event.target.value); setOffset(0) }}><option value="ALL">Todas</option><option value="READ">Leídas</option><option value="UNREAD">Sin leer</option></select></Field></div>
      <div className="table-toolbar"><Field label="Estado técnico"><select value={status} onChange={event => { setStatus(event.target.value); setOffset(0) }}><option value="">Todos</option>{[['SUCCESS', 'Completada'], ['FAILED', 'Fallida'], ['FAILED_PRECONDITION', 'Precondiciones incumplidas'], ['UNKNOWN', 'Confirmación desconocida'], ['CANCELLED', 'Cancelada'], ['BLOCKED', 'Bloqueada'], ['SKIPPED', 'Omitida']].map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></Field></div>
      {query.isPending ? <Loading/> : query.error ? <ErrorState error={query.error} retry={() => query.refetch()}/> : !query.data.items.length ? <Empty title="Sin notificaciones" description="Los próximos resultados aparecerán aquí. Ajusta los filtros para consultar otras notificaciones."/> : <div className="table-scroll"><table><thead><tr><th>Fecha y origen</th><th>Resultado</th><th>Estado técnico</th><th>Decisión</th><th>Detalle</th></tr></thead><tbody>{query.data.items.map(item => <tr key={item.id}><td>{date(item.created_at)}<small className="reason-code">{item.module} · {{ MANUAL: 'Manual', SCHEDULED: 'Programado', CHAINED: 'Encadenado' }[item.origin] || item.origin}</small></td><td>{!item.read_at && <strong>Sin leer · </strong>}{item.description}</td><td><Badge value={item.status}/></td><td>{item.decision ? <Badge value={item.decision}/> : '—'}</td><td><Link to={item.detail_url} onClick={() => { if (!item.read_at) read.mutate(item.id) }}>Ver detalle</Link>{!item.read_at && <button className="text-button" disabled={read.isPending} onClick={() => read.mutate(item.id)}>Marcar leída</button>}</td></tr>)}</tbody></table></div>}
      <Pagination offset={offset} total={query.data?.total || 0} limit={25} onChange={setOffset}/>{(read.error || all.error) && <ErrorState error={read.error || all.error}/>}
    </section></>
}
