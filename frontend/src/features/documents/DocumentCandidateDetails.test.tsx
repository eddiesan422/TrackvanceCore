import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { renderApp } from '../../test/render'
import DocumentCandidateDetails from './DocumentCandidateDetails'

describe('Source observations and candidate alternatives', () => {
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
