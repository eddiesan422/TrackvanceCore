import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, download } from '../../api/client'
import { renderApp } from '../../test/render'
import { RunDetail } from './Runs'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), download: vi.fn(), post: vi.fn() }))

function mockRun(status = 'SUCCESS') {
  vi.mocked(api).mockImplementation(async path => path.includes('/results?') ? { items: [{ original_row_number: 5, rule_code: 'REQUIRED', column: 'customer_id', received_value: null, severity: 'ERROR', message: 'Dato obligatorio' }], total: 1 } : {
    id: 'run-123', module: 'intake', name: 'Validación de clientes', status, decision: status === 'SUCCESS' ? 'REJECTED' : null,
    metrics: { total_rows: 120, valid_rows: 110, error_rows: 10, acceptance_rate: 91.67 }, findings: [],
  })
}
const renderRun = (permissions?: string[]) => renderApp(<RunDetail/>, { path: '/runs/run-123', route: '/runs/:id', permissions })

describe('Run evidence actions', () => {
  beforeEach(() => { mockRun(); vi.mocked(download).mockResolvedValue(undefined) })

  it('exports an Excel report and separates completed status from business rejection', async () => {
    const user = userEvent.setup()
    renderRun()
    await user.click(await screen.findByRole('button', { name: 'Exportar Excel' }))
    expect(download).toHaveBeenCalledWith('/runs/run-123/export.xlsx', 'trackvance_intake_run-123.xlsx')
    expect(screen.queryByRole('button', { name: 'Exportar CSV' })).not.toBeInTheDocument()
    expect(screen.getByText('Completada')).toBeInTheDocument()
    expect(screen.getByText('Rechazado')).toBeInTheDocument()
    expect(await screen.findByRole('columnheader', { name: 'Línea del archivo' })).toBeInTheDocument()
    expect(screen.getByText('REQUIRED')).toBeInTheDocument()
  })

  it('prevents duplicate downloads while preparing the workbook', async () => {
    let complete!: () => void
    vi.mocked(download).mockImplementation(() => new Promise(resolve => { complete = resolve }))
    const user = userEvent.setup()
    renderRun()
    await user.click(await screen.findByRole('button', { name: 'Exportar Excel' }))
    expect(await screen.findByRole('button', { name: 'Preparando descarga…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Manifiesto' })).toBeDisabled()
    await act(async () => { complete() })
    await waitFor(() => expect(screen.getByRole('button', { name: 'Exportar Excel' })).toBeEnabled())
  })

  it('shows download failures and leaves retry available', async () => {
    vi.mocked(download).mockRejectedValue(new Error('No se pudo generar el informe Excel.'))
    const user = userEvent.setup()
    renderRun()
    await user.click(await screen.findByRole('button', { name: 'Exportar Excel' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('No se pudo generar el informe Excel.')
    expect(screen.getByRole('button', { name: 'Exportar Excel' })).toBeEnabled()
  })

  it('requires export and artifact download permissions independently', async () => {
    renderRun(['artifacts:download'])
    expect(await screen.findByRole('button', { name: 'Exportar Excel' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Manifiesto' })).toBeEnabled()
  })

  it.each(['RUNNING', 'QUEUED', 'FAILED', 'CANCELLED'])('disables report downloads for %s execution', async status => {
    mockRun(status)
    renderRun()
    expect(await screen.findByRole('button', { name: 'Exportar Excel' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Manifiesto' })).toBeDisabled()
  })

  it('shows initial loading and an actionable request failure', async () => {
    let fail!: (error: Error) => void
    vi.mocked(api).mockImplementation(() => new Promise((_, reject) => { fail = reject }))
    renderRun()
    expect(screen.getByRole('status')).toHaveTextContent('Cargando ejecución y evidencia')
    await act(async () => { fail(new Error('Servicio local no disponible')) })
    expect(await screen.findByRole('alert')).toHaveTextContent('Servicio local no disponible')
    expect(screen.getByRole('button', { name: 'Volver a intentar' })).toBeInTheDocument()
  })

  it('retains null, empty text and physical line references in Recon results', async () => {
    vi.mocked(api).mockImplementation(async path => path.includes('/results?') ? {
      items: [{ key: 'TX001', classification: 'VALUE_MISMATCH', source_value: null, target_value: '', source_row: 12, target_row: 14, message: 'Valores distintos' }], total: 1,
    } : { id: 'run-123', module: 'recon', name: 'Cruce de clientes', status: 'SUCCESS', decision: 'WITH_FINDINGS', metrics: {}, findings: [] })
    renderRun()
    expect(await screen.findByText('TX001')).toBeInTheDocument()
    expect(screen.getByText('null')).toHaveClass('null-value')
    expect(screen.getByText('"" (texto vacío)')).toHaveClass('empty-value')
    expect(screen.getByRole('cell', { name: '12' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '14' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Línea origen' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: 'Línea destino' })).toBeInTheDocument()
    expect(screen.getByText('VALUE_MISMATCH')).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: /Diferencia de valor VALUE_MISMATCH/ })).toBeInTheDocument()
    expect(screen.getByText('Completada')).toBeInTheDocument()
    expect(screen.getByText('Con hallazgos')).toBeInTheDocument()
  })

  it('identifies Intake rows from a derived version as one-based records without applying a header offset', async () => {
    vi.mocked(api).mockImplementation(async path => path.includes('/results?') ? {
      items: [{ original_row_number: 1, rule_code: 'REQUIRED', column: 'customer_id', received_value: null, severity: 'ERROR', message: 'Dato obligatorio' }], total: 1,
    } : { id: 'run-123', module: 'intake', name: 'Validación derivada', status: 'SUCCESS', decision: 'REJECTED', metrics: { source_row_numbering: 'RECORD_NUMBER' }, findings: [] })
    renderRun()
    expect(await screen.findByRole('columnheader', { name: 'Registro de la versión' })).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Línea del archivo' })).not.toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '1' })).toBeInTheDocument()
    expect(screen.getByText('Hallazgos por registro de la versión y regla')).toBeInTheDocument()
  })

  it.each([
    ['RECORD_NUMBER', 'PHYSICAL_LINE', 'Registro origen', 'Línea destino'],
    ['PHYSICAL_LINE', 'RECORD_NUMBER', 'Línea origen', 'Registro destino'],
  ])('uses independent Recon row references for source %s and target %s', async (sourceNumbering, targetNumbering, sourceHeader, targetHeader) => {
    vi.mocked(api).mockImplementation(async path => path.includes('/results?') ? {
      items: [{ key: 'TX001', classification: 'VALUE_MISMATCH', source_value: '5.00', target_value: '6.00', source_row: 1, target_row: 12, message: 'Importes distintos' }], total: 1,
    } : { id: 'run-123', module: 'recon', name: 'Conciliación de versión derivada y archivo', status: 'SUCCESS', decision: 'WITH_FINDINGS', metrics: { source_row_numbering: sourceNumbering, target_row_numbering: targetNumbering }, findings: [] })
    renderRun()
    expect(await screen.findByRole('columnheader', { name: sourceHeader })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: targetHeader })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '1' })).toBeInTheDocument()
    expect(screen.getByRole('cell', { name: '12' })).toBeInTheDocument()
  })
})
