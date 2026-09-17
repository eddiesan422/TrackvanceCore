import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { renderApp } from '../test/render'
import Dashboard from './Dashboard'

vi.mock('../api/client', async importOriginal => ({
  ...await importOriginal<typeof import('../api/client')>(),
  api: vi.fn(),
}))

const response = {
  stats: { health_score: 82.5, controls_failed: 3, open_exceptions: 2, affected_datasets: 1 },
  variations: {
    health_score: { previous: 90, delta: -7.5 },
    controls_failed: { previous: 1, delta: 2 },
    open_exceptions: { previous: 3, delta: -1 },
    affected_datasets: null,
  },
  attention_total: 1,
  attention: [{ id: 'exception-1', subject_type: 'EXCEPTION', dataset_id: 'ds-1', dataset_name: 'Pedidos críticos', module: 'intake', criticality: 'CRITICAL', problem: 'Filas rechazadas', detail: 'EX-001 · OPEN', date: '2026-09-14T12:00:00Z', href: '/exceptions?id=exception-1', action_label: 'Gestionar excepción' }],
  health_history: [
    { date: '2026-09-13', overall: 95, intake: 90, recon: 100, sentinel: 95 },
    { date: '2026-09-14', overall: 82.5, intake: 80, recon: 88, sentinel: 79.5 },
  ],
  module_status: [
    { module: 'intake', status: 'ATTENTION', activity_count: 5, activity_label: 'ejecuciones', issues: 2, processed_records: 500, health_score: 80, latest_run_at: '2026-09-14T12:00:00Z' },
    { module: 'recon', status: 'HEALTHY', activity_count: 3, activity_label: 'conciliaciones', issues: 0, processed_records: 320, health_score: 98, latest_run_at: '2026-09-14T11:00:00Z' },
    { module: 'sentinel', status: 'ATTENTION', activity_count: 2, activity_label: 'monitores', issues: 1, processed_records: 200, health_score: 75, latest_run_at: '2026-09-14T10:00:00Z' },
  ],
  datasets_attention: [{ dataset_id: 'ds-1', dataset_name: 'Pedidos críticos', domain: 'Finanzas', criticality: 'CRITICAL', health_score: 80, findings: 4, open_exceptions: 2, trend_delta: -10, href: '/datasets/ds-1', action_href: '/runs/run-1', last_run: { id: 'run-1', module: 'intake', decision: 'REJECTED', created_at: '2026-09-14T12:00:00Z' } }],
  recent_runs: [{ id: 'run-1', name: 'Validación pedidos', dataset_id: 'ds-1', dataset_name: 'Pedidos críticos', module: 'intake', status: 'SUCCESS', decision: 'REJECTED', processed_records: 100, finding_count: 4, duration_seconds: 65, created_at: '2026-09-14T12:00:00Z' }],
  filter_options: {
    periods: ['7d', '30d', '90d', 'all'],
    datasets: [{ id: 'ds-1', name: 'Pedidos críticos' }],
  },
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockResolvedValue(response)
})

describe('Operational control center', () => {
  it('prioritizes actionable issues, health and enriched recent runs', async () => {
    renderApp(<Dashboard/>)

    expect(await screen.findByRole('heading', { name: 'Centro de control' })).toBeVisible()
    expect(screen.getByText('Salud general')).toBeVisible()
    expect(screen.getAllByText('82,5%').length).toBeGreaterThan(0)
    expect(screen.getByRole('heading', { name: 'Requiere tu atención' })).toBeVisible()
    expect(screen.getByText('Filas rechazadas')).toBeVisible()
    expect(screen.getByRole('link', { name: /Gestionar excepción/ })).toHaveAttribute('href', '/exceptions?id=exception-1')
    expect(screen.getByRole('img', { name: 'Evolución de salud por módulo' })).toBeVisible()
    expect(screen.getByRole('heading', { name: 'Datasets que necesitan atención' })).toBeVisible()
    expect(screen.getAllByRole('link', { name: 'Pedidos críticos' }).some(link => link.getAttribute('href') === '/datasets/ds-1')).toBe(true)
    expect(screen.getAllByRole('columnheader', { name: 'Hallazgos' }).length).toBeGreaterThanOrEqual(2)
    expect(screen.getByRole('columnheader', { name: 'Duración' })).toBeVisible()
    expect(screen.getByText('1 min 05 s')).toBeVisible()
    expect(screen.getByText('5 ejecuciones')).toBeVisible()
    expect(screen.getByText('3 conciliaciones')).toBeVisible()
    expect(screen.getByText('2 monitores')).toBeVisible()
  })

  it('applies every global filter to the dashboard request and can reset them', async () => {
    const user = userEvent.setup()
    renderApp(<Dashboard/>)
    await screen.findByRole('heading', { name: 'Centro de control' })
    expect(api).toHaveBeenCalledWith('/dashboard?period=30d')

    await user.selectOptions(screen.getByLabelText('Filtrar por periodo'), '7d')
    await user.selectOptions(screen.getByLabelText('Filtrar por dataset'), 'ds-1')
    await user.selectOptions(screen.getByLabelText('Filtrar por módulo'), 'intake')
    await user.selectOptions(screen.getByLabelText('Filtrar por estado'), 'ATTENTION')
    await user.selectOptions(screen.getByLabelText('Filtrar por criticidad'), 'CRITICAL')

    await waitFor(() => expect(api).toHaveBeenLastCalledWith('/dashboard?period=7d&dataset_id=ds-1&module=intake&status=ATTENTION&criticality=CRITICAL'))
    await user.click(screen.getByRole('button', { name: 'Restablecer filtros' }))
    await waitFor(() => expect(api).toHaveBeenLastCalledWith('/dashboard?period=30d'))
  })
})
