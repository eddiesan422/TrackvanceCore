import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, ErrorState, Field, Loading, Notice } from '../../components/ui'
import { extractionStrategies } from './documentReview'

export default function DocumentEngineSettings({ familyId }: { familyId?: string }) {
  const [savedVersion, setSavedVersion] = useState<number | null>(null)
  const endpoint = `/documents/engines${familyId ? `?family_id=${encodeURIComponent(familyId)}` : ''}`
  const query = useQuery({ queryKey: ['document-engines', familyId || 'organization'], queryFn: () => api(endpoint), refetchInterval: 15000 })
  const configure = usePermission('documents:configure')
  if (query.isLoading) return <Loading text="Consultando motores documentales…"/>
  if (query.error || !query.data) return <ErrorState error={query.error} retry={() => void query.refetch()}/>
  return <><EngineConfiguration key={`${endpoint}:${query.data.version}`} data={query.data} endpoint={endpoint} editable={configure && query.data.can_configure} refresh={() => query.refetch()} onSaved={setSavedVersion}/>{savedVersion != null && <Notice>Configuración {savedVersion} guardada. Los trabajos anteriores conservan su motor y parámetros.</Notice>}</>
}

function EngineConfiguration({ data, endpoint, editable, refresh, onSaved }: { data: RecordData; endpoint: string; editable: boolean; refresh: () => Promise<unknown>; onSaved: (version: number) => void }) {
  const [configuration, setConfiguration] = useState<RecordData>(data.configuration)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [contextEdited, setContextEdited] = useState(false)
  const [visualContextPrepared, setVisualContextPrepared] = useState(false)
  const providers: RecordData[] = data.providers || []
  const selected = providers.find(provider => provider.id === configuration.provider)
  const localDisabled = !editable || busy || configuration.provider !== 'PADDLE_QWEN_LOCAL'
  const change = (name: string, value: unknown) => {
    if (name === 'context_tokens') { setContextEdited(true); setVisualContextPrepared(false) }
    setConfiguration({ ...configuration, [name]: value })
  }
  const changeStrategy = (strategy: string) => {
    const visual = ['NATIVE_FIRST', 'QWEN_VISUAL', 'SELECTIVE'].includes(strategy)
    const prepare = visual && !contextEdited && configuration.context_tokens === 8192
    const undo = strategy === 'LEGACY' && visualContextPrepared && !contextEdited
    setConfiguration({ ...configuration, extraction_strategy: strategy, ...(prepare ? { context_tokens: 32768 } : undo ? { context_tokens: 8192 } : {}) })
    setVisualContextPrepared(prepare || visualContextPrepared && !undo)
  }
  return <section className="panel document-engine-settings" aria-labelledby="document-engines-title">
    <h2 id="document-engines-title">Motor y disponibilidad</h2>
    <p>Ámbito: {data.scope === 'FAMILY' ? 'esta familia' : 'organización'} · configuración {data.version}. Cada trabajo conserva la configuración con la que comenzó.</p>
    {data.inherited_version != null && <Notice>Esta familia hereda la configuración {data.inherited_version} de la organización. Guardar crea una configuración específica para sus trabajos nuevos.</Notice>}
    <div className="document-provider-grid">{providers.map(provider => <article key={provider.id}><h3>{provider.name || provider.id}</h3><Badge value={provider.status}>{provider.available && provider.execution_enabled ? 'Disponible para ejecución' : provider.id === 'AWS_TEXTRACT' || provider.status === 'PREPARED' ? 'Preparado para habilitación futura' : 'Ejecución no disponible'}</Badge><p>{provider.model}</p>{provider.reason && <p>{provider.reason}</p>}{provider.id === 'AWS_TEXTRACT' && <small>Las llamadas reales a AWS permanecen bloqueadas en esta versión.</small>}</article>)}</div>
    {!editable && <Notice>La configuración requiere permisos documentales y rol de administrador.</Notice>}
    <p id="document-engine-parameter-scope">Tiempo, contexto, reintentos, concurrencia e interpretación semántica son parámetros de Paddle/Qwen local. Tesseract conserva su límite nativo de 120 segundos.</p>
    <form onSubmit={async event => { event.preventDefault(); setBusy(true); setError(null); try { const result = await api(endpoint, { method: 'PUT', body: JSON.stringify({ expected_version: data.version, configuration, reason: reason.trim() }) }); setReason(''); onSaved(result.version); await refresh() } catch (error) { setError(error) } finally { setBusy(false) } }}>
      <div className="document-engine-fields">
        <Field label="Proveedor documental"><select disabled={!editable || busy} value={configuration.provider} onChange={event => change('provider', event.target.value)}>{providers.map(provider => <option value={provider.id} key={provider.id} disabled={!provider.execution_enabled}>{provider.name || provider.id}{!provider.execution_enabled ? ' · ejecución bloqueada' : ''}</option>)}</select></Field>
        <Field label="Estrategia de extracción" hint="Elegir una estrategia afecta sólo trabajos nuevos. Las familias históricas conservan su configuración."><select disabled={localDisabled} value={configuration.extraction_strategy || 'LEGACY'} onChange={event => changeStrategy(event.target.value)}>{Object.entries(extractionStrategies).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
        <Field label="Modelo visual"><input readOnly value={configuration.paddle_model || 'No aplica'}/></Field>
        <Field label="Modelo semántico"><input readOnly value={configuration.llm_model || 'No aplica'}/></Field>
        <Field label="Versión del prompt"><input readOnly value={configuration.prompt_version || 'No aplica'}/></Field>
        <Field label="Tiempo máximo (segundos)"><input type="number" min={30} max={1800} required aria-describedby="document-engine-parameter-scope" disabled={localDisabled} value={configuration.timeout_seconds} onChange={event => change('timeout_seconds', Number(event.target.value))}/></Field>
        <Field label="Contexto máximo (tokens)"><input type="number" min={1024} max={32768} required aria-describedby="document-engine-parameter-scope" disabled={localDisabled} value={configuration.context_tokens} onChange={event => change('context_tokens', Number(event.target.value))}/></Field>
        <Field label="Reintentos máximos"><input type="number" min={0} max={2} required aria-describedby="document-engine-parameter-scope" disabled={localDisabled} value={configuration.max_retries} onChange={event => change('max_retries', Number(event.target.value))}/></Field>
        <Field label="Trabajos simultáneos"><input type="number" min={1} max={4} required aria-describedby="document-engine-parameter-scope" disabled={localDisabled} value={configuration.max_parallel_jobs} onChange={event => change('max_parallel_jobs', Number(event.target.value))}/></Field>
      </div>
      {visualContextPrepared && !contextEdited && <Notice>Se preparó un contexto de 32768 tokens para las imágenes y el esquema de Qwen visual. Puedes modificarlo antes de guardar; la configuración anterior permanece vigente hasta entonces.</Notice>}
      {configuration.provider === 'PADDLE_QWEN_LOCAL' && !configuration.extraction_strategy && <Notice>La estrategia histórica sigue vigente. Para las nuevas pruebas puedes elegir lectura nativa primero y Qwen visual para páginas que necesiten reconocimiento.</Notice>}
      <label><input type="checkbox" aria-describedby="document-engine-parameter-scope" disabled={localDisabled} checked={!!configuration.use_llm} onChange={event => change('use_llm', event.target.checked)}/> Interpretación semántica cuando sea necesaria</label>
      {editable && <><Field label="Motivo del cambio de motor"><textarea required maxLength={2000} value={reason} onChange={event => setReason(event.target.value)}/></Field><button type="submit" className="button primary" disabled={busy || !reason.trim() || !selected?.execution_enabled}>Guardar configuración auditada</button></>}
    </form>
    {!!error && <ErrorState error={error}/>}
  </section>
}
