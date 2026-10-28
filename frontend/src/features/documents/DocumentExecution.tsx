import type { RecordData } from '../../api/client'
import { Badge } from '../../components/ui'
import { IncompleteSourceNotice } from './IncompleteSourceConfirmation'
import DocumentVerification from './DocumentVerification'
import { extractionStrategies } from './documentReview'

const phases: Record<string, string> = { QUEUED: 'En cola', EXTRACTION: 'Extracción', OCR: 'Reconocimiento visual', LLM: 'Interpretación semántica', VALIDATION: 'Validación independiente', REVIEW_REQUIRED: 'Revisión de excepciones', PUBLICATION: 'Publicación de datasets', COMPLETE: 'Completado', FAILED: 'Fallido', CANCELLED: 'Cancelado', NATIVE: 'Lectura digital nativa', NATIVE_PROBE: 'Comprobación de lectura digital', PADDLE: 'Reconocimiento Paddle estructurado', QWEN_VISUAL: 'Reconocimiento Qwen visual', RENDER: 'Preparación de páginas', ADAPTATION: 'Adaptación de resultados conservados', NORMALIZATION: 'Normalización declarada', CLEANUP: 'Cierre de recursos' }
const checks: Record<string, string> = { independent_extraction: 'Extracción independiente', scalar_and_evidence: 'Tipos y evidencia de valores', arithmetic: 'Coherencia aritmética', row_coverage: 'Completitud de filas', duplicate_keys: 'Ausencia de claves duplicadas' }
const verificationPhases: Record<string, string> = { AUTOMATIC_VERIFICATION: 'Comprobación automática de controles', RECOVERY_REUSE: 'Recuperación de evidencia conservada', VERIFICATION_REQUEST: 'Verificación solicitada', PADDLE_STRUCTURED: 'Reconocimiento Paddle estructurado' }

export default function DocumentExecution({ document: item, compact = false }: { document: RecordData; compact?: boolean }) {
  const snapshot = item.verification_configuration || item.engine_snapshot
  const quality = item.quality
  const resources = item.extraction?.engine?.resources
  const execution = item.execution
  const stage = execution?.stage || item.phase
  const expected = execution?.expected_pages
  const emitted = execution?.emitted_pages
  const pending = execution?.pending_pages
  const partial = item.incomplete_inference?.status === 'PARTIAL' || execution?.recognition_complete === false
  const verification = ['QUEUED', 'RUNNING'].includes(item.status) ? execution?.verification : item.extraction?.engine?.verification || execution?.verification
  if (!snapshot && !stage && !quality && !item.incomplete_inference && resources?.elapsed_seconds == null) return null
  return <div className={`document-execution${compact ? ' compact' : ''}`} aria-label="Ejecución documental">
    <IncompleteSourceNotice review={item.incomplete_inference} compact={compact}/>
    {stage && <span>Fase: {verificationPhases[stage] || phases[stage] || stage}</span>}
    {!compact && item.verification_configuration && <small>Configuración de esta verificación; el motor original conserva su snapshot anterior.</small>}
    <DocumentVerification verification={verification} compact={compact}/>
    {Array.isArray(expected) && Array.isArray(emitted) && <span>Páginas conservadas: {emitted.length}/{expected.length}{Array.isArray(pending) && pending.length > 0 ? ` · pendientes: ${pending.join(', ')}` : ''}</span>}
    {!compact && typeof execution?.resources_released === 'boolean' && <span>Recursos de ejecución: {execution.resources_released ? 'liberados' : 'pendientes de liberar'}</span>}
    {!compact && execution && <><span>Reconocimiento: {execution.recognition_complete === true ? 'terminado' : 'incompleto o pendiente'}</span><span>Validación: {execution.validation_complete === true ? 'terminada' : 'pendiente'}</span><span>Publicación: {execution.publication_complete === true ? 'realizada' : 'no realizada'}</span>{execution.attempt_id && <small>Intento: {execution.attempt_id}</small>}</>}
    {snapshot && <><span>Motor: {snapshot.provider}</span><span>Estrategia: {extractionStrategies[snapshot.extraction_strategy || 'LEGACY'] || snapshot.extraction_strategy}</span>{!compact && <>{snapshot.provider === 'PADDLE_QWEN_LOCAL' && <><span>Paddle: {snapshot.paddle_model || 'No aplica'}</span><span>Qwen: {snapshot.llm_model || 'No aplica'}</span></>}<small>Configuración congelada · versión {snapshot.configuration_version} · {snapshot.snapshot_sha256}</small></>}</>}
    {quality?.status && <Badge value={partial ? 'REVIEW_REQUIRED' : ['AUTO_VERIFIED', 'HUMAN_VERIFIED'].includes(quality.status) ? 'VALID' : quality.status}>{partial ? 'Requiere revisión' : quality.status === 'AUTO_VERIFIED' ? 'Aprobación automática verificada' : quality.status === 'HUMAN_VERIFIED' ? 'Revisión humana verificada' : 'Requiere revisión'}</Badge>}
    {!compact && !partial && quality?.status === 'AUTO_VERIFIED' && <small>Controles del validador {quality.validator_version}. La aprobación automática conserva la distinción con la revisión humana.</small>}
    {!compact && quality?.checks && Object.keys(quality.checks).length > 0 && <details className="document-validation-checks"><summary>Controles independientes</summary><ul>{Object.entries(quality.checks).map(([name, result]) => <li key={name}>{checks[name] || name}: {result === true ? 'Satisfactorio' : result === false ? 'Pendiente o no demostrado' : 'Sin resultado verificable'}</li>)}</ul></details>}
    {!compact && typeof resources?.elapsed_seconds === 'number' && Number.isFinite(resources.elapsed_seconds) && <small>Tiempo de extracción observado: {resources.elapsed_seconds.toLocaleString('es-CO', { maximumFractionDigits: 2 })} segundos.</small>}
  </div>
}
