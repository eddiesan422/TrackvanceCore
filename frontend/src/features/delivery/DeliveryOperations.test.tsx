import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useQuery } from '@tanstack/react-query'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import type { RecordData } from '../../api/client'
import { renderApp } from '../../test/render'
import { DeliveryRunDetail } from './DeliveryRunDetail'
import type { DeliveryReview, DeliveryReviewOutcome } from './types'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn(), download: vi.fn() }))

const permissions = ['runs:read', 'runs:execute', 'artifacts:download', 'audit:read']
const committed = { id: 'run-1', module: 'DELIVERY', status: 'SUCCESS', decision: 'COMMITTED', metrics: { evidence_status: 'PENDING_REPAIR' }, name: 'Entrega confirmada', created_at: '2026-09-25T12:00:00Z' }
const attempt = { id: 'attempt-1', run_id: 'run-1', attempt_number: 1, status: 'COMMITTED', rows_attempted: 2, rows_written: 2, rows_inserted: null, rows_updated: null }
let currentRun: RecordData, currentAttempt: RecordData, reviews: DeliveryReview[]

function ObservedRun() {
  const run = useQuery({ queryKey: ['run', 'run-1'], queryFn: () => api('/runs/run-1') })
  return run.data ? <DeliveryRunDetail data={run.data}/> : null
}

beforeEach(() => {
  vi.resetAllMocks()
  currentRun = { ...committed }
  currentAttempt = { ...attempt }
  reviews = []
  vi.mocked(api).mockImplementation(async path => {
    if (path === '/runs/run-1') return currentRun
    if (path === '/delivery/runs/run-1/attempts') return { items: [currentAttempt], total: 1 }
    if (path === '/delivery/runs/run-1/reviews') return { items: reviews, total: reviews.length }
    if (path === '/audit-events') return { items: [], total: 0 }
    if (path === '/delivery/runs/run-1/receipt') return { run_id: 'run-1', result: 'COMMITTED', rows_written: 2 }
    throw new Error(`Unexpected request ${path}`)
  })
})

describe('Committed evidence repair', () => {
  it.each(['REPAIRED', 'ALREADY_VALID'])('repairs locally with response %s and refreshes evidence without creating a delivery', async status => {
    vi.mocked(post).mockImplementation(async path => {
      expect(path).toBe('/delivery/runs/run-1/repair-evidence')
      currentRun = { ...committed, metrics: { evidence_status: 'VALID', receipt_artifact_id: 'receipt-1' } }
      return { run_id: 'run-1', status, receipt_artifact_id: 'receipt-1', manifest_artifact_id: 'manifest-1' }
    })
    const user = userEvent.setup()
    renderApp(<ObservedRun/>, { permissions })
    expect(await screen.findByText('Entrega confirmada. La evidencia local está pendiente de reparación.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Receipt' })).toBeDisabled()
    expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/receipt'))).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Reparar evidencia' }))
    await waitFor(() => expect(screen.queryByText('Entrega confirmada. La evidencia local está pendiente de reparación.')).not.toBeInTheDocument())
    expect(await screen.findByText(/No se repitió la entrega/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Receipt' })).toBeEnabled()
    expect(post).toHaveBeenCalledTimes(1)
    expect(post).toHaveBeenCalledWith('/delivery/runs/run-1/repair-evidence')
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  })

  it('retains PENDING_REPAIR after a closed failure and does not retry automatically', async () => {
    vi.mocked(post).mockRejectedValue(new Error('Falta el artifact canónico íntegro.'))
    const user = userEvent.setup()
    renderApp(<ObservedRun/>, { permissions })
    await user.click(await screen.findByRole('button', { name: 'Reparar evidencia' }))
    expect(await screen.findByText('Falta el artifact canónico íntegro.')).toBeVisible()
    expect(screen.getByText('Entrega confirmada. La evidencia local está pendiente de reparación.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Receipt' })).toBeDisabled()
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('shows confirmed evidence status to read-only users without offering repair', async () => {
    renderApp(<ObservedRun/>, { permissions: ['runs:read', 'artifacts:download'] })
    expect(await screen.findByText('Entrega confirmada. La evidencia local está pendiente de reparación.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Reparar evidencia' })).not.toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })
})

describe('Operational review of UNKNOWN', () => {
  beforeEach(() => {
    currentRun = { ...committed, status: 'UNKNOWN', decision: 'UNKNOWN', metrics: {} }
    currentAttempt = { ...attempt, status: 'UNKNOWN', rows_written: null }
  })

  it.each<DeliveryReviewOutcome>(['REMOTE_COMMIT_OBSERVED', 'REMOTE_NOT_COMMITTED_OBSERVED', 'INCONCLUSIVE'])('records %s while preserving UNKNOWN, without replay', async outcome => {
    vi.mocked(post).mockImplementation(async (path, body) => {
      expect(path).toBe('/delivery/runs/run-1/reviews')
      const payload = body as { outcome: DeliveryReviewOutcome; note: string }
      const review: DeliveryReview = { id: 'review-1', run_id: 'run-1', delivery_attempt_id: 'attempt-1', reviewer_id: 'reviewer-1', reviewer_name: 'Operador verificador', ...payload, verified_at: '2026-09-25T12:00:00Z', created_at: '2026-09-25T12:01:00Z' }
      reviews = [review]
      return review
    })
    const user = userEvent.setup()
    renderApp(<ObservedRun/>, { permissions })
    await user.click(await screen.findByRole('button', { name: 'Revisar resultado' }))
    const dialog = screen.getByRole('dialog', { name: 'Revisar resultado' })
    expect(within(dialog).getByRole('button', { name: 'Guardar revisión' })).toBeDisabled()
    await user.selectOptions(within(dialog).getByLabelText('Resultado observado externamente'), outcome)
    await user.type(within(dialog).getByLabelText('Nota / motivo'), '  Verificación externa documentada sin datos de negocio.  ')
    await user.click(within(dialog).getByRole('button', { name: 'Guardar revisión' }))
    expect(await screen.findByText('Operador verificador')).toBeVisible()
    expect(screen.getByText(outcome)).toBeVisible()
    expect(screen.getByText('Confirmación remota desconocida')).toBeVisible()
    expect(screen.getAllByText('Confirmación desconocida').length).toBeGreaterThan(0)
    expect(post).toHaveBeenCalledExactlyOnceWith('/delivery/runs/run-1/reviews', { delivery_attempt_id: 'attempt-1', outcome, note: 'Verificación externa documentada sin datos de negocio.' })
    expect(screen.queryByRole('button', { name: /Reintentar|Ejecutar entrega|Reparar evidencia/ })).not.toBeInTheDocument()
    expect(currentRun.status).toBe('UNKNOWN')
    expect(currentAttempt.status).toBe('UNKNOWN')
  })

  it('shows review history without a mutation action for read-only users', async () => {
    reviews = [{ id: 'review-1', run_id: 'run-1', delivery_attempt_id: 'attempt-1', reviewer_id: 'reviewer-1', reviewer_name: 'Auditor externo', outcome: 'INCONCLUSIVE', note: 'No fue posible confirmar.', verified_at: '2026-09-25T12:00:00Z', created_at: '2026-09-25T12:01:00Z' }]
    renderApp(<ObservedRun/>, { permissions: ['runs:read'] })
    expect(await screen.findByText('Auditor externo')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Revisar resultado' })).not.toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('does not hide a rejected review or pretend it was saved', async () => {
    vi.mocked(post).mockRejectedValue(new Error('La fecha de verificación no puede ser futura.'))
    const user = userEvent.setup()
    renderApp(<ObservedRun/>, { permissions })
    await user.click(await screen.findByRole('button', { name: 'Revisar resultado' }))
    await user.type(screen.getByLabelText('Nota / motivo'), 'Verificación documentada.')
    await user.click(screen.getByRole('button', { name: 'Guardar revisión' }))
    expect(await screen.findByText('La fecha de verificación no puede ser futura.')).toBeVisible()
    expect(screen.getByRole('dialog')).toBeVisible()
    expect(post).toHaveBeenCalledTimes(1)
    expect(screen.getByText('Confirmación remota desconocida')).toBeVisible()
  })
})

describe('Delivery metric semantics', () => {
  it('preserves authoritative null metrics as N/D even if another DTO contains counts', async () => {
    currentRun = { ...committed, metrics: { rows_attempted: 90, rows_written: 90, rows_inserted: 90, rows_updated: 90, bytes_sent: 90 } }
    currentAttempt = { ...attempt, rows_attempted: null, rows_written: null, rows_inserted: null, rows_updated: null, bytes_sent: null }
    renderApp(<ObservedRun/>, { permissions: ['runs:read'] })
    await screen.findByText('#1')
    const metricGrid = screen.getByText('Filas preparadas').parentElement!.parentElement!
    expect(within(metricGrid).getAllByText(/N\/D/)).toHaveLength(5)
    expect(within(metricGrid).queryByText('90')).not.toBeInTheDocument()
    expect(screen.getByText(/Filas enviadas no significa filas físicas finales/)).toBeVisible()
    expect(screen.getByTitle(/no es el total físico final/)).toHaveTextContent('Filas enviadas')
  })

  it('displays measured zeros as zero and absent adapter counts as N/D', async () => {
    currentRun = { ...committed, metrics: {} }
    currentAttempt = { ...attempt, rows_attempted: 0, rows_written: 0, rows_inserted: null, rows_updated: null, bytes_sent: 0 }
    renderApp(<ObservedRun/>, { permissions: ['runs:read'] })
    await screen.findByText('#1')
    const metricGrid = screen.getByText('Filas preparadas').parentElement!.parentElement!
    expect(within(metricGrid).getAllByText('0')).toHaveLength(3)
    expect(within(metricGrid).getAllByText(/N\/D/)).toHaveLength(2)
  })
})
