import type { RecordData } from '../../api/client'
import { sourceGeometryKind } from './documentReview'

const normalizationRules: Record<string, string> = {
  IDENTITY: 'Texto conservado sin transformación',
  DECIMAL_SIGNED: 'Importe firmado con formato declarado',
  DEDUCTION_MAGNITUDE: 'Magnitud de deducción declarada',
  DECLARED_CURRENCY_EQUIVALENCE: 'Equivalencia de moneda declarada',
}

function text(value: unknown): string {
  return value == null ? 'Ausente' : typeof value === 'object' ? JSON.stringify(value) : String(value)
}

export default function DocumentCandidateDetails({ value, canReview = false, onChoose }: {
  value: RecordData; canReview?: boolean; onChoose?: (value: string) => void
}) {
  const normalization = value.normalization
  const alternatives: unknown[] = value.candidate_alternatives?.length ? value.candidate_alternatives : value.alternatives || []
  const kind = sourceGeometryKind(value)
  return <div className="document-candidate-details">
    {normalization && <dl aria-label="Normalización del valor">
      <div><dt>Texto observado</dt><dd>{text(normalization.observed !== undefined ? normalization.observed : value.observed)}</dd></div>
      <div><dt>Valor normalizado</dt><dd>{text(normalization.normalized !== undefined ? normalization.normalized : value.value)}</dd></div>
      <div><dt>Regla aplicada</dt><dd>{normalizationRules[normalization.rule] || normalization.rule} · versión {normalization.version}</dd></div>
      {!!normalization.evidence_refs?.length && <div><dt>Evidencia de la normalización</dt><dd>{normalization.evidence_refs.join(', ')}</dd></div>}
    </dl>}
    {kind && <small>{kind === 'MEASURED' ? 'Localización medida en la fuente' : kind === 'QUERY_REGION' ? 'La región consultada no es una localización medida de este valor.' : 'La ubicación exacta de este valor no está demostrada.'}</small>}
    {alternatives.length > 0 && <details className="document-candidate-alternatives"><summary>Alternativas conservadas ({alternatives.length})</summary>
      <p>La propuesta inicial permanece conservada. Elegir otra alternativa prepara una corrección que requiere confirmación.</p>
      <ul>{alternatives.map((alternative, index) => {
        const wrapper = typeof alternative === 'object' && alternative !== null ? alternative as RecordData : { observed: alternative, value: alternative }
        const item: RecordData = wrapper.candidate && typeof wrapper.candidate === 'object' ? wrapper.candidate : wrapper
        const normalized = item.normalized ?? item.value ?? item.normalization?.normalized
        const page = item.page || item.origin?.page
        return <li key={wrapper.candidate_id || item.candidate_id || item.id || index}>
          <span>{wrapper.stage || item.route || item.strategy || item.provider || 'Candidato conservado'}{(item.status || item.validity || item.ambiguous) ? ` · ${item.status || item.validity || 'AMBIGUOUS'}` : ''}</span>
          <small>Observado: {text(item.observed)} · Normalizado: {normalized === undefined ? 'Sin valor normalizado disponible' : text(normalized)}</small>
          {page && <small>Página de la alternativa: {page}</small>}
          {!!item.evidence_refs?.length && <small>Evidencia de la alternativa: {item.evidence_refs.join(', ')}</small>}
          {canReview && onChoose && typeof normalized === 'string' && <button type="button" className="button secondary small" onClick={() => onChoose(normalized)}>Preparar esta alternativa</button>}
        </li>
      })}</ul>
    </details>}
  </div>
}
