import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { AutomaticMonitorPanel } from './AutomaticMonitorPanel'
import { SentinelVisualSummary } from './SentinelVisualSummary'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))

describe('Sentinel execution visual evidence', () => {
  beforeEach(() => { vi.clearAllMocks() })
  it('loads only the selected execution and distinguishes unknown data from zero', async () => {
    vi.mocked(api).mockResolvedValue({ run_id: 'old-run', dataset_version_id: 'old-version', dataset_version: 1,
      observed_at: '2026-10-01T00:00:00Z', indicators: { row_count: 3, column_count: 2, duplicate_row_count: null, conforming_checks: 2 },
      columns: [{ name: 'category', logical_type: 'STRING', null_count: 0, distinct_count: 1, null_denominator: 3, distinct_denominator: 3, distinct_rate: '0.3333333333333333', uniqueness_ratio: 0 }],
      checks: [{ code: 'NULLS', name: 'Nulos informativos', status: 'PASS', kind: 'OBSERVATION', actual: 0, expected: 'Observado', message: 'Sin regla obligatoria' }],
      exact_metrics: [{ metric_key: 'row_count', dimensions: {}, exact_value: '9007199254740993', method: 'EXACT_OBSERVED', metric_definition_version: 2 }], evolution: [] })
    const user = userEvent.setup()
    renderApp(<SentinelVisualSummary runId="old-run"/>)
    expect(await screen.findByText('old-version')).toBeInTheDocument()
    expect(screen.getByText('No disponible')).toBeInTheDocument()
    expect(screen.getByText(/Los controles conformes no equivalen a filas válidas/)).toBeInTheDocument()
    expect(screen.getByText('Informativo')).toBeInTheDocument()
    await user.click(screen.getByText('Tabla de métricas exactas de la ejecución'))
    expect(screen.getByText('9007199254740993')).toBeInTheDocument()
    expect(api).toHaveBeenCalledWith('/runs/old-run/visual-summary')
    expect(post).not.toHaveBeenCalled()
  })
  it('never draws a trend from one compatible observation', async () => {
    vi.mocked(api).mockResolvedValue({ dataset_version: 1, dataset_version_id: 'v1', indicators: {}, columns: [], checks: [], exact_metrics: [],
      evolution: [{ metric_key: 'row_count', method: 'EXACT_OBSERVED', metric_definition_version: 2,
        points: [{ run_id: 'run', dataset_version_id: 'v1', observed_at: '2026-10-01T00:00:00Z', exact_value: '3', value: 3 }] }] })
    renderApp(<SentinelVisualSummary runId="run"/>)
    expect(await screen.findByText(/Se necesitan dos observaciones comparables/)).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Evolución compatible/ })).not.toBeInTheDocument()
  })
  it('keeps waiting state visible and updates pause with the current revision', async () => {
    const automatic = { id: 'auto', version: 2, enabled: true, review_interval_seconds: 86400,
      responsible_user_id: 'admin', state: 'WAITING_FIRST_VERSION', next_review_at: '2026-10-15T00:00:00Z', reviews: [] }
    vi.mocked(api).mockResolvedValue(automatic)
    vi.mocked(post).mockResolvedValue({ ...automatic, version: 3, enabled: false, state: 'PAUSED' })
    const user = userEvent.setup()
    renderApp(<AutomaticMonitorPanel monitorId="monitor"/>)
    expect(await screen.findByText('Esperando primera versión')).toBeInTheDocument()
    await user.click(screen.getByText('Monitoreo automático por dataset'))
    await user.click(screen.getByLabelText('Evaluar nuevas versiones y revisar frescura'))
    await user.click(screen.getByRole('button', { name: 'Guardar monitoreo automático' }))
    expect(post).toHaveBeenCalledWith('/monitors/monitor/automatic', { enabled: false, expected_version: 2, review_interval_seconds: 86400 })
  })
})
