import type { RecordData } from '../../api/client'

const statuses: Record<string, string> = { QUEUED: 'Verificación en cola', VERIFYING: 'Comprobando la extracción', CONTROLS_COMPLETED: 'Controles adicionales completados', REVIEW_REQUIRED: 'Verificación terminada con excepciones', BLOCKED: 'Verificación adicional detenida' }
const reasons: Record<string, string> = {
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

export default function DocumentVerification({ verification, compact = false }: { verification?: RecordData; compact?: boolean }) {
  if (!verification) return null
  const pending: RecordData[] = verification.remaining_reasons || []
  return <section aria-label="Verificación automática de la extracción" className="document-verification">
    <strong>{statuses[verification.status] || 'Controles de la extracción'}</strong>
    {!compact && <>
      {verification.primary_reused && <p>Se reutilizó el reconocimiento original conservado.</p>}
      {verification.requested_reason && <p>Motivo registrado: {verification.requested_reason}</p>}
      {!!verification.reasons_before?.length && <details><summary>Motivo de la comprobación adicional</summary><ul>{verification.reasons_before.map((reason: RecordData, index: number) => <li key={index}>{reasons[reason.code] || reason.code}{reason.path ? `: ${target(reason.path)}` : reason.table ? `: ${reason.table}` : ''}</li>)}</ul></details>}
      {verification.scope?.kind === 'WHOLE_DOCUMENT' && <p>Alcance: documento completo ({verification.scope.pages?.length || 0} páginas), necesario para comparar las ocurrencias y continuaciones de las tablas.</p>}
      {!!verification.resolved_paths?.length && <details><summary>Controles resueltos ({verification.resolved_paths.length})</summary><ul>{verification.resolved_paths.map((path: string) => <li key={path}>{target(path)}</li>)}</ul></details>}
      {!!pending.length && <details open><summary>Controles que siguen pendientes ({pending.length})</summary><ul>{pending.map((reason, index) => <li key={index}>{reasons[reason.code] || reason.code}{reason.path ? `: ${target(reason.path)}` : reason.table ? `: ${reason.table}` : ''}</li>)}</ul></details>}
      {verification.blocked_reason ? <p>{actions[verification.blocked_reason] || 'Comprueba las incidencias y el original antes de continuar.'}</p> : ['QUEUED', 'VERIFYING'].includes(verification.status) ? <p>No necesitas confirmar campos mientras se completan estos controles.</p> : pending.length ? <p>Revisa únicamente las excepciones indicadas contra el original. Los campos ya verificados no necesitan confirmación.</p> : <p>El validador conserva los controles de fuente, estructura y reglas de negocio antes de aprobar. Esta comprobación no declara revisión humana.</p>}
    </>}
  </section>
}
