import type { RecordData } from '../../api/client'
import { Badge } from '../../components/ui'
import { IncompleteSourceNotice } from './IncompleteSourceConfirmation'

const phases: Record<string, string> = { QUEUED: 'En cola', EXTRACTION: 'Extracción', OCR: 'Reconocimiento visual', LLM: 'Interpretación semántica', VALIDATION: 'Validación independiente', REVIEW_REQUIRED: 'Revisión de excepciones', PUBLICATION: 'Publicación de datasets', COMPLETE: 'Completado', FAILED: 'Fallido', CANCELLED: 'Cancelado' }
const checks: Record<string, string> = { independent_extraction: 'Extracción independiente', scalar_and_evidence: 'Tipos y evidencia de valores', arithmetic: 'Coherencia aritmética', row_coverage: 'Completitud de filas', duplicate_keys: 'Ausencia de claves duplicadas' }

export default function DocumentExecution({ document: item, compact = false }: { document: RecordData; compact?: boolean }) {
  const snapshot = item.engine_snapshot
  const quality = item.quality
  const resources = item.extraction?.engine?.resources
  const partial = item.incomplete_inference?.status === 'PARTIAL'
  if (!snapshot && !item.phase && !quality && !item.incomplete_inference && resources?.elapsed_seconds == null) return null
  return <div className={`document-execution${compact ? ' compact' : ''}`} aria-label="Ejecución documental">
    <IncompleteSourceNotice review={item.incomplete_inference} compact={compact}/>
    {item.phase && <span>Fase: {phases[item.phase] || item.phase}</span>}
    {['QUEUED', 'RUNNING'].includes(item.status) && typeof item.progress === 'number' && <progress max={100} value={item.progress} aria-label="Progreso del procesamiento"/>}
    {snapshot && <><span>Motor: {snapshot.provider}</span>{!compact && <>{snapshot.provider === 'PADDLE_QWEN_LOCAL' && <><span>Modelo visual: {snapshot.paddle_model || 'No aplica'}</span><span>Modelo semántico: {snapshot.use_llm ? snapshot.llm_model : 'Desactivado'}</span></>}<small>Configuración congelada · versión {snapshot.configuration_version} · {snapshot.snapshot_sha256}</small></>}</>}
    {quality?.status && <Badge value={partial ? 'REVIEW_REQUIRED' : ['AUTO_VERIFIED', 'HUMAN_VERIFIED'].includes(quality.status) ? 'VALID' : quality.status}>{partial ? 'Requiere revisión' : quality.status === 'AUTO_VERIFIED' ? 'Aprobación automática verificada' : quality.status === 'HUMAN_VERIFIED' ? 'Revisión humana verificada' : 'Requiere revisión'}</Badge>}
    {!compact && !partial && quality?.status === 'AUTO_VERIFIED' && <small>Controles del validador {quality.validator_version}. La aprobación automática conserva la distinción con la revisión humana.</small>}
    {!compact && quality?.checks && Object.keys(quality.checks).length > 0 && <details className="document-validation-checks"><summary>Controles independientes</summary><ul>{Object.entries(quality.checks).map(([name, result]) => <li key={name}>{checks[name] || name}: {result === true ? 'Satisfactorio' : result === false ? 'Pendiente o no demostrado' : 'Sin resultado verificable'}</li>)}</ul></details>}
    {!compact && typeof resources?.elapsed_seconds === 'number' && Number.isFinite(resources.elapsed_seconds) && <small>Tiempo de extracción observado: {resources.elapsed_seconds.toLocaleString('es-CO', { maximumFractionDigits: 2 })} segundos.</small>}
  </div>
}
