import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client'
import { usePermission, useSession } from '../../app/session'
import { date, ErrorState, Field, Loading, Notice } from '../../components/ui'
import { SourceRefresh } from './SourceRefresh'

type Settings = { connection_id: string; schema_name: string; object_name: string; column_overrides: Record<string, unknown>; responsible_user_id: string; interval_seconds: number; timezone: string; enabled: boolean; publication_policy: 'ON_CHANGE' | 'EVERY_CUT'; empty_policy: 'BLOCK' | 'PRESERVE' }
type State = { dataset_id: string; version: number | null; settings: Settings | null; binding: Pick<Settings, 'connection_id' | 'schema_name' | 'object_name' | 'column_overrides'> | null; enabled: boolean; next_run_at: string | null; last_attempt_at: string | null; last_query_success_at: string | null; last_publication_at: string | null }

function SourceForm({ state, datasetId, saved }: { state: State; datasetId: string; saved: () => void }) {
  const session = useSession(), canWrite = usePermission('datasets:write'), canUse = usePermission('connections:use')
  const [form, setForm] = useState<Settings>(state.settings || { connection_id: state.binding?.connection_id || '', schema_name: state.binding?.schema_name || 'REST', object_name: state.binding?.object_name || 'collection', column_overrides: state.binding?.column_overrides || {}, responsible_user_id: session?.user.id || '', interval_seconds: 86400, timezone: 'America/Bogota', enabled: false, publication_policy: 'ON_CHANGE', empty_policy: 'BLOCK' })
  const connections = useQuery({ queryKey: ['connections'], queryFn: () => api<{ items: { id: string; name: string; source_type: string }[] }>('/connections'), enabled: canUse })
  const users = useQuery({ queryKey: ['acquisition-responsibles'], queryFn: () => api<{ items: { id: string; name: string }[] }>('/users'), enabled: usePermission('users:read') })
  const save = useMutation({ mutationFn: () => api<State>(`/datasets/${datasetId}/source-settings`, { method: 'PUT', body: JSON.stringify({ ...form, expected_version: state.version }) }), onSuccess: saved })
  const update = (patch: Partial<Settings>) => setForm(value => ({ ...value, ...patch }))
  return <form className="form-stack" onSubmit={event => { event.preventDefault(); save.mutate() }}><fieldset disabled={!canWrite || !canUse || save.isPending}><div className="form-grid">
    <Field label="Conexión"><select required value={form.connection_id} onChange={event => { const connection = connections.data?.items.find(item => item.id === event.target.value); update({ connection_id: event.target.value, schema_name: connection?.source_type === 'REST' ? 'REST' : '', object_name: connection?.source_type === 'REST' ? 'collection' : '' }) }}><option value="">Selecciona conexión</option>{connections.data?.items.map(item => <option key={item.id} value={item.id}>{item.name} · {item.source_type}</option>)}</select></Field>
    <Field label="Schema / espacio de datos"><input required maxLength={128} value={form.schema_name} onChange={event => update({ schema_name: event.target.value })}/></Field><Field label="Tabla, vista o colección"><input required maxLength={128} value={form.object_name} onChange={event => update({ object_name: event.target.value })}/></Field>
    <Field label="Responsable"><select required value={form.responsible_user_id} onChange={event => update({ responsible_user_id: event.target.value })}>{!users.data?.items.some(item => item.id === form.responsible_user_id) && <option value={form.responsible_user_id}>{form.responsible_user_id === session?.user.id ? session?.user.name : form.responsible_user_id}</option>}{users.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field>
    <Field label="Frecuencia (segundos)"><input type="number" required min={60} max={2678400} value={form.interval_seconds} onChange={event => update({ interval_seconds: Number(event.target.value) })}/></Field><Field label="Zona horaria IANA"><input required value={form.timezone} onChange={event => update({ timezone: event.target.value })}/></Field>
    <Field label="Publicación"><select value={form.publication_policy} onChange={event => update({ publication_policy: event.target.value as Settings['publication_policy'] })}><option value="ON_CHANGE">Publicar solo con cambios</option><option value="EVERY_CUT">Publicar cada corte</option></select></Field>
    <Field label="Resultado vacío"><select value={form.empty_policy} onChange={event => update({ empty_policy: event.target.value as Settings['empty_policy'] })}><option value="BLOCK">Bloquear y solicitar revisión</option><option value="PRESERVE">Registrar consulta y conservar versión vigente</option></select></Field>
  </div><label><input type="checkbox" checked={form.enabled} onChange={event => update({ enabled: event.target.checked })}/> Activar actualizaciones programadas</label></fieldset>
    <Notice>Adquiere fotografías completas. Los fallos y esquemas incompatibles conservan las versiones publicadas. Un resultado vacío no vacía destinos. Cada ejecución congela su configuración; la pausa afecta nuevas ocurrencias.</Notice>
    {save.error && <ErrorState error={save.error}/>}<div><button className="button secondary" disabled={!canWrite || !canUse || save.isPending}>{save.isPending ? 'Guardando…' : 'Guardar origen y actualización'}</button></div>
  </form>
}

export function DatasetSourceSettings({ datasetId, onRefreshed }: { datasetId: string; onRefreshed: (versionId: string) => void }) {
  const cache = useQueryClient()
  const state = useQuery({ queryKey: ['dataset-source-settings', datasetId], queryFn: () => api<State>(`/datasets/${datasetId}/source-settings`), refetchInterval: 15000 })
  return <section className="panel"><div className="panel-heading"><div><h2>Origen y actualización</h2><p>Conexión, lectura completa y publicación del dataset.</p></div></div><div className="connection-browser-body">
    {state.isPending ? <Loading/> : state.error ? <ErrorState error={state.error} retry={() => state.refetch()}/> : <><dl className="detail-summary"><div><dt>Último intento</dt><dd>{date(state.data.last_attempt_at)}</dd></div><div><dt>Última consulta exitosa</dt><dd>{date(state.data.last_query_success_at)}</dd></div><div><dt>Última publicación</dt><dd>{date(state.data.last_publication_at)}</dd></div><div><dt>Próxima ejecución</dt><dd>{state.data.enabled ? date(state.data.next_run_at) : 'En pausa'}</dd></div></dl>
      <SourceForm key={`${datasetId}:${state.data.version}`} state={state.data} datasetId={datasetId} saved={() => { cache.invalidateQueries({ queryKey: ['dataset-source-settings', datasetId] }); cache.invalidateQueries({ queryKey: ['dataset', datasetId] }) }}/>
      {state.data.binding && <SourceRefresh datasetId={datasetId} connectionId={state.data.binding.connection_id} onRefreshed={id => { state.refetch(); onRefreshed(id) }}/>}</>}
  </div></section>
}
