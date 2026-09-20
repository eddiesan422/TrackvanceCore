import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { ArrowRight, Database, Pencil, Plug, Plus, RefreshCw, ShieldCheck, Trash2 } from 'lucide-react'
import { api, post } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Modal, Notice, number, PageHeading, SearchBox } from '../../components/ui'
import { SampleValue } from '../datasets/VersionIdentity'
import './connections.css'

type SourceType = 'POSTGRESQL' | 'SQLSERVER'
type ConnectionOptions = { connect_timeout: number; query_timeout: number; sslmode?: string; encryption?: string }
export type ConnectionRecord = {
  id: string; name: string; source_type: SourceType; host: string; port: number; database: string; username: string
  options: ConnectionOptions; enabled: boolean; version: number; last_test_status?: string | null
  last_test_at?: string | null; last_test_message?: string | null
}
type ConnectionInput = Pick<ConnectionRecord, 'name' | 'source_type' | 'host' | 'port' | 'database' | 'username' | 'options'> & { password?: string }
type ConnectionTest = { status: 'SUCCESS' | 'FAILED'; code?: string; message: string; tested_at: string }
type SourceObject = { name: string; kind: 'TABLE' | 'VIEW' }
type SourcePreview = { columns: { name: string; native_type: string; logical_type: string; nullable: boolean; numeric: boolean }[]; rows: Record<string, unknown>[]; sampled_rows: number }
const sourceLabel = (value: SourceType) => value === 'POSTGRESQL' ? 'PostgreSQL' : 'SQL Server'
const defaultOptions = (source: SourceType): ConnectionOptions => ({ connect_timeout: 5, query_timeout: 30, ...(source === 'POSTGRESQL' ? { sslmode: 'require' } : { encryption: 'require' }) })
const transportMode = (source: SourceType, options: ConnectionOptions) => source === 'POSTGRESQL' ? options.sslmode || 'require' : options.encryption || 'require'
const updateInput = (connection: Pick<ConnectionRecord, 'name' | 'host' | 'port' | 'database' | 'username' | 'options'>) => ({ name: connection.name, host: connection.host, port: connection.port, database: connection.database, username: connection.username, options: connection.options })

function TestResult({ result }: { result: ConnectionTest }) {
  return <Notice success={result.status === 'SUCCESS'}><strong>{result.status === 'SUCCESS' ? 'Conexión verificada' : 'No se pudo conectar'}</strong> · {result.message}<small className="connection-test-code">{result.code ? `${result.code} · ` : ''}{date(result.tested_at)}</small></Notice>
}

export function ConnectionDialog({ connection, close, saved }: { connection?: ConnectionRecord; close: () => void; saved: (connection: ConnectionRecord) => void }) {
  const [form, setForm] = useState<ConnectionInput>(() => connection ? { ...updateInput(connection), source_type: connection.source_type, options: { ...defaultOptions(connection.source_type), ...connection.options }, password: '' } : { name: '', source_type: 'POSTGRESQL', host: '', port: 5432, database: '', username: '', password: '', options: defaultOptions('POSTGRESQL') })
  const [result, setResult] = useState<ConnectionTest | null>(null)
  const revision = useRef(0)
  const test = useMutation({
    mutationFn: ({ input }: { input: ConnectionInput; revision: number }) => post<ConnectionTest>('/connections/test', { ...input, ...(connection ? { connection_id: connection.id } : {}) }),
    onSuccess: (data, variables) => { if (variables.revision === revision.current) setResult(data) },
  })
  const save = useMutation({
    mutationFn: () => {
      const input = payload()
      return connection ? api<ConnectionRecord>(`/connections/${connection.id}`, { method: 'PATCH', body: JSON.stringify({ ...updateInput(input), ...(input.password ? { password: input.password } : {}), version: connection.version }) }) : post<ConnectionRecord>('/connections', input)
    },
    onSuccess: saved,
  })
  function payload(): ConnectionInput {
    const { password, ...input } = form
    return { ...input, name: input.name.trim(), host: input.host.trim(), ...(password ? { password } : {}) }
  }
  function change(updates: Partial<ConnectionInput>) {
    revision.current += 1
    setResult(null)
    test.reset()
    save.reset()
    setForm(current => ({ ...current, ...updates }))
  }
  const endpointChanged = !!connection && (form.host.trim() !== connection.host || form.port !== connection.port || form.database !== connection.database || form.username !== connection.username || transportMode(form.source_type, form.options) !== transportMode(connection.source_type, connection.options))
  const passwordAvailable = !!form.password || (!!connection && !endpointChanged)
  const valid = !!(form.name.trim() && form.host.trim() && form.database && form.username && passwordAvailable && Number.isInteger(form.port) && form.port > 0 && form.port <= 65535 && Number.isInteger(form.options.connect_timeout) && form.options.connect_timeout >= 1 && form.options.connect_timeout <= 15 && Number.isInteger(form.options.query_timeout) && form.options.query_timeout >= 1 && form.options.query_timeout <= 60)
  return <Modal open onOpenChange={value => { if (!value && !save.isPending) close() }} title={connection ? 'Editar conexión' : 'Nueva conexión'} description="Conecta una fuente externa de lectura. La prueba verifica el acceso antes de guardar." wide>
    <form className="form-stack" onSubmit={event => { event.preventDefault(); if (result?.status === 'SUCCESS') save.mutate() }}>
      <fieldset className="connection-fields" disabled={save.isPending}>
        <div className="form-grid">
          <Field label="Tipo de conexión"><select disabled={!!connection} value={form.source_type} onChange={event => { const source = event.target.value as SourceType; change({ source_type: source, port: source === 'POSTGRESQL' ? 5432 : 1433, options: defaultOptions(source) }) }}><option value="POSTGRESQL">PostgreSQL</option><option value="SQLSERVER">SQL Server</option></select></Field>
          <Field label="Nombre de conexión"><input required maxLength={160} value={form.name} onChange={event => change({ name: event.target.value })}/></Field>
          <Field label="Host" hint="Dirección accesible desde el servidor de Trackvance."><input required value={form.host} onChange={event => change({ host: event.target.value })} placeholder="db.ejemplo.local"/></Field>
          <Field label="Puerto"><input required type="number" min={1} max={65535} value={form.port} onChange={event => change({ port: Number(event.target.value) })}/></Field>
          <Field label="Base de datos"><input required value={form.database} onChange={event => change({ database: event.target.value })}/></Field>
          <Field label="Usuario"><input required autoComplete="off" value={form.username} onChange={event => change({ username: event.target.value })}/></Field>
          <Field label="Contraseña" hint={connection ? endpointChanged ? 'Indícala nuevamente al cambiar host, puerto, base de datos, usuario o cifrado.' : 'Puedes dejarla vacía mientras conserves host, puerto, base de datos, usuario y cifrado.' : 'Se guarda cifrada y no se muestra nuevamente.'}><input required={!connection || endpointChanged} type="password" autoComplete="new-password" value={form.password || ''} onChange={event => change({ password: event.target.value })}/></Field>
          <Field label="Cifrado de transporte" hint="Desactívalo solamente si tu fuente de pruebas local no dispone de TLS."><select value={form.source_type === 'POSTGRESQL' ? form.options.sslmode : form.options.encryption} onChange={event => change({ options: { ...form.options, ...(form.source_type === 'POSTGRESQL' ? { sslmode: event.target.value } : { encryption: event.target.value }) } })}><option value="require">TLS requerido</option>{form.source_type === 'POSTGRESQL' && <><option value="verify-ca">TLS y verificación de certificado</option><option value="verify-full">TLS y verificación de identidad</option></>}<option value={form.source_type === 'POSTGRESQL' ? 'disable' : 'off'}>Sin TLS · pruebas locales</option></select></Field>
          <Field label="Tiempo máximo de conexión (segundos)"><input type="number" required min={1} max={15} value={form.options.connect_timeout} onChange={event => change({ options: { ...form.options, connect_timeout: Number(event.target.value) } })}/></Field>
          <Field label="Tiempo máximo de consulta (segundos)"><input type="number" required min={1} max={60} value={form.options.query_timeout} onChange={event => change({ options: { ...form.options, query_timeout: Number(event.target.value) } })}/></Field>
        </div>
      </fieldset>
      <Notice>Utiliza una cuenta con permiso de lectura sobre las tablas o vistas que quieras consultar.</Notice>
      {result && <TestResult result={result}/>}
      {test.error && <ErrorState error={test.error}/>}
      {save.error && <ErrorState error={save.error}/>}
      {!result && <p className="connection-help">Prueba la configuración actual para habilitar el guardado.</p>}
      <div className="modal-footer"><button type="button" className="button secondary" disabled={save.isPending} onClick={close}>Cancelar</button><button type="button" className="button secondary" disabled={!valid || test.isPending || save.isPending} onClick={() => test.mutate({ input: payload(), revision: revision.current })}><Plug size={16}/>{test.isPending ? 'Probando conexión…' : 'Probar conexión'}</button><button className="button primary" disabled={!valid || result?.status !== 'SUCCESS' || test.isPending || save.isPending}>{save.isPending ? 'Guardando…' : 'Guardar conexión'}</button></div>
    </form>
  </Modal>
}

export function ConnectionsPage() {
  const canRead = usePermission('connections:read'), canManage = usePermission('connections:manage')
  const [creating, setCreating] = useState(false), [search, setSearch] = useState('')
  const cache = useQueryClient(), navigate = useNavigate()
  const connections = useQuery({ queryKey: ['connections'], queryFn: () => api<{ items: ConnectionRecord[]; total: number }>('/connections'), enabled: canRead })
  if (!canRead) return <Notice>No tienes permisos para consultar las conexiones.</Notice>
  const items = (connections.data?.items || []).filter(item => `${item.name} ${item.database} ${sourceLabel(item.source_type)}`.toLocaleLowerCase().includes(search.toLocaleLowerCase()))
  return <>
    <PageHeading eyebrow="FUENTES EXTERNAS" title="Conexiones" description="Explora tus bases de datos y crea datasets con evidencia de su origen." action={<button className="button primary" disabled={!canManage} onClick={() => setCreating(true)}><Plus size={17}/> Nueva conexión</button>}/>
    <div className="module-intro connection-intro"><ShieldCheck size={25}/><div><strong>Fuentes de entrada · solo lectura</strong><p>Cada consulta importada genera una versión inmutable. Los controles de calidad trabajan sobre ese snapshot.</p></div></div>
    <section className="panel"><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar conexión o base de datos…"/><span className="muted">{number(connections.data?.total || 0)} conexiones</span></div>
      {connections.isPending ? <Loading text="Cargando conexiones…"/> : connections.error ? <ErrorState error={connections.error} retry={() => connections.refetch()}/> : !items.length ? <Empty title={search ? 'No hay coincidencias' : 'Conecta tu primera fuente'} description={search ? 'Prueba otro nombre o base de datos.' : 'Agrega PostgreSQL o SQL Server para explorar las tablas y vistas disponibles.'}/> : <div className="table-scroll"><table><thead><tr><th>Conexión</th><th>Tipo</th><th>Base de datos</th><th>Estado</th><th>Última prueba</th><th/></tr></thead><tbody>{items.map(connection => <tr key={connection.id}><td><Link className="table-primary" to={`/connections/${connection.id}`}>{connection.name}</Link><span className="table-subtitle">{connection.host}:{connection.port}</span></td><td>{sourceLabel(connection.source_type)}</td><td>{connection.database}</td><td><Badge value={connection.enabled ? 'ACTIVE' : 'INACTIVE'}/></td><td><Badge value={connection.last_test_status || 'NOT_REQUESTED'}>{connection.last_test_status === 'SUCCESS' ? 'Conectada' : connection.last_test_status === 'FAILED' ? 'Falló la prueba' : 'Sin probar'}</Badge><span className="table-subtitle">{date(connection.last_test_at)}</span></td><td><Link className="text-link" to={`/connections/${connection.id}`} aria-label={`Explorar ${connection.name}`}><ArrowRight size={17}/></Link></td></tr>)}</tbody></table></div>}
    </section>
    {creating && <ConnectionDialog close={() => setCreating(false)} saved={connection => { cache.invalidateQueries({ queryKey: ['connections'] }); setCreating(false); navigate(`/connections/${connection.id}`) }}/>}
  </>
}

function SourceBrowser({ connection }: { connection: ConnectionRecord }) {
  const canImport = usePermission('datasets:write'), cache = useQueryClient()
  const [schema, setSchema] = useState(''), [object, setObject] = useState('')
  const [name, setName] = useState(''), [domain, setDomain] = useState('Operaciones'), [description, setDescription] = useState('')
  const [created, setCreated] = useState<{ id: string; name: string } | null>(null)
  const schemas = useQuery({ queryKey: ['connection-schemas', connection.id, connection.version], queryFn: () => api<{ items: string[]; total: number }>(`/connections/${connection.id}/schemas`) })
  const objects = useQuery({ queryKey: ['connection-objects', connection.id, connection.version, schema], queryFn: () => api<{ items: SourceObject[]; total: number }>(`/connections/${connection.id}/objects?${new URLSearchParams({ schema_name: schema })}`), enabled: !!schema })
  const preview = useQuery({ queryKey: ['connection-preview', connection.id, connection.version, schema, object], queryFn: () => api<SourcePreview>(`/connections/${connection.id}/preview?${new URLSearchParams({ schema_name: schema, object_name: object, limit: '20' })}`), enabled: !!schema && !!object })
  const create = useMutation({
    mutationFn: () => post<{ dataset: { id: string; name: string }; version: { id: string } }>(`/connections/${connection.id}/datasets`, { name: name.trim(), domain: domain.trim(), description: description.trim(), schema_name: schema, object_name: object }),
    onSuccess: data => { setCreated(data.dataset); cache.invalidateQueries({ queryKey: ['datasets'] }); cache.invalidateQueries({ queryKey: ['dashboard'] }) },
  })
  return <section className="panel connection-browser"><div className="panel-heading"><div><h2>Explorar fuente</h2><p>Selecciona una tabla o vista. La vista previa consulta hasta 20 registros.</p></div><button className="button secondary small" disabled={schemas.isFetching || create.isPending} onClick={() => { schemas.refetch(); if (schema) objects.refetch(); if (object) preview.refetch() }}><RefreshCw size={14}/> Actualizar fuente</button></div>
    <div className="connection-browser-body"><div className="form-grid"><Field label="Schema"><select value={schema} disabled={schemas.isPending || create.isPending} onChange={event => { setSchema(event.target.value); setObject(''); setName(''); setCreated(null); create.reset() }}><option value="">Selecciona un schema</option>{(schemas.data?.items || []).map(item => <option key={item} value={item}>{item}</option>)}</select></Field><Field label="Tabla o vista"><select value={object} disabled={!schema || objects.isPending || create.isPending} onChange={event => { setObject(event.target.value); setName(event.target.value); setCreated(null); create.reset() }}><option value="">Selecciona una tabla o vista</option>{(objects.data?.items || []).map(item => <option key={item.name} value={item.name}>{item.name} · {item.kind === 'VIEW' ? 'Vista' : 'Tabla'}</option>)}</select></Field></div>
      {schemas.isPending && <Loading text="Explorando schemas…"/>}{schemas.error && <ErrorState error={schemas.error} retry={() => schemas.refetch()}/>}
      {schemas.data && !schemas.data.items.length && <Notice>No hay schemas accesibles para esta cuenta.</Notice>}
      {schema && objects.isPending && <Loading text="Consultando tablas y vistas…"/>}{objects.error && <ErrorState error={objects.error} retry={() => objects.refetch()}/>}
      {schema && objects.data && !objects.data.items.length && <Notice>No hay tablas o vistas accesibles en este schema.</Notice>}
      {object && preview.isPending && <Loading text="Leyendo esquema y vista previa…"/>}{preview.error && <ErrorState error={preview.error} retry={() => preview.refetch()}/>}
      {object && preview.data && <><div className="connection-preview-heading"><h3>Columnas detectadas</h3><span>{number(preview.data.columns.length)} columnas</span></div><div className="table-scroll"><table aria-label="Columnas de la fuente"><thead><tr><th>Columna</th><th>Tipo de origen</th><th>Tipo en Trackvance</th><th>Permite nulos</th></tr></thead><tbody>{preview.data.columns.map(column => <tr key={column.name}><td className="mono">{column.name}</td><td>{column.native_type}</td><td><span className="type-pill">{column.logical_type}</span></td><td>{column.nullable ? 'Sí' : 'No'}</td></tr>)}</tbody></table></div>
        <div className="connection-preview-heading"><h3>Vista previa</h3><span>Muestra limitada · {number(preview.data.sampled_rows)} registros</span></div>{preview.data.rows.length ? <div className="table-scroll"><table aria-label="Vista previa de la fuente"><thead><tr>{preview.data.columns.map(column => <th key={column.name}>{column.name}</th>)}</tr></thead><tbody>{preview.data.rows.map((row, index) => <tr key={index}>{preview.data!.columns.map(column => <td key={column.name}><SampleValue value={typeof row[column.name] === 'object' && row[column.name] != null ? JSON.stringify(row[column.name]) : row[column.name]}/></td>)}</tr>)}</tbody></table></div> : <Notice>La fuente está vacía. Su esquema está disponible para registrar el dataset.</Notice>}
        {created ? <div className="connection-created"><Notice success>Dataset «{created.name}» creado. Su primera versión conserva la fuente y el snapshot Parquet.</Notice><div className="connection-actions"><Link className="button secondary" to={`/datasets/${created.id}`}>Ver dataset</Link><Link className="button primary" to="/intake">Ir a Data Intake <ArrowRight size={16}/></Link></div></div> : <form className="connection-import form-stack" onSubmit={event => { event.preventDefault(); create.mutate() }}><h3>Registrar como Dataset</h3><div className="form-grid"><Field label="Nombre del dataset"><input required maxLength={160} disabled={create.isPending || !canImport} value={name} onChange={event => setName(event.target.value)}/></Field><Field label="Área de negocio"><input required maxLength={80} list="connection-dataset-domains" disabled={create.isPending || !canImport} value={domain} onChange={event => setDomain(event.target.value)}/></Field></div><datalist id="connection-dataset-domains"><option value="Operaciones"/><option value="Finanzas"/><option value="Logística"/><option value="Ventas"/></datalist><Field label="Descripción (opcional)"><input maxLength={2000} disabled={create.isPending || !canImport} value={description} onChange={event => setDescription(event.target.value)}/></Field><Notice>Al crear el dataset se consulta la fuente completa dentro de los límites configurados y se conserva un snapshot inmutable. Podrás consultar de nuevo la fuente para crear versiones posteriores.</Notice>{!canImport && <Notice>Necesitas permiso para crear datasets.</Notice>}{create.error && <ErrorState error={create.error}/>}<button className="button primary" disabled={!canImport || create.isPending || !name.trim() || !domain.trim()}><Database size={16}/>{create.isPending ? 'Creando snapshot…' : 'Crear dataset'}</button></form>}
      </>}
    </div>
  </section>
}

export function ConnectionDetail() {
  const { id } = useParams(), cache = useQueryClient(), navigate = useNavigate()
  const canRead = usePermission('connections:read'), canUse = usePermission('connections:use'), canManage = usePermission('connections:manage')
  const [editing, setEditing] = useState(false), [confirm, setConfirm] = useState<'toggle' | 'delete' | null>(null)
  const connection = useQuery({ queryKey: ['connection', id], queryFn: () => api<ConnectionRecord>(`/connections/${id}`), enabled: canRead && !!id })
  function invalidate() { cache.invalidateQueries({ queryKey: ['connections'] }); cache.invalidateQueries({ queryKey: ['connection', id] }) }
  const test = useMutation({ mutationFn: () => post<ConnectionTest>(`/connections/${id}/test`), onSettled: invalidate })
  const change = useMutation({ mutationFn: () => confirm === 'delete' ? api(`/connections/${id}?version=${connection.data!.version}`, { method: 'DELETE' }) : api(`/connections/${id}`, { method: 'PATCH', body: JSON.stringify({ version: connection.data!.version, enabled: !connection.data!.enabled }) }), onSuccess: () => { invalidate(); if (confirm === 'delete') navigate('/connections'); setConfirm(null) } })
  if (!canRead) return <Notice>No tienes permisos para consultar esta conexión.</Notice>
  if (connection.isPending) return <Loading text="Cargando conexión…"/>
  if (connection.error) return <ErrorState error={connection.error} retry={() => connection.refetch()}/>
  const data = connection.data
  return <><PageHeading back="/connections" eyebrow="CONEXIÓN EXTERNA" title={data.name} description={`${sourceLabel(data.source_type)} · ${data.database}`} action={<><button className="button secondary" disabled={!canManage} onClick={() => setEditing(true)}><Pencil size={15}/> Editar conexión</button><button className="button primary" disabled={!canUse || !data.enabled || test.isPending} onClick={() => test.mutate()}><Plug size={16}/>{test.isPending ? 'Probando…' : 'Probar conexión'}</button></>}/>
    <section className="panel connection-details"><div className="connection-summary"><div><span>Servidor</span><strong>{data.host}:{data.port}</strong></div><div><span>Cuenta de lectura</span><strong>{data.username}</strong></div><div><span>Configuración</span><strong>Versión {data.version}</strong></div><div><span>Estado</span><Badge value={data.enabled ? 'ACTIVE' : 'INACTIVE'}/></div><div><span>Última prueba</span><strong>{date(data.last_test_at)}</strong><Badge value={data.last_test_status || 'NOT_REQUESTED'}>{data.last_test_status === 'SUCCESS' ? 'Conectada' : data.last_test_status === 'FAILED' ? 'Falló la prueba' : 'Sin probar'}</Badge></div></div>
      {test.data ? <TestResult result={test.data}/> : data.last_test_message && <p className="connection-help">{data.last_test_message}</p>}{test.error && <ErrorState error={test.error}/>}
      <div className="connection-actions"><button className="text-button" disabled={!canManage || change.isPending} onClick={() => { change.reset(); setConfirm('toggle') }}>{data.enabled ? 'Deshabilitar conexión' : 'Habilitar conexión'}</button><button className="text-button connection-delete" disabled={!canManage || change.isPending} onClick={() => { change.reset(); setConfirm('delete') }}><Trash2 size={14}/> Eliminar conexión</button></div>
    </section>
    {!data.enabled ? <Notice>La conexión está deshabilitada. Los snapshots existentes continúan disponibles; habilítala para consultar nuevos datos.</Notice> : !canUse ? <Notice>No tienes permiso para explorar esta fuente.</Notice> : <SourceBrowser key={`${data.id}-${data.version}`} connection={data}/>}
    {editing && <ConnectionDialog connection={data} close={() => setEditing(false)} saved={() => { invalidate(); test.reset(); setEditing(false) }}/>}
    {confirm && <Modal open onOpenChange={open => { if (!open && !change.isPending) setConfirm(null) }} title={confirm === 'delete' ? 'Eliminar conexión' : data.enabled ? 'Deshabilitar conexión' : 'Habilitar conexión'} description={confirm === 'delete' ? 'La conexión dejará de estar disponible para nuevas consultas. Sus configuraciones históricas, datasets y snapshots se conservarán.' : data.enabled ? 'Se suspenderán las nuevas consultas a esta fuente. Las versiones y ejecuciones históricas se conservan.' : 'Se volverá a permitir consultar la fuente con la configuración guardada.'}><p>Conexión: <strong>{data.name}</strong></p>{change.error && <ErrorState error={change.error}/>}<div className="modal-footer"><button className="button secondary" disabled={change.isPending} onClick={() => setConfirm(null)}>Cancelar</button><button className="button primary" disabled={change.isPending} onClick={() => change.mutate()}>{change.isPending ? 'Aplicando…' : confirm === 'delete' ? 'Confirmar eliminación' : 'Confirmar cambio'}</button></div></Modal>}
  </>
}
