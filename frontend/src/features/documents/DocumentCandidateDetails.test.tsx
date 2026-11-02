import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { renderApp } from '../../test/render'
import DocumentCandidateDetails from './DocumentCandidateDetails'

describe('Source observations and candidate alternatives', () => {
  it('renders actual association arrays and event hash lists without deriving an observed section from the requested one', () => {
    renderApp(<DocumentCandidateDetails value={{ observed: 'USD', value: 'USD', geometry_kind: 'PAGE_LOCATOR_ONLY' }} semanticReview={{ state: 'VERIFIED_AUTOMATICALLY', observed: 'USD', normalized: 'USD', verified: 'USD', corroboration: {
      status: 'VERIFIED', mechanisms: ['PDF_NATIVE'], reasons: [],
      source_refs: [{ id: 'native:line:currency', page: 1, stage: 'NATIVE_SOURCE_PROBE', mechanism: 'PDF_NATIVE', geometry_kind: 'MEASURED_SOURCE', artifact_sha256: 'artifact-proof-hash', event_hashes: ['event-hash-one', 'event-hash-two'] }],
      association: [{ basis: 'PDF_NATIVE_LABEL_VALUE', label: 'Currency', section: null, requested_section: 'TOTALS', relation: 'LABEL_VALUE_INLINE' }],
    } }}/>)
    expect(screen.getByText(/Etiqueta: Currency/)).toHaveTextContent('Etiqueta y valor en la misma línea')
    expect(screen.getByText(/Sección de origen no demostrada/)).toHaveTextContent('Sección solicitada por el contrato: TOTALS')
    expect(screen.queryByText(/Sección observada: TOTALS/)).not.toBeInTheDocument()
    expect(screen.getByText(/native:line:currency/)).toHaveTextContent('SHA-256 artifact-proof-hash')
    expect(screen.getByText(/native:line:currency/)).toHaveTextContent('Evento SHA-256 event-hash-one')
    expect(screen.getByText(/native:line:currency/)).toHaveTextContent('Evento SHA-256 event-hash-two')
    expect(screen.getByText(/ubicación exacta de este valor no está demostrada/)).toBeVisible()
  })

  it('labels an invalidated source proof as prior history and does not use it to verify the current candidate', () => {
    renderApp(<DocumentCandidateDetails value={{ observed: 'USD', value: 'EUR' }} semanticReview={{ state: 'NORMALIZED_PENDING_VERIFICATION', observed: 'USD', normalized: 'EUR', verified: null, review_required: true, corroboration: {
      status: 'INVALIDATED', reasons: ['CURRENT_CELL_DIFFERS_FROM_ATTESTED_VALUE'], mechanisms: ['PDF_NATIVE'],
      prior_proof: { proof_scope: 'PRIOR_ATTESTED_CANDIDATE' }, source_refs: [{ id: 'old-source-line', page: 1, stage: 'NATIVE_SOURCE_PROBE', proof_scope: 'PRIOR_ATTESTED_CANDIDATE', artifact_sha256: 'prior-artifact', event_hashes: ['prior-event'] }],
      association: [{ basis: 'PDF_NATIVE_LABEL_VALUE', label: 'Currency', section: null, relation: 'LABEL_VALUE_INLINE' }],
    } }}/>)
    expect(screen.getByText('Pendiente de verificación', { exact: true })).toBeVisible()
    expect(screen.getByText(/La prueba anterior se conserva como historial y no respalda el valor actual/)).toBeVisible()
    expect(screen.getByText('Evidencia de corroboración anterior')).toBeVisible()
    expect(screen.getByText(/old-source-line/)).toHaveTextContent('Prueba de un candidato anterior')
    expect(screen.getByText('El valor actual difiere del valor acreditado por la prueba anterior')).toBeVisible()
    expect(screen.queryByText('Verificado automáticamente con evidencia documental')).not.toBeInTheDocument()
    expect(screen.queryByText('Asociación documental', { exact: true })).not.toBeInTheDocument()
  })

  it('shows independent automatic evidence without claiming human review or measured geometry', () => {
    renderApp(<DocumentCandidateDetails value={{ observed: 'USD', value: 'USD', reviewed: false, geometry_kind: 'PAGE_LOCATOR_ONLY' }} semanticReview={{ state: 'VERIFIED_AUTOMATICALLY', observed: 'US dollars', normalized: 'USD', verified: 'USD', evidence_refs: [{ id: 'native-line-1', page: 2, stage: 'PDF_NATIVE', artifact_sha256: 'source-proof' }], corroboration: { status: 'VERIFIED', mechanisms: ['PDF_NATIVE'], reasons: [], association: { basis: 'NATIVE_LABEL_VALUE' } } }}/>)
    expect(screen.getByText('Verificado automáticamente con evidencia documental')).toBeVisible()
    expect(screen.getByText('Texto nativo del PDF', { exact: true })).toBeVisible()
    expect(screen.getByText(/native-line-1/)).toBeVisible()
    expect(screen.getByText(/Página 2/)).toBeVisible()
    expect(screen.queryByText(/mediante revisión autorizada/)).not.toBeInTheDocument()
    expect(screen.getByText(/ubicación exacta de este valor no está demostrada/)).toBeVisible()
  })

  it('distinguishes automatically corroborated absence from an unverified null', () => {
    const { rerender } = renderApp(<DocumentCandidateDetails value={{}} semanticReview={{ state: 'VERIFIED_AUTOMATICALLY', observed: null, normalized: null, verified: null, corroboration: { status: 'VERIFIED' } }}/>)
    expect(screen.getByText('Ausencia verificada automáticamente')).toBeVisible()
    expect(screen.queryByText('Ausencia confirmada mediante revisión')).not.toBeInTheDocument()
    rerender(<DocumentCandidateDetails value={{}} semanticReview={{ state: 'PENDING_OR_ABSENT', observed: null, normalized: null, verified: null, corroboration: { status: 'REVIEW_REQUIRED', reasons: ['OPTIONAL_ABSENCE_UNVERIFIED'] } }}/>)
    expect(screen.getByText('Pendiente de verificación', { exact: true })).toBeVisible()
    expect(screen.queryByText('Ausencia verificada automáticamente')).not.toBeInTheDocument()
    expect(screen.getByText('La ausencia opcional no está acreditada contra la fuente')).toBeVisible()
  })

  it('keeps the signed observation while showing the declared normalization and an explicit alternative action', async () => {
    const choose = vi.fn(), user = userEvent.setup()
    const value = { observed: '-12.50', value: '12.50', geometry_kind: 'TABLE_REGION', normalization: { version: 1, rule: 'DEDUCTION_MAGNITUDE', observed: '-12.50', normalized: '12.50', evidence_refs: ['page-1'] }, candidate_alternatives: [{ stage: 'PADDLE_STRUCTURED', candidate: { candidate_id: 'other', observed: '-13.50', value: '13.50', origin: { page: 2 }, evidence_refs: ['other-cell'] } }] }
    renderApp(<DocumentCandidateDetails value={value} canReview onChoose={choose}/>)
    expect(screen.getByText('-12.50', { exact: true })).toBeVisible()
    expect(screen.getByText('12.50', { exact: true })).toBeVisible()
    expect(screen.getByText(/Magnitud de deducción declarada/)).toBeVisible()
    expect(screen.getByText(/no es una localización medida/)).toBeVisible()
    await user.click(screen.getByText('Alternativas conservadas (1)'))
    expect(screen.getByText('PADDLE_STRUCTURED')).toBeVisible()
    expect(screen.getByText('Página de la alternativa: 2')).toBeVisible()
    expect(screen.getByText('Evidencia de la alternativa: other-cell')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Preparar esta alternativa' }))
    expect(choose).toHaveBeenCalledWith('13.50')
    expect(value.observed).toBe('-12.50')
    expect(value.value).toBe('12.50')
  })

  it('does not offer an alternative correction without review permission', async () => {
    renderApp(<DocumentCandidateDetails value={{ alternatives: ['000012'] }}/>)
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })

  it('keeps a normalized semantic proposal pending while preserving the model context and literal alternatives', async () => {
    const user = userEvent.setup()
    renderApp(<DocumentCandidateDetails value={{ observed: '125.00', value: '125.00', geometry_kind: 'PAGE_LOCATOR_ONLY' }} targetType="DECIMAL" semanticReview={{ state: 'NORMALIZED_PENDING_VERIFICATION', observed: '125.00', normalized: '125.00', verified: null }} observations={[
      { observation_id: 'observation-1', source_page: 2, stage: 'QWEN_VISUAL', observed: '125.00', alternatives: ['150.00'], model_context: { literal_context: 'No se aprobó el pago de 125.00.', basis: 'PRINTED', polarity: 'NEGATED' } },
    ]}/> )
    expect(screen.getByText('Normalizado; pendiente de verificación')).toBeVisible()
    expect(screen.getByText('Pendiente de verificación', { exact: true })).toBeVisible()
    expect(screen.getByText('Decimal', { exact: true })).toBeVisible()
    await user.click(screen.getByText('Observaciones de origen (1)'))
    expect(screen.getByText('No se aprobó el pago de 125.00.')).toBeVisible()
    expect(screen.getByText('Clasificación propuesta: PRINTED · NEGATED')).toBeVisible()
    expect(screen.getByText('Alternativas observadas: 150.00')).toBeVisible()
    expect(screen.queryByText(/Aprobación automática/)).not.toBeInTheDocument()
    expect(screen.getByText(/ubicación exacta de este valor no está demostrada/)).toBeVisible()
  })

  it('identifies an authorized correction and a confirmed optional absence without inferring automatic approval', () => {
    renderApp(<DocumentCandidateDetails value={{}} semanticReview={{ state: 'AUTHORIZED_CORRECTION', observed: 'unsupported', normalized: null, verified: null }}/> )
    expect(screen.getByText('Corregido mediante revisión autorizada')).toBeVisible()
    expect(screen.getByText('Ausencia confirmada mediante revisión')).toBeVisible()
    expect(screen.queryByText('Pendiente de verificación')).not.toBeInTheDocument()
  })
})
