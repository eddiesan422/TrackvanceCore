import { act, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, download, post } from '../../api/client'
import type { RecordData } from '../../api/client'
import { renderApp } from '../../test/render'
import { RunDetail } from '../runs/Runs'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn(), download: vi.fn() }))

let currentRun: RecordData, runReads: number
const permissions = ['runs:read', 'runs:execute', 'artifacts:download']
const advance = async (milliseconds: number) => { await act(async () => { await vi.advanceTimersByTimeAsync(milliseconds) }) }
const renderRun = (grants = permissions) => renderApp(<RunDetail/>, { path: '/runs/run-settling', route: '/runs/:id', permissions: grants })

beforeEach(async () => {
  await import('./DeliveryRunDetail')
  vi.useFakeTimers()
  vi.resetAllMocks()
  runReads = 0
  currentRun = { id: 'run-settling', module: 'DELIVERY', status: 'SUCCESS', decision: 'COMMITTED', name: 'Entrega confirmada', metrics: {} }
  vi.mocked(api).mockImplementation(async path => {
    if (path === '/runs/run-settling') {
      runReads += 1
      // Force a fresh render each poll, proving the deadline is not extended by DTO updates.
      return { ...currentRun, progress_stage: `Lectura ${runReads}` }
    }
    if (path === '/delivery/runs/run-settling/attempts') return { items: [{ id: 'attempt-settling', status: 'COMMITTED', attempt_number: 1 }], total: 1 }
    if (path === '/delivery/runs/run-settling/receipt') return { kind: 'DELIVERY_RECEIPT', run_id: 'run-settling', result: 'COMMITTED', target_locator: 'public.published' }
    throw new Error(`Unexpected request ${path}`)
  })
})

afterEach(() => vi.useRealTimers())

describe('Delivery commit before local evidence publication', () => {
  it('keeps polling after RUNNING becomes COMMITTED and loads a later receipt without replay', async () => {
    currentRun = { ...currentRun, status: 'RUNNING', decision: null }
    renderRun()
    await advance(500)
    currentRun = { ...currentRun, status: 'SUCCESS', decision: 'COMMITTED' }
    await advance(1600)
    expect(screen.getByText('Entrega confirmada. Publicando evidencia local…')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Receipt' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Manifiesto' })).toBeDisabled()
    expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/receipt'))).toBe(false)
    currentRun = { ...currentRun, metrics: { receipt_artifact_id: 'receipt-settling' } }
    await advance(1600)
    expect(screen.getByText('public.published')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Receipt' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Manifiesto' })).toBeEnabled()
    const settledReads = runReads
    await advance(5000)
    expect(runReads).toBe(settledReads)
    expect(post).not.toHaveBeenCalled()
    expect(download).not.toHaveBeenCalled()
  })

  it('discovers a late PENDING_REPAIR marker from a direct committed deep link', async () => {
    renderRun()
    await advance(500)
    expect(screen.getByText('Entrega confirmada. Publicando evidencia local…')).toBeVisible()
    currentRun = { ...currentRun, metrics: { evidence_status: 'PENDING_REPAIR' } }
    await advance(1600)
    expect(screen.getByText('Entrega confirmada. La evidencia local está pendiente de reparación.')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Reparar evidencia' })).toBeEnabled()
    expect(screen.getByRole('button', { name: 'Receipt' })).toBeDisabled()
    const settledReads = runReads
    await advance(5000)
    expect(runReads).toBe(settledReads)
    expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/receipt'))).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('bounds automatic polling despite repeated rerenders and permits a read-only manual refresh', async () => {
    renderRun()
    await advance(61_000)
    expect(screen.getByText(/La consulta automática se detuvo/)).toBeVisible()
    expect(runReads).toBeGreaterThan(10)
    expect(runReads).toBeLessThanOrEqual(42)
    expect(screen.queryByRole('button', { name: 'Reparar evidencia' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Receipt' })).toBeDisabled()
    const stoppedReads = runReads
    await advance(30_000)
    expect(runReads).toBe(stoppedReads)
    currentRun = { ...currentRun, metrics: { receipt_artifact_id: 'receipt-settling' } }
    fireEvent.click(screen.getByRole('button', { name: 'Actualizar estado' }))
    await advance(100)
    expect(screen.getByText('public.published')).toBeVisible()
    expect(runReads).toBe(stoppedReads + 1)
    expect(post).not.toHaveBeenCalled()
    expect(download).not.toHaveBeenCalled()
  })

  it('settles for read-only users without offering repair or accessing downloads', async () => {
    renderRun(['runs:read'])
    await advance(500)
    currentRun = { ...currentRun, metrics: { evidence_status: 'PENDING_REPAIR' } }
    await advance(1600)
    expect(screen.getByText('Entrega confirmada. La evidencia local está pendiente de reparación.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Reparar evidencia' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Receipt' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Manifiesto' })).toBeDisabled()
    expect(vi.mocked(api).mock.calls.some(([path]) => path.endsWith('/receipt'))).toBe(false)
    expect(post).not.toHaveBeenCalled()
  })

  it('reuses a slow in-flight status read instead of cancelling it at every interval', async () => {
    renderRun()
    await advance(500)
    const original = vi.mocked(api).getMockImplementation()!
    let finishRead!: (value: RecordData) => void
    const delayed = new Promise<RecordData>(resolve => { finishRead = resolve })
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path === '/runs/run-settling') { runReads += 1; return delayed }
      return original(path, options)
    })
    await advance(1600)
    expect(runReads).toBe(2)
    await advance(5000)
    expect(runReads).toBe(2)
    finishRead({ ...currentRun, metrics: { receipt_artifact_id: 'receipt-settling' } })
    await advance(100)
    expect(screen.getByText('public.published')).toBeVisible()
    expect(post).not.toHaveBeenCalled()
  })

  it('clears settlement timers when leaving the Run', async () => {
    const rendered = renderRun()
    await advance(500)
    const beforeLeaving = runReads
    rendered.unmount()
    await advance(61_000)
    expect(runReads).toBe(beforeLeaving)
    expect(post).not.toHaveBeenCalled()
  })
})
