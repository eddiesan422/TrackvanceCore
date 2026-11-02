import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { api, post } from '../../api/client'
import type { RecordData } from '../../api/client'
import { ErrorState, Field, Loading, Notice } from '../../components/ui'
import SemanticProfileEditor, { semanticFieldTypes } from './SemanticProfileEditor'
import type { SemanticProfileDraft } from './SemanticProfileEditor'

type SemanticProfile = SemanticProfileDraft & { sha256: string; automatic_business_controls?: {
  profile_sha256: string; controls: RecordData; controls_sha256: string; explicit_scope_confirmation_required: boolean
} | null }
type ProfileAvailability = { enabled: boolean; mode: string; default_enabled: boolean; profiles: SemanticProfile[]; automatic_verification_enabled?: boolean; automatic_verification_default?: string; automatic_verification_version?: string }
const profileKey = (profile: Pick<SemanticProfile, 'id' | 'revision'>) => JSON.stringify([profile.id, profile.revision])

export default function DocumentSemanticProfile({ familyId, current, canConfigure, refresh }: {
  familyId: string; current?: RecordData; canConfigure: boolean; refresh: () => void
}) {
  const profiles = useQuery({ queryKey: ['document-semantic-profiles'], queryFn: () => api<ProfileAvailability>('/documents/semantic-profiles') })
  const [selected, setSelected] = useState('')
  const [route, setRoute] = useState('QWEN_VISUAL')
  const [automaticVerification, setAutomaticVerification] = useState('OFF')
  const [businessScopeConfirmed, setBusinessScopeConfirmed] = useState(false)
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [saved, setSaved] = useState<RecordData | null>(null)
  const [custom, setCustom] = useState<SemanticProfileDraft | null>(null)
  const profile = profiles.data?.profiles.find(item => profileKey(item) === selected)
  const automaticVersion = profiles.data?.automatic_verification_version === 'SEMANTIC_AUTO_V2' ? 'SEMANTIC_AUTO_V2' : 'SEMANTIC_AUTO_V1'
  const automatic = profiles.data?.automatic_verification_enabled === true && automaticVerification === automaticVersion
  const businessProposal = !custom && profile?.automatic_business_controls?.profile_sha256 === profile?.sha256 && profile?.automatic_business_controls?.explicit_scope_confirmation_required === true ? profile.automatic_business_controls : null
  const businessConfirmationPending = automatic && !!businessProposal && !businessScopeConfirmed
  const disabled = !canConfigure || !profiles.data?.enabled || busy
  const revisionConflict = !!error && typeof error === 'object' && 'code' in error && error.code === 'DOCUMENT_SEMANTIC_PROFILE_REVISION_CONFLICT'

  return <section className="panel document-semantic-profile" aria-labelledby="document-semantic-profile-title">
    <h2 id="document-semantic-profile-title">Extracción general</h2>
    <p>Define la información que necesitas sin marcar posiciones ni preparar una plantilla visual por proveedor.</p>
    {current && <p>Perfil vigente: <strong>{current.profile_name || current.profile_id}</strong> · revisión {current.profile_revision}. Los documentos anteriores conservan su contrato.</p>}
    {current && <p>Política vigente: {['SEMANTIC_AUTO_V1', 'SEMANTIC_AUTO_V2'].includes(current.automatic_verification) ? 'verificación automática documental optativa' : 'revisión autorizada'}. Cambiar el perfil sólo afecta a documentos nuevos.</p>}
    {profiles.isLoading && <Loading text="Consultando perfiles semánticos…"/>}
    {profiles.error && <ErrorState error={profiles.error} retry={() => void profiles.refetch()}/>}
    {profiles.data && <>
      {!profiles.data.enabled && <Notice>La extracción general está deshabilitada en este entorno. Un administrador debe habilitarla antes de configurar nuevos documentos. Las plantillas existentes continúan disponibles.</Notice>}
      {!canConfigure && <Notice>Seleccionar un perfil para esta familia requiere permiso de configuración documental.</Notice>}
      <form onSubmit={async event => {
        event.preventDefault()
        if (disabled || !profile || !reason.trim() || businessConfirmationPending) return
        setBusy(true); setError(null); setSaved(null)
        try {
          const result = await post(`/documents/families/${encodeURIComponent(familyId)}/semantic-profile`, { ...(custom ? { profile: custom } : { profile_id: profile.id, profile_revision: profile.revision }), route, reason: reason.trim(), ...(profiles.data?.automatic_verification_enabled ? { automatic_verification: automaticVerification } : {}), ...(automatic && businessProposal && businessScopeConfirmed ? { business_controls: businessProposal.controls } : {}) })
          setSaved(result.profile || { id: custom?.id || profile.id, revision: custom?.revision || profile.revision }); setReason(''); refresh()
        } catch (failure) { setError(failure) } finally { setBusy(false) }
      }}>
        <div className="document-engine-fields">
          <Field label="Perfil semántico"><select value={selected} disabled={disabled} required onChange={event => { setSelected(event.target.value); setCustom(null); setSaved(null); setBusinessScopeConfirmed(false) }}>
            <option value="">Selecciona la información que necesitas</option>{profiles.data.profiles.map(item => <option key={profileKey(item)} value={profileKey(item)}>{item.name} · revisión {item.revision}</option>)}
          </select></Field>
          <Field label="Lectura del documento" hint="Se ejecuta la ruta seleccionada; no se exige ejecutar ambos motores."><select value={route} disabled={disabled} onChange={event => { setRoute(event.target.value); setSaved(null) }}>
            <option value="QWEN_VISUAL">Qwen visual independiente</option>
            <option value="NATIVE_FIRST">Lectura digital nativa primero</option>
            <option value="PADDLE_STRUCTURED">Paddle estructural</option>
          </select></Field>
          {profiles.data.automatic_verification_enabled && <Field label="Verificación automática documental" hint="Optativa para documentos nuevos; sólo aprueba cuando la evidencia y todos los controles sean suficientes."><select value={automaticVerification} disabled={disabled} onChange={event => { setAutomaticVerification(event.target.value); setSaved(null); setBusinessScopeConfirmed(false) }}>
            <option value="OFF">Deshabilitada: revisión autorizada</option>
            <option value={automaticVersion}>Verificar evidencia automáticamente; revisar excepciones</option>
          </select></Field>}
        </div>
        {profile && <><label className="document-semantic-customize"><input type="checkbox" disabled={disabled} checked={!!custom} onChange={event => {
          setSaved(null); setBusinessScopeConfirmed(false)
          setCustom(event.target.checked ? { schema_version: 'SEMANTIC_GENERAL_V1', id: `custom-${crypto.randomUUID()}`, name: `${profile.name} personalizada`.slice(0, 140), revision: 1, document_type: profile.document_type,
            description: profile.description, fields: profile.fields.map(field => ({ ...field })), tables: profile.tables.map(table => ({ ...table, fields: table.fields.map((field: RecordData) => ({ ...field })) })) } : null)
        }}/> Personalizar información solicitada</label>
        {custom ? <SemanticProfileEditor value={custom} onChange={value => {
          const savedRevision = saved?.id === value.id ? Number(saved.revision) : null
          setCustom({ ...value, revision: savedRevision != null && value.revision <= savedRevision ? savedRevision + 1 : value.revision }); setSaved(null)
        }} disabled={disabled}/> : <div className="document-semantic-definition" aria-label="Información solicitada por el perfil">
          <h3>{profile.name}</h3>{profile.description && <p>{profile.description}</p>}
          {!!profile.fields.length && <><h4>Campos del documento</h4><ul>{profile.fields.map(field => <li key={field.name}><strong>{field.label || field.name}</strong> · {semanticFieldTypes[field.logical_type] || field.logical_type} · {field.required ? 'Obligatorio' : 'Opcional'}{field.description && <small>{field.description}</small>}</li>)}</ul></>}
          {!!profile.tables.length && <><h4>Tablas y colecciones solicitadas</h4>{profile.tables.map(table => <div key={table.name}><strong>{table.name}</strong> · {table.required ? 'Obligatoria' : 'Opcional'}<p>{(table.fields || []).map((column: RecordData) => `${column.label || column.name} (${semanticFieldTypes[column.logical_type] || column.logical_type})`).join(' · ')}</p></div>)}</>}
          <details><summary>Versión y procedencia del perfil</summary><p>{profile.id} · revisión {profile.revision}</p><small>SHA-256: {profile.sha256}</small></details>
        </div>}</>}
        {automatic && businessProposal && <section aria-label="Coherencia de la factura completa">
          <p>Se comprobará que los conceptos del detalle coincidan con el subtotal y que subtotal, impuestos y total sean coherentes. La evidencia y la cobertura documental siguen siendo obligatorias.</p>
          <label><input type="checkbox" disabled={disabled} checked={businessScopeConfirmed} onChange={event => setBusinessScopeConfirmed(event.target.checked)}/> Confirmo que este perfil representa la factura completa, sin descuentos ni ajustes que queden fuera de sus campos.</label>
          <p>Si hay descuentos o ajustes no representados, conserva la revisión de esas facturas.</p>
        </section>}
        {automatic && (custom?.document_type || profile?.document_type) === 'INVOICE' && !businessProposal && <Notice>Este perfil no tiene una declaración completa de controles de factura. Sus facturas conservarán revisión; seleccionar la verificación automática no sustituye esos controles.</Notice>}
        <Notice>Los resultados conservarán el texto observado, las alternativas y su origen. {automatic ? 'El modo seleccionado verificará la evidencia y mantendrá en revisión las excepciones no resueltas;' : 'La extracción general requiere revisión;'} guardar este perfil no aprueba ni publica documentos.</Notice>
        {canConfigure && <><Field label="Motivo del cambio de perfil"><textarea value={reason} required maxLength={2000} disabled={disabled} onChange={event => setReason(event.target.value)}/></Field><button className="button primary" type="submit" disabled={disabled || !profile || !reason.trim() || businessConfirmationPending}>Usar perfil para documentos nuevos</button></>}
      </form>
    </>}
    {saved && <Notice success>Perfil {saved.id || selected} guardado, revisión {saved.revision}. Ya puedes cargar documentos en esta familia; sus resultados pasarán por los controles y la revisión existentes.</Notice>}
    {revisionConflict && <Notice>Incrementa la revisión del perfil personalizado para guardar el cambio. El contrato y los documentos de la revisión anterior permanecen conservados.</Notice>}
    {!!error && <ErrorState error={error}/>}
  </section>
}
