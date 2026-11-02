import type { RecordData } from '../../api/client'
import { date } from '../../components/ui'
import { evidenceReference } from './documentSemanticEvidence'
import type { DocumentaryEvidenceReference } from './documentSemanticEvidence'

export default function SemanticTableCoverage({ definitions, tables, status, confirmed, onChange, canView, busy }: {
  definitions: RecordData[]; tables: RecordData; status?: RecordData; confirmed: string[]; onChange: (names: string[]) => void; canView: boolean; busy: boolean
}) {
  if (!definitions.length) return null
  return <section className="document-semantic-coverage" aria-label="Revisión de cobertura documental">
    <h3>Cobertura de tablas y colecciones</h3>
    <p>Confirma únicamente las colecciones pendientes después de comprobar todas las páginas, filas y posibles ausencias en el original. La cobertura ya acreditada no necesita otra confirmación.</p>
    {definitions.map(table => {
      const review = status?.tables?.find((item: RecordData) => item.name === table.name)
      const covered = review?.review_required === false
      const automatic = covered && review.coverage_state === 'AUTOMATICALLY_VERIFIED'
      return <div key={table.name}>
        <strong>{table.name}</strong><small>Filas conservadas: {tables[table.name]?.length || 0}</small>
        {typeof review?.review_required === 'boolean' && <small>Estado de cobertura guardado: {review.review_required ? 'requiere revisión' : automatic ? 'verificada automáticamente' : 'confirmado mediante revisión'}</small>}
        {!!review?.proof_refs?.length && <small>Evidencia de cobertura: {review.proof_refs.map((reference: DocumentaryEvidenceReference) => evidenceReference(reference)).join(' · ')}</small>}
        {review?.latest_confirmation && <small>Última confirmación: {date(review.latest_confirmation.created_at)} · {review.latest_confirmation.reason}</small>}
        {!covered && <label><input type="checkbox" disabled={!canView || busy} checked={confirmed.includes(table.name)} onChange={event => onChange(event.target.checked ? [...confirmed, table.name] : confirmed.filter(name => name !== table.name))}/> Confirmé en el original la cobertura de {table.name}</label>}
      </div>
    })}
    {!canView && definitions.some(table => status?.tables?.find((item: RecordData) => item.name === table.name)?.review_required !== false) && <p>La confirmación de cobertura requiere permiso para consultar el original.</p>}
  </section>
}
