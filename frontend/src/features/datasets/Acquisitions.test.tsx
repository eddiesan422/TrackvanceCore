import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, uploadBinary } from '../../api/client'
import { renderApp } from '../../test/render'
import { AcquisitionDialog, AcquisitionHistory, AcquisitionStatus } from './Acquisitions'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), uploadBinary: vi.fn() }))
const queued = { id: 'acq-one', dataset_id: 'orders', source_type: 'UPLOAD', filename: 'orders.csv', status: 'QUEUED', stage: 'QUEUED', attempts: 0, processed_rows: 0, processed_bytes: 0, total_rows: null, total_bytes: 16, duration_seconds: null, cancel_requested: false, output_version_id: null, error_code: null, error_message: null, initiated_by: 'Tester', created_at: '2026-10-03T12:00:00Z', started_at: null, finished_at: null }
const received = { upload: { id: 'received-file', filename: 'orders.csv', size_bytes: 16, transfer_complete: true }, inspection: { format: 'CSV', format_label: 'CSV delimitado', filename: 'orders.csv', row_count: null, sampled_rows: 2, sheets: [], selected_sheet: null, detected_delimiter: ',', reader_options: { delimiter: ',' }, columns: [{ name: 'id', logical_type: 'STRING', semantic_tag: 'IDENTIFIER' }, { name: 'amount', logical_type: 'DECIMAL' }] } }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(uploadBinary).mockResolvedValue(received) })

describe('Durable asynchronous acquisition', () => {
  it.each([
    ['CSV', 'CSV', 'orders.csv'],
    ['XLSX', 'Excel XLSX', 'orders.xlsx'],
    ['JSON', 'JSON tabular', 'orders.json'],
    ['PARQUET', 'Apache Parquet', 'orders.parquet'],
    ['TXT', 'TXT delimitado', 'orders.txt'],
  ])('shows the detected %s label beside its inspected sample', async (format, formatLabel, filename) => {
    vi.mocked(uploadBinary).mockResolvedValue({ ...received, inspection: { ...received.inspection, format, format_label: formatLabel } })
    const user = userEvent.setup()
    renderApp(<AcquisitionDialog open onClose={vi.fn()} datasetId="orders" datasetName="Pedidos"/>)
    await user.upload(screen.getByLabelText('Archivo'), new File(['fixture'], filename))
    expect(await screen.findByText(`${formatLabel} · muestra de 2 registros`)).toBeInTheDocument()
    expect(api).not.toHaveBeenCalled()
  })

  it('receives untouched raw bytes before registering a confirmed request with a retry key', async () => {
    vi.mocked(api).mockResolvedValue(queued)
    const close = vi.fn(), user = userEvent.setup()
    renderApp(<AcquisitionDialog open onClose={close} datasetId="orders" datasetName="Pedidos"/>, { permissions: ['datasets:write'] })
    const file = new File(['id,amount\n001,2\n'], 'orders.csv', { type: 'text/csv' })
    await user.upload(screen.getByLabelText('Archivo'), file)
    await screen.findByText(/Transferencia completa/)
    expect(uploadBinary).toHaveBeenCalledWith('/datasets/uploads/stage?filename=orders.csv', file, expect.any(Function), expect.any(AbortSignal))
    expect(api).not.toHaveBeenCalled()
    await user.selectOptions(screen.getByLabelText('Tipo de amount'), 'DECIMAL')
    await user.click(screen.getByRole('button', { name: 'Registrar adquisición' }))
    await waitFor(() => expect(close).toHaveBeenCalledOnce())
    const [path, options] = vi.mocked(api).mock.calls[0]
    expect(path).toBe('/datasets/orders/acquisitions')
    expect(options?.headers).toEqual({ 'Idempotency-Key': expect.any(String) })
    expect(JSON.parse(String(options?.body))).toEqual({ upload_id: 'received-file', reader_options: { delimiter: ',' }, column_overrides: { amount: { logical_type: 'DECIMAL' } } })
  })

  it('keeps registration disabled while transfer is pending and closing aborts only transfer', async () => {
    vi.mocked(uploadBinary).mockImplementation(() => new Promise(() => {}))
    const user = userEvent.setup(), view = renderApp(<AcquisitionDialog open onClose={vi.fn()} datasetId="orders" datasetName="Pedidos"/>)
    await user.upload(screen.getByLabelText('Archivo'), new File(['id\n1'], 'orders.csv'))
    expect(await screen.findByText('Recibiendo archivo…')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Registrar adquisición' })).toBeDisabled()
    const signal = vi.mocked(uploadBinary).mock.calls[0][3]!
    view.unmount()
    expect(signal.aborted).toBe(true)
    expect(api).not.toHaveBeenCalled()
  })

  it('shows persisted progress and sends cooperative cancellation without inventing an output', async () => {
    vi.mocked(api).mockImplementation(async (_path, options) => options?.method === 'POST' ? { ...queued, status: 'RUNNING', cancel_requested: true } : { ...queued, status: 'RUNNING', stage: 'PROFILING', processed_rows: 100001, processed_bytes: 1234567 })
    const completed = vi.fn(), user = userEvent.setup()
    renderApp(<AcquisitionStatus id="acq-one" onCompleted={completed}/>, { permissions: ['datasets:write'] })
    expect(await screen.findByText(/Perfilando toda la población/)).toBeInTheDocument()
    expect(screen.getByText(/Puedes salir de esta pantalla/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cancelar adquisición' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/acquisitions/acq-one/cancel', { method: 'POST' }))
    expect(await screen.findByRole('button', { name: 'Cancelación solicitada…' })).toBeDisabled()
    expect(completed).not.toHaveBeenCalled()
  })

  it('selects only an actually published successful version and allows a failed request to finish', async () => {
    const completed = vi.fn(), finished = vi.fn()
    vi.mocked(api).mockResolvedValue({ ...queued, status: 'SUCCESS', stage: 'COMPLETED', output_version_id: 'version-real' })
    const view = renderApp(<AcquisitionStatus id="acq-one" onCompleted={completed} onFinished={finished}/>)
    await waitFor(() => expect(completed).toHaveBeenCalledWith('version-real'))
    expect(finished).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: 'Cancelar adquisición' })).not.toBeInTheDocument()
    view.unmount()
    vi.mocked(api).mockResolvedValue({ ...queued, id: 'failed', status: 'FAILED', stage: 'FAILED', error_code: 'SOURCE_TIMEOUT', error_message: 'La fuente excedió su tiempo.' })
    renderApp(<AcquisitionStatus id="failed" onCompleted={completed} onFinished={finished}/>)
    expect(await screen.findByText(/SOURCE_TIMEOUT/)).toBeInTheDocument()
    expect(completed).toHaveBeenCalledOnce()
    expect(finished).toHaveBeenCalledTimes(2)
  })

  it('reloads persisted history after navigation and links the immutable output', async () => {
    vi.mocked(api).mockResolvedValue({ items: [{ ...queued, status: 'SUCCESS', output_version_id: 'version-real' }], total: 1 })
    renderApp(<AcquisitionHistory datasetId="orders"/>, { permissions: ['datasets:read'] })
    expect(await screen.findByRole('link', { name: 'Ver versión' })).toHaveAttribute('href', '/datasets/orders?version=version-real')
    expect(api).toHaveBeenCalledWith('/acquisitions?limit=100&dataset_id=orders')
  })
})
