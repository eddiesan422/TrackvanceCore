import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { Activity, ArrowUpRight, BookOpen, Check, ClipboardList, Cpu, RefreshCw, ShieldCheck, Users } from 'lucide-react'
import { api } from '../api/client'
import type { Collection, RecordData } from '../api/client'
import { Badge, date, Empty, ErrorState, Field, label, Loading, Modal, Notice, number, PageHeading, SearchBox } from '../components/ui'

const states = ['OPEN', 'INVESTIGATING', 'WAITING_EXTERNAL', 'RESOLVED', 'ACCEPTED', 'FALSE_POSITIVE']
const transitions: Record<string, string[]> = {
  OPEN: ['INVESTIGATING', 'WAITING_EXTERNAL', 'RESOLVED', 'ACCEPTED', 'FALSE_POSITIVE'],
  INVESTIGATING: ['WAITING_EXTERNAL', 'RESOLVED', 'ACCEPTED', 'FALSE_POSITIVE'],
  WAITING_EXTERNAL: ['INVESTIGATING', 'RESOLVED', 'ACCEPTED'],
  RESOLVED: ['OPEN'], ACCEPTED: ['OPEN'], FALSE_POSITIVE: ['OPEN'],
}

function ExceptionEditor({ item, onSaved }: { item: RecordData; onSaved: () => void }) {
  const cache = useQueryClient()
  const [state, setState] = useState(item.state), [owner, setOwner] = useState(item.owner)
  const [cause, setCause] = useState(item.root_cause || ''), [resolution, setResolution] = useState(item.resolution || '')
  const [comment, setComment] = useState('')
  const update = useMutation({
    mutationFn: () => api(`/exceptions/${item.id}`, { method: 'PATCH', body: JSON.stringify({ version: item.version, state, owner, root_cause: cause, resolution, comment }) }),
    onSuccess: () => {
      cache.invalidateQueries({ queryKey: ['exceptions'] })
      cache.invalidateQueries({ queryKey: ['exception', item.id] })
      cache.invalidateQueries({ queryKey: ['dashboard'] })
      cache.invalidateQueries({ queryKey: ['audit'] })
      onSaved()
    },
  })
  return <form className="form-stack" onSubmit={event => { event.preventDefault(); update.mutate() }}>
    <div className="detail-summary"><Badge value={item.severity}/><Badge value={item.state}/><span>{label(item.module)}</span><Link to={`/runs/${item.run_id}`} className="text-link">Ver ejecución de origen <ArrowUpRight size={15}/></Link></div>
    <div className="form-grid"><Field label="Estado"><select value={state} onChange={event => setState(event.target.value)}>{[item.state, ...(transitions[item.state] || [])].map(value => <option key={value} value={value}>{label(value)}</option>)}</select></Field><Field label="Responsable"><input required maxLength={120} value={owner} onChange={event => setOwner(event.target.value)}/></Field></div>
    <Field label="Causa raíz" hint="Describe por qué ocurrió la diferencia."><textarea rows={3} maxLength={10000} value={cause} onChange={event => setCause(event.target.value)} required={state === 'RESOLVED'}/></Field>
    <Field label="Resolución o justificación"><textarea rows={3} maxLength={10000} value={resolution} onChange={event => setResolution(event.target.value)} required={['RESOLVED', 'ACCEPTED', 'FALSE_POSITIVE'].includes(state)}/></Field>
    <Field label="Comentario para el historial"><input maxLength={10000} value={comment} onChange={event => setComment(event.target.value)} placeholder="Qué cambió en esta revisión"/></Field>
    {update.error && <ErrorState error={update.error} retry={() => cache.invalidateQueries({ queryKey: ['exception', item.id] })}/>}
    <div className="modal-footer"><span className="muted">Versión {item.version} · {date(item.updated_at)}</span><button className="button primary" disabled={update.isPending}><Check size={16}/>{update.isPending ? 'Guardando…' : 'Guardar cambios'}</button></div>
    <section className="case-history"><h3>Historial de la excepción</h3>{[...(item.events || [])].reverse().map((event: RecordData, index: number) => <div className="history-event" key={`${event.timestamp}-${index}`}><Activity size={16}/><div><strong>{event.actor} · {label(event.to_state)}</strong><p>{event.comment}</p><small>{date(event.timestamp)}</small></div></div>)}</section>
  </form>
}

export function ExceptionsPage() {
  const [params, setParams] = useSearchParams(), selected = params.get('id')
  const [search, setSearch] = useState(''), [state, setState] = useState(''), [severity, setSeverity] = useState(''), [saved, setSaved] = useState(false)
  const cases = useQuery({ queryKey: ['exceptions', 'list'], queryFn: () => api<Collection>('/exceptions') })
  const detail = useQuery({ queryKey: ['exception', selected], queryFn: () => api(`/exceptions/${selected}`), enabled: !!selected })
  const rows = cases.data?.items || [], filtered = rows.filter(item => `${item.title} ${item.display_id} ${item.owner}`.toLowerCase().includes(search.toLowerCase()) && (!state || item.state === state) && (!severity || item.severity === severity))
  const open = rows.filter(item => ['OPEN', 'INVESTIGATING', 'WAITING_EXTERNAL'].includes(item.state))
  return <><PageHeading eyebrow="DEL HALLAZGO A LA RESOLUCIÓN" title="Excepciones" description="Investiga cada diferencia, asigna un responsable y conserva las decisiones de tu equipo."/>
    <div className="compact-stats"><div><ClipboardList size={19}/><strong>{number(open.length)}</strong><span>pendientes de resolución</span></div><div><ShieldCheck size={19}/><strong>{number(rows.filter(item => item.state === 'RESOLVED').length)}</strong><span>resueltas</span></div><div><Activity size={19}/><span>Historial y evidencia en cada caso</span></div></div>
    {saved && <Notice success>Los cambios de la excepción quedaron registrados.</Notice>}
    <section className="panel"><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar por caso, título o responsable…"/><div className="toolbar-right"><select aria-label="Filtrar por estado" value={state} onChange={event => setState(event.target.value)}><option value="">Todos los estados</option>{states.map(value => <option key={value} value={value}>{label(value)}</option>)}</select><select aria-label="Filtrar por severidad" value={severity} onChange={event => setSeverity(event.target.value)}><option value="">Todas las prioridades</option>{['CRITICAL','HIGH','MEDIUM','LOW'].map(value => <option key={value} value={value}>{label(value)}</option>)}</select></div></div>
      {cases.isPending ? <Loading/> : cases.error ? <ErrorState error={cases.error} retry={() => cases.refetch()}/> : !filtered.length ? <Empty title="Sin excepciones en esta vista" description="Ajusta los filtros o crea una excepción desde los hallazgos de una ejecución."/> : <div className="table-scroll"><table><thead><tr><th>Caso</th><th>Módulo</th><th>Prioridad</th><th>Estado</th><th>Responsable</th><th>Actualización</th><th/></tr></thead><tbody>{filtered.map(item => <tr key={item.id}><td><button className="table-primary text-button" onClick={() => { setSaved(false); setParams({ id: item.id }) }}>{item.title}</button><small className="table-subtitle mono">{item.display_id}</small></td><td><span className={`module-label ${item.module}`}>{label(item.module)}</span></td><td><Badge value={item.severity}/></td><td><Badge value={item.state}/></td><td>{item.owner}</td><td className="muted no-wrap">{date(item.updated_at)}</td><td><button className="icon-button" aria-label={`Abrir ${item.display_id}`} onClick={() => { setSaved(false); setParams({ id: item.id }) }}><ArrowUpRight size={17}/></button></td></tr>)}</tbody></table></div>}
    </section><Modal open={!!selected} onOpenChange={value => { if (!value) setParams({}) }} title={detail.data ? `${detail.data.display_id} · ${detail.data.title}` : 'Detalle de la excepción'} description="Los cambios se guardan con responsable, fecha y versión." wide>{detail.isPending ? <Loading/> : detail.error ? <ErrorState error={detail.error} retry={() => detail.refetch()}/> : detail.data && <ExceptionEditor key={`${detail.data.id}-${detail.data.version}`} item={detail.data} onSaved={() => setSaved(true)}/>}</Modal>
  </>
}

export function AuditPage() {
  const [search, setSearch] = useState(''), [actor, setActor] = useState('')
  const events = useQuery({ queryKey: ['audit'], queryFn: () => api<Collection>('/audit-events') })
  const rows = events.data?.items || [], filtered = rows.filter(item => `${item.message} ${item.event_type} ${item.subject_id}`.toLowerCase().includes(search.toLowerCase()) && (!actor || item.actor === actor))
  return <><PageHeading eyebrow="EVIDENCIA DE CADA ACCIÓN" title="Auditoría" description="Consulta quién realizó cada acción y sobre qué datos, controles o excepciones." action={<button className="button secondary" onClick={() => events.refetch()} disabled={events.isFetching}><RefreshCw size={16}/> Actualizar</button>}/><section className="panel"><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar acción o identificador…"/><select aria-label="Filtrar por actor" value={actor} onChange={event => setActor(event.target.value)}><option value="">Todos los responsables</option>{[...new Set(rows.map(item => String(item.actor)))].map(value => <option key={value}>{value}</option>)}</select></div>
    {events.isPending ? <Loading/> : events.error ? <ErrorState error={events.error} retry={() => events.refetch()}/> : !filtered.length ? <Empty title="Sin eventos en esta vista" description="Las cargas, ejecuciones y decisiones generan registros de auditoría."/> : <div className="table-scroll"><table><thead><tr><th>Fecha</th><th>Responsable</th><th>Acción</th><th>Referencia</th></tr></thead><tbody>{filtered.map(item => <tr key={item.id}><td className="no-wrap muted">{date(item.created_at)}</td><td>{item.actor}</td><td><strong>{item.message}</strong><small className="table-subtitle mono">{item.event_type}</small></td><td>{item.subject_type === 'run' ? <Link className="text-link" to={`/runs/${item.subject_id}`}>Ver ejecución <ArrowUpRight size={14}/></Link> : item.subject_type === 'exception' ? <Link className="text-link" to={`/exceptions?id=${item.subject_id}`}>Ver excepción <ArrowUpRight size={14}/></Link> : <span className="mono muted">{String(item.subject_id).slice(0, 12)}</span>}</td></tr>)}</tbody></table></div>}
  </section></>
}

export function RulesPage() {
  const [search, setSearch] = useState(''), [module, setModule] = useState('')
  const rules = useQuery({ queryKey: ['rules'], queryFn: () => api<Collection>('/rules') })
  const catalog = (rules.data?.items || []).map(item => item.code === 'EXACT_MATCH' ? { ...item, code: 'NUMERIC_TOLERANCE', name: 'Tolerancia numérica', description: 'Compara importes decimales con tolerancia absoluta. Las ejecuciones históricas conservan su configuración.', legacy_aliases: ['EXACT_MATCH'] } : item)
  const filtered = catalog.filter(item => `${item.name} ${item.code} ${item.description} ${(item.legacy_aliases || []).join(' ')}`.toLowerCase().includes(search.toLowerCase()) && (!module || module === item.module))
  return <><PageHeading eyebrow="CRITERIOS CLAROS Y REUTILIZABLES" title="Biblioteca de reglas" description="Estas son las comprobaciones disponibles en el prototipo. Configúralas desde cada módulo."/>
    <section className="panel"><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar una regla…"/><select aria-label="Filtrar por módulo" value={module} onChange={event => setModule(event.target.value)}><option value="">Todos los módulos</option>{['intake','recon','sentinel'].map(value => <option key={value} value={value}>{label(value)}</option>)}</select></div>
    {rules.isPending ? <Loading/> : rules.error ? <ErrorState error={rules.error} retry={() => rules.refetch()}/> : !filtered.length ? <Empty title="No encontramos reglas" description="Prueba otro término o cambia el módulo seleccionado."/> : <div className="table-scroll"><table><thead><tr><th>Regla</th><th>Qué comprueba</th><th>Módulo</th><th/></tr></thead><tbody>{filtered.map(item => <tr key={item.id}><td><div className="cell-with-icon"><span className="data-icon"><BookOpen size={18}/></span><div><strong>{item.name}</strong><small className="table-subtitle mono">{item.code}</small>{item.legacy_aliases?.length > 0 && <small className="table-subtitle">Alias histórico: <code>{item.legacy_aliases.join(', ')}</code></small>}</div></div></td><td>{item.description}</td><td><span className={`module-label ${item.module}`}>{label(item.module)}</span></td><td><Link to={`/${item.module}`} className="text-link">Configurar <ArrowUpRight size={15}/></Link></td></tr>)}</tbody></table></div>}
    </section></>
}

export function SettingsPage() {
  const [tab, setTab] = useState('system')
  const engines = useQuery({ queryKey: ['engines'], queryFn: () => api('/system/engines'), refetchInterval: 10000 })
  const users = useQuery({ queryKey: ['users'], queryFn: () => api<Collection>('/users'), enabled: tab === 'users' })
  return <><PageHeading eyebrow="TU ENTORNO DE TRABAJO" title="Configuración" description="Consulta el estado del procesamiento local y las cuentas disponibles."/>
    <div className="tabs"><button className={tab === 'system' ? 'active' : ''} onClick={() => setTab('system')}><Cpu size={16}/> Procesamiento y entorno</button><button className={tab === 'users' ? 'active' : ''} onClick={() => setTab('users')}><Users size={16}/> Usuarios locales</button></div>
    {tab === 'system' ? engines.isPending ? <Loading/> : engines.error ? <ErrorState error={engines.error} retry={() => engines.refetch()}/> : <>
      <section className="panel"><div className="panel-heading"><div><h2>Estado del procesador</h2><p>Los controles se ejecutan en un proceso local independiente.</p></div><Badge value={engines.data.worker?.status}/></div><div className="profile-stats"><div><span>Última señal</span><strong style={{ fontSize: '1rem' }}>{date(engines.data.worker?.last_seen)}</strong></div><div><span>Máximo por carga</span><strong>{number(engines.data.limits?.max_upload_mb)} <small>MiB</small></strong></div><div><span>Filas por archivo</span><strong>{number(engines.data.limits?.max_rows)}</strong></div></div></section>
      <section className="panel" style={{ marginTop: 24 }}><div className="panel-heading"><div><h2>Motores de procesamiento</h2><p>Disponibilidad efectiva de esta instalación.</p></div></div><div className="table-scroll"><table><thead><tr><th>Motor</th><th>Disponibilidad</th><th>Versión</th><th>Capacidad</th></tr></thead><tbody>{engines.data.items?.map((item: RecordData) => <tr key={item.id}><td><strong>{item.name}</strong></td><td><Badge value={item.available ? 'ACTIVE' : 'PLANNED'}>{item.available ? 'Disponible' : 'Próxima etapa'}</Badge></td><td className="mono">{item.version || '—'}</td><td>{item.description}</td></tr>)}</tbody></table></div></section>
      <Notice>Las cargas admiten CSV UTF-8 y los informes se descargan en Excel XLSX. Las reglas avanzadas se configuran desde cada módulo. Los permisos se aplican por rol; la administración de cuentas todavía no dispone de formulario.</Notice>
    </> : <section className="panel"><div className="panel-heading"><div><h2>Cuentas de esta organización</h2><p>Consulta de usuarios; la edición de roles queda para la siguiente etapa.</p></div></div>{users.isPending ? <Loading/> : users.error ? <ErrorState error={users.error} retry={() => users.refetch()}/> : <div className="table-scroll"><table><thead><tr><th>Nombre</th><th>Correo</th><th>Rol</th></tr></thead><tbody>{users.data?.items.map(item => <tr key={item.id}><td>{item.name}</td><td>{item.email}</td><td><Badge value={item.role}/></td></tr>)}</tbody></table></div>}</section>}
  </>
}
