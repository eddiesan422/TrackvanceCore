import type { RecordData } from '../../api/client'
import { evidenceReference, semanticReasonDescription } from './documentSemanticEvidence'
import type { PrintedPagination, SemanticDocumentReview, SemanticSourceChecks } from './documentSemanticEvidence'

const statuses: Record<string, string> = { QUEUED: 'Verificación en cola', VERIFYING: 'Comprobando la extracción', CONTROLS_COMPLETED: 'Controles adicionales completados', REVIEW_REQUIRED: 'Verificación terminada con excepciones', BLOCKED: 'Verificación adicional detenida' }
const reasons: Record<string, string> = {
  VALIDATION_POLICY_REQUIRED: 'Falta declarar la política de validación de esta plantilla antes de solicitar otra lectura',
  INDEPENDENT_SOURCE_REQUIRED: 'Falta corroborar el valor con un mecanismo distinto',
  INDEPENDENT_ROW_STRUCTURE_REQUIRED: 'Falta comprobar la estructura y continuidad de la tabla',
  UNRESOLVED_VALUE: 'El valor todavía está ausente, es ambiguo o tiene un formato inválido',
  TABLE_INCOMPLETE_OR_UNASSOCIATED: 'La tabla tiene regiones o filas sin interpretar',
  STRUCTURE_OR_SEMANTIC_CONFLICT: 'Persisten observaciones contradictorias o problemas de estructura',
  DECLARED_ARITHMETIC_CONSISTENCY: 'Falta resolver una regla aritmética declarada',
  SOURCE_VALUE_BINDING: 'El valor no conserva una asociación demostrable con su fuente',
  RETAINED_SOURCE_INTEGRITY: 'La evidencia conservada necesita comprobar su integridad',
  ARITHMETIC_CONTROL_UNDECLARED: 'Falta declarar el control aritmético del importe',
  DECLARED_TABLE_STRUCTURE_REQUIRED: 'Falta una regla declarada que permita comprobar las filas de la tabla',
  OPTIONAL_TABLE_ABSENCE_UNVERIFIED: 'La ausencia de la tabla opcional todavía no está comprobada',
}
const actions: Record<string, string> = {
  QWEN_DISABLED_IN_FROZEN_CONFIGURATION: 'La configuración congelada no permite la lectura semántica de control. Administración puede habilitarla para un nuevo intento; este resultado conserva su configuración.',
  PRIOR_RESOURCES_NOT_RELEASED: 'Debe comprobarse el cierre de la lectura anterior antes de admitir otra. Conserva el resultado y consulta el estado del servicio.',
  PRIMARY_RECOGNITION_INCOMPLETE: 'El reconocimiento está incompleto. Comprueba las páginas y regiones pendientes antes de publicar.',
  INSUFFICIENT_FROZEN_ATTEMPT_BUDGET: 'El tiempo reservado no alcanza para otra lectura. Revisa las excepciones o prepara un nuevo intento con un límite explícito.',
  INDEPENDENT_MECHANISMS_ALREADY_ATTEMPTED_OR_SOURCE_UNVERIFIED: 'Las lecturas disponibles no permiten resolver todos los controles. Revisa las excepciones contra el original; no se repetirá automáticamente la misma lectura.',
  DECLARED_CONTROLS_OR_SOURCE_BINDING_REQUIRED: 'Revisa la configuración declarada o la asociación de la evidencia con el original. Otra llamada al modelo no sustituye estos controles.',
}
function target(path: string) {
  const parts = path.split('.')
  return parts.length === 3 ? `${parts[0] === 'headers' ? 'Cabecera' : `${parts[0]} · fila ${Number(parts[1]) + 1}`} · ${parts[2]}` : path
}

function SourceChecks({ checks }: { checks: SemanticSourceChecks }) {
  const descriptions: Record<string, string> = { VERIFIED: 'Comprobada', UNVERIFIED: 'Pendiente de comprobar', AVAILABLE: 'Disponible', PARTIAL: 'Parcial', UNAVAILABLE: 'No disponible', COMPLETE: 'Procesadas', INCOMPLETE: 'Incompleta', PENDING: 'Pendiente', CONFLICT: 'Inconsistente', ELIGIBLE: 'Elegible según el validador', BLOCKED: 'No acreditada para aprobación automática' }
  const status = (value?: string) => descriptions[value || ''] || 'Sin resultado verificable'
  const pages = (values?: number[]) => values ? values.length ? values.join(', ') : 'ninguna' : 'sin dato'
  const names = (values?: string[]) => values ? values.length ? values.join(', ') : 'ninguna' : 'sin dato'
  const count = (value?: number) => typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : 'sin dato'
  const refs = [...new Set([...(checks.integrity?.source_refs || []), ...(checks.text?.source_refs || [])].map(evidenceReference))]
  return <div className="document-source-checks">
    <dl aria-label="Controles de fuente y completitud">
      {checks.integrity && <div><dt>Integridad de la fuente</dt><dd>{status(checks.integrity.status)}</dd></div>}
      {checks.text && <div><dt>Texto de fuente</dt><dd>{status(checks.text.status)} · páginas con texto: {pages(checks.text.available_pages)} · sin texto admitido: {pages(checks.text.missing_pages)}</dd></div>}
      {checks.execution_pages && <div><dt>Páginas del archivo</dt><dd>Recibidas: {pages(checks.execution_pages.physical_pages)} · procesadas: {pages(checks.execution_pages.processed_pages)} · pendientes de lectura: {pages(checks.execution_pages.missing_pages)}</dd></div>}
      {checks.associations && <div><dt>Asociaciones campo–valor</dt><dd>{status(checks.associations.status)} · acreditadas: {count(checks.associations.verified)} · pendientes: {count(checks.associations.pending)}</dd></div>}
      {checks.cell_corroboration && <div><dt>Corroboración de celdas</dt><dd>{status(checks.cell_corroboration.status)} · acreditadas: {count(checks.cell_corroboration.verified)} · pendientes: {count(checks.cell_corroboration.pending)}</dd></div>}
      {checks.table_coverage && <div><dt>Cobertura de tablas</dt><dd>{status(checks.table_coverage.status)} · acreditadas: {names(checks.table_coverage.verified)} · pendientes: {names(checks.table_coverage.pending)}</dd></div>}
      {checks.documentary_completeness && <div><dt>Completitud documental</dt><dd>{status(checks.documentary_completeness.status)}{checks.documentary_completeness.scope === 'DECLARED_EXTRACTION_CONTRACT' && ' · para la información solicitada por el contrato'}</dd></div>}
      {checks.automatic_eligibility && <div><dt>Elegibilidad automática</dt><dd>{status(checks.automatic_eligibility.status)}</dd></div>}
    </dl>
    {checks.text && <p>Disponer de texto o procesar todas las páginas recibidas no acredita por sí solo las asociaciones ni la cobertura documental.</p>}
    {!!checks.documentary_completeness?.reasons?.length && <ul aria-label="Motivos de completitud pendiente">{checks.documentary_completeness.reasons.map((reason, index) => <li key={index}>{semanticReasonDescription(reason)}</li>)}</ul>}
    {!!checks.automatic_eligibility?.reasons?.length && <ul aria-label="Controles para aprobación automática">{checks.automatic_eligibility.reasons.map((reason, index) => <li key={index}>{semanticReasonDescription(reason)}</li>)}</ul>}
    {!!refs.length && <details><summary>Evidencia de fuente</summary><ul>{refs.map(ref => <li key={ref}>{ref}</li>)}</ul></details>}
  </div>
}

function PrintedPages({ pagination }: { pagination: PrintedPagination }) {
  const states: Record<string, string> = {
    NOT_OBSERVED: 'No se ha acreditado numeración impresa. Esto no demuestra que el documento esté completo.',
    CONSISTENT: 'La numeración impresa acreditada es coherente dentro de su ámbito.',
    INCOMPLETE: 'La numeración impresa acreditada indica páginas documentales ausentes.',
    CONFLICT: 'La numeración impresa conserva una inconsistencia que requiere revisión.',
    UNVERIFIED: 'La numeración impresa necesita corroboración o aclaración de su ámbito.',
  }
  const scope = (kind?: string, label?: string | null) => kind === 'DOCUMENT' ? 'Documento' : kind === 'SECTION' ? `Sección${label ? `: ${label}` : ' sin etiqueta acreditada'}` : 'Ámbito no demostrado'
  const numbers = (values?: number[]) => values ? values.length ? values.join(', ') : 'ninguna' : 'sin dato'
  return <div aria-label="Paginación documental impresa">
    <strong>Paginación documental impresa</strong>
    <p>{states[pagination.status || ''] || 'No hay resultado verificable de numeración impresa.'}</p>
    <p>Páginas físicas recibidas: {numbers(pagination.physical_pages)} · procesadas: {numbers(pagination.processed_pages)} · pendientes de lectura: {numbers(pagination.missing_execution_pages)}</p>
    {!!pagination.reasons?.length && <ul>{pagination.reasons.map((reason, index) => <li key={index}>{semanticReasonDescription(reason)}</li>)}</ul>}
    {!!pagination.sequences?.length && <ul aria-label="Secuencias impresas conservadas">{pagination.sequences.map((sequence, index) => <li key={index}>
      <span>Ámbito: {scope(sequence.scope, sequence.scope_label)} · números observados: {numbers(sequence.observed_numbers)} · total declarado: {sequence.expected_total ?? 'sin dato'} · {sequence.status === 'INCOMPLETE' ? 'números documentales faltantes acreditados' : 'números impresos no observados'}: {numbers(sequence.missing_logical_numbers)}</span>
      {sequence.status === 'PARTIALLY_OBSERVED' && <small>La observación parcial de la numeración no demuestra que falten páginas de esta sección.</small>}
      {!!sequence.source_refs?.length && <small>Evidencia de secuencia: {sequence.source_refs.map(evidenceReference).join(' · ')}</small>}
    </li>)}</ul>}
    {!!pagination.observations?.length && <details><summary>Numeración observada y procedencia ({pagination.observations.length})</summary><ul>{pagination.observations.map((observation, index) => <li key={index}>
      <span>Literal: {observation.literal ?? 'sin dato'} · página física: {observation.physical_page ?? 'sin dato'} · {observation.status === 'SUPPORTED' && observation.independent === true ? 'Respaldada por fuente independiente' : 'Pendiente de corroboración'}</span>
      <small>Ámbito registrado: {scope(observation.scope, observation.scope_label)}</small>
      {observation.reason && <small>{semanticReasonDescription(observation.reason)}</small>}
      {!!observation.source_refs?.length && <small>Evidencia de numeración: {observation.source_refs.map(evidenceReference).join(' · ')}</small>}
    </li>)}</ul></details>}
    {!!pagination.ignored_mentions?.length && <small>Menciones conservadas que no acreditan una secuencia: {pagination.ignored_mentions.length}</small>}
    {pagination.documentary_complete === true && <p>Secuencia impresa acreditada completa; los demás controles documentales siguen vigentes.</p>}
    {pagination.blocking === true && <p>Este control mantiene bloqueada la aprobación automática.</p>}
  </div>
}

export default function DocumentVerification({ verification, semanticReview, compact = false }: { verification?: RecordData; semanticReview?: SemanticDocumentReview; compact?: boolean }) {
  const checks = semanticReview?.verification_version === 'SEMANTIC_AUTO_V2' ? semanticReview.source_checks : undefined
  const pagination = semanticReview?.verification_version === 'SEMANTIC_AUTO_V2' ? semanticReview.printed_pagination : undefined
  if (!verification && !checks && !pagination) return null
  const pending: RecordData[] = verification?.remaining_reasons || []
  return <section aria-label="Verificación automática de la extracción" className="document-verification">
    <strong>{statuses[verification?.status || ''] || 'Controles de la extracción'}</strong>
    {!compact && checks && <SourceChecks checks={checks}/>}
    {!compact && pagination && <PrintedPages pagination={pagination}/>}
    {!compact && verification && <>
      {verification.primary_reused && <p>Se reutilizó el reconocimiento original conservado.</p>}
      {verification.requested_reason && <p>Motivo registrado: {verification.requested_reason}</p>}
      {!!verification.reasons_before?.length && <details><summary>Motivo de la comprobación adicional</summary><ul>{verification.reasons_before.map((reason: RecordData, index: number) => <li key={index}>{reasons[reason.code] || semanticReasonDescription(reason.code)}{reason.path ? `: ${target(reason.path)}` : reason.table ? `: ${reason.table}` : ''}</li>)}</ul></details>}
      {verification.scope?.kind === 'WHOLE_DOCUMENT' && <p>{checks ? `Alcance de lectura: todas las páginas recibidas (${verification.scope.pages?.length || 0} páginas).` : `Alcance: documento completo (${verification.scope.pages?.length || 0} páginas), necesario para comparar las ocurrencias y continuaciones de las tablas.`}</p>}
      {!!verification.resolved_paths?.length && <details><summary>Controles resueltos ({verification.resolved_paths.length})</summary><ul>{verification.resolved_paths.map((path: string) => <li key={path}>{target(path)}</li>)}</ul></details>}
      {!!pending.length && <details open><summary>Controles que siguen pendientes ({pending.length})</summary><ul>{pending.map((reason, index) => <li key={index}>{reasons[reason.code] || semanticReasonDescription(reason.code)}{reason.path ? `: ${target(reason.path)}` : reason.table ? `: ${reason.table}` : ''}</li>)}</ul></details>}
      {verification.blocked_reason ? <p>{actions[verification.blocked_reason] || 'Comprueba las incidencias y el original antes de continuar.'}</p> : ['QUEUED', 'VERIFYING'].includes(verification.status) ? <p>No necesitas confirmar campos mientras se completan estos controles.</p> : pending.length ? <p>Revisa únicamente las excepciones indicadas contra el original. Los campos ya verificados no necesitan confirmación.</p> : <p>El validador conserva los controles de fuente, estructura y reglas de negocio antes de aprobar. Esta comprobación no declara revisión humana.</p>}
    </>}
  </section>
}
