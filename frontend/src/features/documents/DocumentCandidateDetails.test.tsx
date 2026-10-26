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
})
