import { screen, within } from '@testing-library/react'
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
    expect(within(screen.getByText('Filas duplicadas exactas').closest('div')!).getByText('No disponible')).toBeInTheDocument()
    expect(screen.getByText(/Los controles conformes no equivalen a filas válidas/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Informativo (1)' }))
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
  it('reconciles actual control categories without counting informational observations as successes', async () => {
    vi.mocked(api).mockResolvedValue({ indicators: { row_count: 100000, column_count: 1 }, columns: [], evolution: [], checks: [
      { code: 'VALID', name: 'Control conforme', status: 'PASS' },
      { code: 'STALE', name: 'Actualización', status: 'FAIL', severity: 'WARNING', actual: 62.13, expected: { max_hours: 48 } },
      { code: 'FRESHNESS', name: 'Antigüedad', status: 'FAIL', actual: 62.13, expected: { max_hours: 48 } },
      { code: 'MISSING', name: 'Dato pendiente', status: 'UNAVAILABLE', actual: null },
      { code: 'BASE_NULLS', name: 'Nulos informativos', kind: 'OBSERVATION', status: 'PASS', actual: 10000, expected: 'Observación exacta' },
    ] })
    const user = userEvent.setup()
    renderApp(<SentinelVisualSummary runId="mixed"/>)
    expect(await screen.findByRole('img', { name: 'Conforme: 1; Advertencia: 1; Incumplido: 1; No evaluado: 1' })).toBeInTheDocument()
    expect(screen.getByText('4 controles · 1 observación informativa fuera del total')).toBeInTheDocument()
    expect(within(screen.getByText('Controles conformes').closest('div')!).getByText('1')).toBeInTheDocument()
    expect(screen.getByText('100.000')).toBeInTheDocument()
    expect(screen.getByText('62,13 horas')).toBeInTheDocument()
    expect(screen.getAllByText('Máximo: 48 horas')).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Advertencia (1)' }))
    expect(screen.getByRole('heading', { name: 'Actualización' })).toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Control conforme' })).not.toBeInTheDocument()
    await user.click(screen.getByText('Detalle técnico del control'))
    expect(screen.getByText(/"max_hours": 48/)).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })
  it('keeps wide profiles explicitly partial and displays null counts, percentages, denominator and evaluated limits', async () => {
    vi.mocked(api).mockResolvedValue({ indicators: { row_count: 10, column_count: 15 }, checks: [], evolution: [],
      columns: Array.from({ length: 15 }, (_, index) => ({ name: `columna_${index}`, logical_type: 'STRING', null_count: index === 0 ? 2 : 0,
        null_denominator: 10, distinct_count: 2, distinct_denominator: index === 0 ? 8 : 10, distinct_rate: 0.25, uniqueness_ratio: 0,
        null_threshold_checks: index === 0 ? [{ code: 'NULL_RATE_COLUMNA_0', expected: { max: 0.1 } }] : [] })) })
    const user = userEvent.setup()
    renderApp(<SentinelVisualSummary runId="wide"/>)
    expect(await screen.findByText(/Vista parcial: 12 de 15 columnas/)).toBeInTheDocument()
    expect(screen.getByRole('img', { name: 'columna_0: 2 nulos · 20 % de 10 filas' })).toBeInTheDocument()
    expect(screen.getByText('Máximo: 10 %')).toBeInTheDocument()
    expect(screen.getAllByText('Denominador: 10 filas')).toHaveLength(12)
    expect(screen.queryByRole('heading', { name: 'columna_14' })).not.toBeInTheDocument()
    await user.click(screen.getByText('Seleccionar columnas (12 de 15)'))
    await user.click(screen.getByRole('button', { name: 'Todas las columnas' }))
    expect(screen.getByText('Vista de todas las columnas: 15.')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'columna_14' })).toBeInTheDocument()
    await user.click(screen.getByLabelText('columna_0'))
    expect(screen.getByText(/Vista parcial: 14 de 15/)).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /columna_0: 2 nulos/ })).not.toBeInTheDocument()
    expect(api).toHaveBeenCalledTimes(1)
  })
  it('preserves no available percentages for empty populations and labels zero as a measured count', async () => {
    vi.mocked(api).mockResolvedValue({ indicators: { row_count: 0, duplicate_row_count: null }, checks: [], evolution: [],
      columns: [{ name: 'empty', null_count: 0, null_denominator: 0, distinct_count: 0, distinct_denominator: 0, null_rate: null, distinct_rate: null, uniqueness_ratio: null }] })
    renderApp(<SentinelVisualSummary runId="empty"/>)
    expect(await screen.findByRole('img', { name: 'empty: 0 nulos · No disponible de 0 filas' })).toBeInTheDocument()
    expect(within(screen.getByText('Filas de la versión').closest('div')!).getByText('0')).toBeInTheDocument()
    expect(screen.queryByText('0 %')).not.toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Evolución compatible/ })).not.toBeInTheDocument()
  })
  it('does not infer zero conforming controls when historical control evidence is missing', async () => {
    vi.mocked(api).mockResolvedValue({ indicators: {}, checks: [], checks_available: false, columns: [], evolution: [] })
    renderApp(<SentinelVisualSummary runId="unavailable"/>)
    expect(await screen.findByRole('heading', { name: 'Controles no disponibles' })).toBeInTheDocument()
    expect(within(screen.getByText('Controles conformes').closest('div')!).getByText('No disponible')).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: /Conforme:/ })).not.toBeInTheDocument()
  })
  it('selects distinct historical dimensions with readable metric names and retains exact values', async () => {
    const series = (column: string, value: number) => ({ metric_key: `null_rate:${column}`, dimensions: { column }, method: 'EXACT_OBSERVED', metric_definition_version: 2,
      points: [{ run_id: 'first', dataset_version_id: 'v1', observed_at: '2026-10-01T00:00:00Z', value, exact_value: String(value) },
        { run_id: 'second', dataset_version_id: 'v2', observed_at: '2026-10-02T00:00:00Z', value, exact_value: String(value) }] })
    vi.mocked(api).mockResolvedValue({ indicators: {}, checks: [], columns: [], evolution: [series('a', 0.25), series('b', 0.5)] })
    const user = userEvent.setup()
    renderApp(<SentinelVisualSummary runId="history"/>)
    expect(await screen.findByRole('img', { name: 'Evolución compatible de Proporción de nulos · a' })).toBeInTheDocument()
    const metric = screen.getByLabelText('Métrica de evolución')
    await user.selectOptions(metric, within(metric).getByRole('option', { name: 'Proporción de nulos · b · Medición exacta v2' }))
    expect(screen.getByRole('img', { name: 'Evolución compatible de Proporción de nulos · b' })).toBeInTheDocument()
    expect(screen.queryByRole('img', { name: 'Evolución compatible de Proporción de nulos · a' })).not.toBeInTheDocument()
    await user.click(screen.getByText('Observaciones y valores exactos de la evolución'))
    expect(screen.getAllByText('0.5')).toHaveLength(2)
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
  it('presents automatic freshness reviews with hour units while retaining their exact value', async () => {
    vi.mocked(api).mockResolvedValue({ id: 'auto', version: 1, enabled: true, review_interval_seconds: 86400, state: 'ACTIVE',
      reviews: [{ id: 'review', status: 'STALE', observed_at: '2026-10-01T00:00:00Z', dataset_version_id: 'v1', metrics: { age_hours: 62.133333333 } }] })
    const user = userEvent.setup()
    renderApp(<AutomaticMonitorPanel monitorId="monitor"/>)
    await user.click(await screen.findByText('Monitoreo automático por dataset'))
    await user.click(screen.getByText('Revisiones ligeras recientes'))
    expect(screen.getByText('62,13 horas')).toBeInTheDocument()
    await user.click(screen.getByText('Valor exacto'))
    expect(screen.getByText('62.133333333')).toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })
})
