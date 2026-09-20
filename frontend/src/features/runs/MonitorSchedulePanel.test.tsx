import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { MonitorSchedulePanel } from './MonitorSchedulePanel'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))

const existing = { id: 'schedule', version: 2, enabled: true, interval_seconds: 3600, next_run_at: '2026-09-20T10:00:00Z', starts_at: '2026-09-20T10:00:00Z' }
function mockResponses(schedule: typeof existing | null = null) {
  vi.mocked(api).mockImplementation(async path => {
    if (path.endsWith('/schedule')) return schedule
    if (path.endsWith('/series')) return { items: [], sample_count: 0, limit: 500 }
    return { items: [], total: 0 }
  })
}

describe('Sentinel local scheduling', () => {
  beforeEach(() => { vi.clearAllMocks(); mockResponses(); vi.mocked(post).mockResolvedValue(existing) })

  it('loads lazily and saves an explicit cadence against registered snapshots', async () => {
    const user = userEvent.setup()
    renderApp(<MonitorSchedulePanel monitorId="monitor"/>)
    expect(api).not.toHaveBeenCalled()
    await user.click(screen.getByText('Programación e histórico'))
    const minutes = await screen.findByLabelText('Periodicidad (minutos)')
    await user.clear(minutes); await user.type(minutes, '15')
    expect(screen.getByText(/Evalúa el último snapshot registrado/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Guardar programación' }))
    expect(post).toHaveBeenCalledWith('/monitors/monitor/schedule', { interval_seconds: 900, enabled: true, starts_at: null, expected_version: null })
  })

  it('pauses using the current revision and keeps errors actionable', async () => {
    mockResponses(existing)
    vi.mocked(post).mockRejectedValue(new Error('La programación cambió. Actualiza e intenta nuevamente.'))
    const user = userEvent.setup()
    renderApp(<MonitorSchedulePanel monitorId="monitor"/>)
    await user.click(screen.getByText('Programación e histórico'))
    await user.click(await screen.findByLabelText('Programación activa'))
    await user.click(screen.getByRole('button', { name: 'Guardar programación' }))
    expect(post).toHaveBeenCalledWith('/monitors/monitor/schedule', expect.objectContaining({ enabled: false, expected_version: 2 }))
    expect(await screen.findByRole('alert')).toHaveTextContent('La programación cambió')
  })

  it('accepts an accessible local start date and sends its explicit UTC instant', async () => {
    const user = userEvent.setup()
    renderApp(<MonitorSchedulePanel monitorId="monitor"/>)
    await user.click(screen.getByText('Programación e histórico'))
    fireEvent.change(await screen.findByLabelText('Primera ejecución (hora local)'), { target: { value: '2030-01-01T10:30' } })
    await user.click(screen.getByRole('button', { name: 'Guardar programación' }))
    expect(post).toHaveBeenCalledWith('/monitors/monitor/schedule', expect.objectContaining({ starts_at: new Date('2030-01-01T10:30').toISOString() }))
  })

  it('preserves read-only access for viewers', async () => {
    mockResponses(existing)
    const user = userEvent.setup()
    renderApp(<MonitorSchedulePanel monitorId="monitor"/>, { permissions: ['runs:read'] })
    await user.click(screen.getByText('Programación e histórico'))
    expect(await screen.findByRole('button', { name: 'Guardar programación' })).toBeDisabled()
    expect(screen.getByLabelText('Programación activa')).toBeDisabled()
    expect(screen.getByLabelText('Periodicidad (minutos)')).toBeDisabled()
  })

  it('shows loading and independent schedule retrieval failure', async () => {
    let reject!: (error: Error) => void
    vi.mocked(api).mockImplementation(path => path.endsWith('/schedule') ? new Promise((_, fail) => { reject = fail }) : Promise.resolve({ items: [], total: 0 }))
    const user = userEvent.setup()
    renderApp(<MonitorSchedulePanel monitorId="monitor"/>)
    await user.click(screen.getByText('Programación e histórico'))
    expect(await screen.findByText('Cargando programación…')).toBeInTheDocument()
    await act(async () => reject(new Error('Servicio no disponible')))
    expect(await screen.findByRole('alert')).toHaveTextContent('Servicio no disponible')
    expect(screen.getByRole('button', { name: 'Volver a intentar' })).toBeEnabled()
  })

  it('links alerts, occurrences and comparable temporal metrics to their evidence', async () => {
    vi.mocked(api).mockImplementation(async path => {
      if (path.endsWith('/schedule')) return existing
      if (path.endsWith('/series')) return { items: [{ metric_key: 'row_count', dimensions: {}, method: 'EXACT_OBSERVED', metric_definition_version: 2, points: [
        { run_id: 'one', observed_at: '2026-09-19T10:00:00Z', value: 100, decision: 'HEALTHY' },
        { run_id: 'middle', observed_at: '2026-09-19T16:00:00Z', value: 85, decision: 'HEALTHY' },
        { run_id: 'two', observed_at: '2026-09-20T10:00:00Z', value: 70, decision: 'ALERT' },
      ] }], sample_count: 3, limit: 500 }
      if (path.endsWith('/occurrences')) return { items: [{ id: 'o', planned_at: '2026-09-20T10:00:00Z', started_at: '2026-09-20T10:00:05Z', status: 'SUCCESS', decision: 'ALERT', run_id: 'two' }], total: 1 }
      return { items: [{ id: 'finding', run_id: 'two', title: 'Volumen reducido', severity: 'HIGH', created_at: '2026-09-20T10:00:00Z', exception_id: 'case' }], total: 1 }
    })
    const user = userEvent.setup()
    renderApp(<MonitorSchedulePanel monitorId="monitor"/>)
    await user.click(screen.getByText('Programación e histórico'))
    const chart = await screen.findByRole('img', { name: 'Evolución de row_count' })
    const positions = [...chart.querySelectorAll('circle')].map(circle => Number(circle.getAttribute('cx')))
    // Six hours occupy a quarter of a 24-hour interval, regardless of sample count.
    expect((positions[1] - positions[0]) / (positions[2] - positions[0])).toBeCloseTo(0.25)
    expect(chart.textContent).toContain('100')
    expect(screen.getByRole('link', { name: 'Volumen reducido' })).toHaveAttribute('href', '/runs/two')
    expect(screen.getByRole('link', { name: 'Ver excepción' })).toHaveAttribute('href', '/exceptions?id=case')
    await waitFor(() => expect(screen.getByText('Completada')).toBeInTheDocument())
    expect(screen.getAllByText('Alerta').length).toBeGreaterThan(0)
  })
})
