import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowRight, Pencil, Plug, Plus, Send, ShieldCheck, Trash2 } from 'lucide-react'
import { api, post } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Modal, Notice, number, PageHeading, SearchBox } from '../../components/ui'
import type { DeliveryDestination, DestinationConfig, DestinationInput, DestinationTestResult, SinkType } from './types'
import { destinationVersionId } from './types'
import { DeliveryNavigation } from './DeliveryNavigation'
import './delivery.css'

interface DestinationForm {
  name: string
  sink_type: SinkType
  config: DestinationConfig
  password?: string
}

const sinkLabel = (value: SinkType) => value === 'POSTGRESQL' ? 'PostgreSQL' : 'SQL Server'
const defaultConfig = (sink: SinkType): DestinationConfig => ({
  host: '',
  port: sink === 'POSTGRESQL' ? 5432 : 1433,
  database: '',
  username: '',
  connect_timeout: 5,
  query_timeout: 60,
  ...(sink === 'POSTGRESQL' ? { sslmode: 'require' } : { encryption: 'require' }),
})
const transportMode = (sink: SinkType, config: DestinationConfig) => sink === 'POSTGRESQL' ? config.sslmode || 'require' : config.encryption || 'require'

function savedConfig(destination: DeliveryDestination): DestinationConfig {
  const defaults = defaultConfig(destination.sink_type)
  return {
    ...defaults,
    host: destination.host,
    port: destination.port,
    database: destination.database,
    username: destination.username,
    connect_timeout: Number(destination.options?.connect_timeout ?? defaults.connect_timeout),
    query_timeout: Number(destination.options?.query_timeout ?? defaults.query_timeout),
    ...(destination.sink_type === 'POSTGRESQL'
      ? { sslmode: String(destination.options?.sslmode || defaults.sslmode) }
      : { encryption: String(destination.options?.encryption || defaults.encryption) }),
  }
}

function TestResult({ result }: { result: DestinationTestResult }) {
  return <Notice success={result.status === 'SUCCESS'}><strong>{result.status === 'SUCCESS' ? 'Destino verificado' : 'No se pudo conectar'}</strong> · {result.message}<small className="delivery-test-code">{result.code ? `${result.code} · ` : ''}{date(result.tested_at)}</small></Notice>
}

export function DestinationDialog({ destination, close, saved }: { destination?: DeliveryDestination; close: () => void; saved: (destination: DeliveryDestination) => void }) {
  const initial = destination ? savedConfig(destination) : defaultConfig('POSTGRESQL')
  const [form, setForm] = useState<DestinationForm>(() => ({ name: destination?.name || '', sink_type: destination?.sink_type || 'POSTGRESQL', config: initial, password: '' }))
  const [result, setResult] = useState<DestinationTestResult | null>(null)
  const revision = useRef(0)
  const test = useMutation({
    mutationFn: ({ input }: { input: DestinationInput; revision: number }) => post<DestinationTestResult>('/delivery/destinations/test', { ...input, ...(destination ? { destination_id: destination.id } : {}) }),
    onSuccess: (data, variables) => { if (variables.revision === revision.current) setResult(data) },
  })
  const save = useMutation({
    mutationFn: () => {
      const input = payload()
      if (!destination) return post<DeliveryDestination>('/delivery/destinations', input)
      return api<DeliveryDestination>(`/delivery/destinations/${destination.id}`, { method: 'PATCH', body: JSON.stringify({ name: input.name, host: input.host, port: input.port, database: input.database, username: input.username, options: input.options, ...(input.password ? { password: input.password } : {}), version: destination.version }) })
    },
    onSuccess: saved,
  })
  function payload(): DestinationInput {
    const { password, config, ...input } = form
    return {
      ...input,
      name: input.name.trim(),
      host: config.host.trim(),
      port: config.port,
      database: config.database.trim(),
      username: config.username.trim(),
      options: {
        connect_timeout: config.connect_timeout,
        query_timeout: config.query_timeout,
        ...(form.sink_type === 'POSTGRESQL' ? { sslmode: config.sslmode } : { encryption: config.encryption }),
      },
      ...(password ? { password } : {}),
    }
  }
  function change(updates: Partial<DestinationForm>, config?: Partial<DestinationConfig>) {
    revision.current += 1
    setResult(null)
    test.reset()
    save.reset()
    setForm(current => ({ ...current, ...updates, ...(config ? { config: { ...current.config, ...config } } : {}) }))
  }
  const original = destination ? savedConfig(destination) : null
  const endpointChanged = !!destination && !!original && (
    form.config.host.trim() !== original.host || form.config.port !== original.port || form.config.database !== original.database
    || form.config.username !== original.username || transportMode(form.sink_type, form.config) !== transportMode(destination.sink_type, original)
  )
  const passwordAvailable = !!form.password || (!!destination && !endpointChanged)
  const valid = !!(form.name.trim() && form.config.host.trim() && form.config.database && form.config.username && passwordAvailable
    && Number.isInteger(form.config.port) && form.config.port > 0 && form.config.port <= 65535
    && Number.isInteger(form.config.connect_timeout) && form.config.connect_timeout >= 1 && form.config.connect_timeout <= 15
    && Number.isInteger(form.config.query_timeout) && form.config.query_timeout >= 1 && form.config.query_timeout <= 300)
  return <Modal open onOpenChange={value => { if (!value && !save.isPending) close() }} title={destination ? 'Editar destino' : 'Nuevo destino'} description="Configura una cuenta de escritura independiente. Trackvance probará la configuración actual antes de guardarla." wide>
    <form className="form-stack" onSubmit={event => { event.preventDefault(); if (result?.status === 'SUCCESS') save.mutate() }}>
      <fieldset className="delivery-fields" disabled={save.isPending}>
        <div className="form-grid">
          <Field label="Motor de destino"><select disabled={!!destination} value={form.sink_type} onChange={event => { const sink = event.target.value as SinkType; change({ sink_type: sink, config: defaultConfig(sink) }) }}><option value="POSTGRESQL">PostgreSQL</option><option value="SQLSERVER">SQL Server</option></select></Field>
          <Field label="Nombre del destino"><input required maxLength={160} value={form.name} onChange={event => change({ name: event.target.value })}/></Field>
          <Field label="Host" hint="Dirección accesible únicamente desde API y delivery-worker."><input required maxLength={253} value={form.config.host} onChange={event => change({}, { host: event.target.value })} placeholder="db-destino.ejemplo.local"/></Field>
          <Field label="Puerto"><input required type="number" min={1} max={65535} value={form.config.port} onChange={event => change({}, { port: Number(event.target.value) })}/></Field>
          <Field label="Base de datos"><input required maxLength={128} value={form.config.database} onChange={event => change({}, { database: event.target.value })}/></Field>
          <Field label="Usuario de escritura"><input required maxLength={128} autoComplete="off" value={form.config.username} onChange={event => change({}, { username: event.target.value })}/></Field>
          <Field label="Contraseña" hint={destination ? endpointChanged ? 'Indícala nuevamente al cambiar host, puerto, base de datos, usuario o cifrado.' : 'Déjala vacía para conservar la credencial del destino actual.' : 'Se almacena cifrada y no vuelve a mostrarse.'}><input required={!destination || endpointChanged} maxLength={1024} type="password" autoComplete="new-password" value={form.password || ''} onChange={event => change({ password: event.target.value })}/></Field>
          <Field label="Cifrado de transporte" hint="Desactívalo solamente para fixtures locales desechables."><select value={form.sink_type === 'POSTGRESQL' ? form.config.sslmode : form.config.encryption} onChange={event => change({}, form.sink_type === 'POSTGRESQL' ? { sslmode: event.target.value } : { encryption: event.target.value })}><option value="require">TLS requerido</option>{form.sink_type === 'POSTGRESQL' && <><option value="verify-ca">TLS y certificado</option><option value="verify-full">TLS e identidad</option></>}<option value={form.sink_type === 'POSTGRESQL' ? 'disable' : 'off'}>Sin TLS · pruebas locales</option></select></Field>
          <Field label="Tiempo máximo de conexión (segundos)"><input type="number" required min={1} max={15} value={form.config.connect_timeout} onChange={event => change({}, { connect_timeout: Number(event.target.value) })}/></Field>
          <Field label="Tiempo máximo de escritura (segundos)"><input type="number" required min={1} max={300} value={form.config.query_timeout} onChange={event => change({}, { query_timeout: Number(event.target.value) })}/></Field>
        </div>
      </fieldset>
      <Notice>Usa una cuenta con privilegios mínimos sobre los schemas y tablas previstos. Las credenciales de fuentes y destinos permanecen segregadas.</Notice>
      {result && <TestResult result={result}/>} {test.error && <ErrorState error={test.error}/>} {save.error && <ErrorState error={save.error}/>} {!result && <p className="delivery-help">Prueba la configuración actual para habilitar el guardado.</p>}
      <div className="modal-footer"><button type="button" className="button secondary" disabled={save.isPending} onClick={close}>Cancelar</button><button type="button" className="button secondary" disabled={!valid || test.isPending || save.isPending} onClick={() => test.mutate({ input: payload(), revision: revision.current })}><Plug size={16}/>{test.isPending ? 'Probando destino…' : 'Probar destino'}</button><button className="button primary" disabled={!valid || result?.status !== 'SUCCESS' || test.isPending || save.isPending}>{save.isPending ? 'Guardando…' : 'Guardar destino'}</button></div>
    </form>
  </Modal>
}

export function DestinationsPage() {
  const canRead = usePermission('connections:read'), canManage = usePermission('connections:manage')
  const [creating, setCreating] = useState(false), [search, setSearch] = useState('')
  const cache = useQueryClient(), navigate = useNavigate()
  const destinations = useQuery({ queryKey: ['delivery-destinations'], queryFn: () => api<{ items: DeliveryDestination[]; total: number }>('/delivery/destinations'), enabled: canRead })
  if (!canRead) return <><DeliveryNavigation/><Notice>No tienes permisos para consultar los destinos.</Notice></>
  const items = (destinations.data?.items || []).filter(item => `${item.name} ${item.sink_type} ${item.database || ''}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()))
  return <><DeliveryNavigation/><PageHeading eyebrow="DATA DELIVERY / DESTINOS" title="Destinos" description="Administra conexiones de escritura versionadas y separadas de las fuentes de adquisición." action={<button className="button primary" disabled={!canManage} onClick={() => setCreating(true)}><Plus size={17}/> Nuevo destino</button>}/>
    <div className="module-intro delivery"><ShieldCheck size={25}/><div><strong>Publicación controlada · credenciales segregadas</strong><p>Un destino define dónde publicar; cada entrega fija la revisión exacta que utilizó.</p></div></div>
    <section className="panel"><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar destino o motor…"/><span className="muted">{number(destinations.data?.total || 0)} destinos</span></div>
      {destinations.isPending ? <Loading text="Cargando destinos…"/> : destinations.error ? <ErrorState error={destinations.error} retry={() => destinations.refetch()}/> : !items.length ? <Empty title={search ? 'No hay coincidencias' : 'Crea tu primer destino'} description={search ? 'Prueba otro nombre o motor.' : 'Agrega PostgreSQL o SQL Server para preparar entregas controladas.'}/> : <div className="table-scroll"><table><thead><tr><th>Destino</th><th>Motor</th><th>Estado</th><th>Última prueba</th><th>Última actualización</th><th/></tr></thead><tbody>{items.map(destination => <tr key={destination.id}><td><Link className="table-primary" to={`/delivery/destinations/${destination.id}`}>{destination.name}</Link><span className="table-subtitle">Revisión {destination.version}</span></td><td>{sinkLabel(destination.sink_type)}</td><td><Badge value={destination.enabled && !destination.deleted ? 'ACTIVE' : 'INACTIVE'}/></td><td><Badge value={destination.last_test_status || 'NOT_REQUESTED'}>{destination.last_test_status === 'SUCCESS' ? 'Conectado' : destination.last_test_status === 'FAILED' ? 'Falló la prueba' : 'Sin probar'}</Badge><span className="table-subtitle">{date(destination.last_test_at)}</span></td><td>{date(destination.updated_at)}</td><td><Link className="text-link" to={`/delivery/destinations/${destination.id}`} aria-label={`Abrir ${destination.name}`}><ArrowRight size={17}/></Link></td></tr>)}</tbody></table></div>}
    </section>
    {creating && <DestinationDialog close={() => setCreating(false)} saved={destination => { cache.invalidateQueries({ queryKey: ['delivery-destinations'] }); setCreating(false); navigate(`/delivery/destinations/${destination.id}`) }}/>}
  </>
}

export function DestinationDetail() {
  const { id } = useParams(), cache = useQueryClient(), navigate = useNavigate()
  const canRead = usePermission('connections:read'), canManage = usePermission('connections:manage')
  const [editing, setEditing] = useState(false), [confirm, setConfirm] = useState<'toggle' | 'delete' | null>(null)
  const destination = useQuery({ queryKey: ['delivery-destination', id], queryFn: () => api<DeliveryDestination>(`/delivery/destinations/${id}`), enabled: canRead && !!id })
  function invalidate() { cache.invalidateQueries({ queryKey: ['delivery-destinations'] }); cache.invalidateQueries({ queryKey: ['delivery-destination', id] }) }
  const test = useMutation({ mutationFn: () => post<DestinationTestResult>(`/delivery/destinations/${id}/test`), onSettled: invalidate })
  const change = useMutation({
    mutationFn: () => confirm === 'delete'
      ? api(`/delivery/destinations/${id}?version=${destination.data!.version}`, { method: 'DELETE' })
      : api(`/delivery/destinations/${id}`, { method: 'PATCH', body: JSON.stringify({ version: destination.data!.version, enabled: !destination.data!.enabled }) }),
    onSuccess: () => { invalidate(); if (confirm === 'delete') navigate('/delivery/destinations'); setConfirm(null) },
  })
  if (!canRead) return <><DeliveryNavigation/><Notice>No tienes permisos para consultar este destino.</Notice></>
  if (destination.isPending) return <Loading text="Cargando destino…"/>
  if (destination.error) return <ErrorState error={destination.error} retry={() => destination.refetch()}/>
  const data = destination.data, config = savedConfig(data)
  return <><DeliveryNavigation/><PageHeading back="/delivery/destinations" eyebrow="DATA DELIVERY / DESTINO" title={data.name} description={`${sinkLabel(data.sink_type)} · ${config.database}`} action={<><button className="button secondary" disabled={!canManage} onClick={() => setEditing(true)}><Pencil size={15}/> Editar destino</button><button className="button primary" disabled={!canManage || !data.enabled || test.isPending} onClick={() => test.mutate()}><Plug size={16}/>{test.isPending ? 'Probando…' : 'Probar destino'}</button></>}/>
    <section className="panel delivery-details"><div className="delivery-summary"><div><span>Servidor</span><strong>{config.host}:{config.port}</strong></div><div><span>Cuenta de escritura</span><strong>{config.username}</strong></div><div><span>Revisión</span><strong>{data.version}</strong><small className="mono">{destinationVersionId(data).slice(0, 16) || '—'}</small></div><div><span>Estado</span><Badge value={data.enabled ? 'ACTIVE' : 'INACTIVE'}/></div><div><span>Última prueba</span><strong>{date(data.last_test_at)}</strong><Badge value={data.last_test_status || 'NOT_REQUESTED'}/></div></div>
      {test.data ? <TestResult result={test.data}/> : data.last_test_message && <p className="delivery-help">{data.last_test_message}</p>}{test.error && <ErrorState error={test.error}/>}<div className="delivery-actions"><button className="text-button" disabled={!canManage || change.isPending} onClick={() => { change.reset(); setConfirm('toggle') }}>{data.enabled ? 'Deshabilitar destino' : 'Habilitar destino'}</button><button className="text-button delivery-delete" disabled={!canManage || change.isPending} onClick={() => { change.reset(); setConfirm('delete') }}><Trash2 size={14}/> Dar de baja</button><Link className="button secondary small" to={`/delivery/new?destination=${data.id}`}><Send size={14}/> Preparar entrega</Link></div>
    </section>
    {!data.enabled && <Notice>El destino está deshabilitado. Las configuraciones y entregas históricas se conservan.</Notice>}
    {editing && <DestinationDialog destination={data} close={() => setEditing(false)} saved={() => { invalidate(); test.reset(); setEditing(false) }}/>}
    {confirm && <Modal open onOpenChange={open => { if (!open && !change.isPending) setConfirm(null) }} title={confirm === 'delete' ? 'Dar de baja el destino' : data.enabled ? 'Deshabilitar destino' : 'Habilitar destino'} description={confirm === 'delete' ? 'No estará disponible para nuevas entregas. Las revisiones, Runs, attempts y receipts históricos se conservarán.' : data.enabled ? 'Se impedirán nuevas entregas hacia este destino.' : 'Se volverá a permitir configurar y ejecutar entregas.'}><p>Destino: <strong>{data.name}</strong></p>{change.error && <ErrorState error={change.error}/>}<div className="modal-footer"><button className="button secondary" disabled={change.isPending} onClick={() => setConfirm(null)}>Cancelar</button><button className="button primary" disabled={change.isPending} onClick={() => change.mutate()}>{change.isPending ? 'Aplicando…' : 'Confirmar cambio'}</button></div></Modal>}
  </>
}
