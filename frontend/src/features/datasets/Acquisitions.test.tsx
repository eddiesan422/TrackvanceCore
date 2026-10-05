import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, uploadBinary } from '../../api/client'
import { renderApp } from '../../test/render'
import { acquisitionLimitsFixture } from '../../test/acquisitionLimits'
import { governanceFixture } from '../../test/governance'
import { AcquisitionDialog, AcquisitionHistory, AcquisitionStatus } from './Acquisitions'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), uploadBinary: vi.fn() }))
const queued = { id: 'acq-one', dataset_id: 'orders', source_type: 'UPLOAD', filename: 'orders.csv', status: 'QUEUED', stage: 'QUEUED', attempts: 0, processed_rows: 0, processed_bytes: 0, total_rows: null, total_bytes: 16, duration_seconds: null, cancel_requested: false, output_version_id: null, error_code: null, error_message: null, initiated_by: 'Tester', created_at: '2026-10-03T12:00:00Z', started_at: null, finished_at: null }
const received = { upload: { id: 'received-file', filename: 'orders.csv', size_bytes: 16, transfer_complete: true }, inspection: { format: 'CSV', format_label: 'CSV delimitado', filename: 'orders.csv', row_count: null, sampled_rows: 2, sheets: [], selected_sheet: null, detected_delimiter: ',', reader_options: { delimiter: ',' }, columns: [{ name: 'id', logical_type: 'STRING', semantic_tag: 'IDENTIFIER' }, { name: 'amount', logical_type: 'DECIMAL' }] } }
function mockApi(value: Record<string, unknown> | ((path: string, options?: RequestInit) => Promise<Record<string, unknown>>)) {
  vi.mocked(api).mockImplementation(async (path, options) => acquisitionLimitsFixture(path) || governanceFixture(path) || (typeof value === 'function' ? value(path, options) : value))
}

beforeEach(() => { vi.clearAllMocks(); mockApi({}); vi.mocked(uploadBinary).mockResolvedValue(received) })

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
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  })

  it('receives untouched raw bytes before registering a confirmed request with a retry key', async () => {
    mockApi(queued)
    const close = vi.fn(), user = userEvent.setup()
    renderApp(<AcquisitionDialog open onClose={close} datasetId="orders" datasetName="Pedidos"/>, { permissions: ['datasets:write'] })
    const file = new File(['id,amount\n001,2\n'], 'orders.csv', { type: 'text/csv' })
    await user.upload(screen.getByLabelText('Archivo'), file)
    await screen.findByText(/Transferencia completa/)
    expect(uploadBinary).toHaveBeenCalledWith('/datasets/uploads/stage?filename=orders.csv', file, expect.any(Function), expect.any(AbortSignal))
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
    await user.selectOptions(screen.getByLabelText('Tipo de amount'), 'DECIMAL')
    await user.click(screen.getByRole('button', { name: 'Registrar adquisición' }))
    await waitFor(() => expect(close).toHaveBeenCalledOnce())
    const [path, options] = vi.mocked(api).mock.calls.find(([path]) => path === '/datasets/orders/acquisitions')!
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
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  })

  it('shows persisted progress and sends cooperative cancellation without inventing an output', async () => {
    mockApi(async (_path, options) => options?.method === 'POST' ? { ...queued, status: 'RUNNING', cancel_requested: true } : { ...queued, status: 'RUNNING', stage: 'PROFILING', processed_rows: 100001, processed_bytes: 1234567 })
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
    mockApi({ ...queued, status: 'SUCCESS', stage: 'COMPLETED', output_version_id: 'version-real' })
    const view = renderApp(<AcquisitionStatus id="acq-one" onCompleted={completed} onFinished={finished}/>)
    await waitFor(() => expect(completed).toHaveBeenCalledWith('version-real'))
    expect(finished).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: 'Cancelar adquisición' })).not.toBeInTheDocument()
    view.unmount()
    mockApi({ ...queued, id: 'failed', status: 'FAILED', stage: 'FAILED', error_code: 'SOURCE_TIMEOUT', error_message: 'La fuente excedió su tiempo.' })
    renderApp(<AcquisitionStatus id="failed" onCompleted={completed} onFinished={finished}/>)
    expect(await screen.findByText(/SOURCE_TIMEOUT/)).toBeInTheDocument()
    expect(completed).toHaveBeenCalledOnce()
    expect(finished).toHaveBeenCalledTimes(2)
  })

  it('reloads persisted history after navigation and links the immutable output', async () => {
    mockApi({ items: [{ ...queued, status: 'SUCCESS', output_version_id: 'version-real' }], total: 1 })
    renderApp(<AcquisitionHistory datasetId="orders"/>, { permissions: ['datasets:read'] })
    expect(await screen.findByRole('link', { name: 'Ver versión' })).toHaveAttribute('href', '/datasets/orders?version=version-real')
    expect(api).toHaveBeenCalledWith('/acquisitions?limit=100&dataset_id=orders')
  })

  it('shows backend XLSX limits and registers a bounded partial inspection without pretending it is empty', async () => {
    const descriptor = acquisitionLimitsFixture('/acquisitions/limits?format=XLSX&route=ASYNC_ACQUISITION')!
    descriptor.inspection_limited = true
    descriptor.header_row_number = 4
    vi.mocked(uploadBinary).mockResolvedValue({ ...received, inspection: { ...received.inspection,
      format: 'XLSX', format_label: 'Excel XLSX', sampled_rows: 0, columns: [], sheets: ['Datos'], selected_sheet: 'Datos', reader_options: { sheet_name: 'Datos' }, effective_limits: descriptor } })
    mockApi(queued)
    const user = userEvent.setup(), close = vi.fn()
    renderApp(<AcquisitionDialog open onClose={close} datasetId="orders" datasetName="Pedidos"/>)
    await user.upload(screen.getByLabelText('Archivo'), new File(['xlsx'], 'orders.xlsx'))
    expect(await screen.findByText(/La inspección preliminar está limitada/)).toBeInTheDocument()
    expect(screen.getByText('1.000.000 registros')).toBeInTheDocument()
    expect(screen.getByText('4 GiB')).toBeInTheDocument()
    expect(screen.getByText(/Encabezado observado en la fila 4/)).toBeInTheDocument()
    expect(screen.queryByText(/archivo vacío/i)).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Registrar adquisición' }))
    await waitFor(() => expect(close).toHaveBeenCalledOnce())
    const call = vi.mocked(api).mock.calls.find(([path]) => path === '/datasets/orders/acquisitions')!
    expect(JSON.parse(String(call[1]?.body)).column_overrides).toEqual({})
  })

  it('persists a controlled optional classification once and reuses the same dataset and request key when registration is retried', async () => {
    let attempts = 0
    mockApi(async path => {
      if (path === '/datasets') return { id: 'risk-events' }
      if (++attempts === 1) throw new Error('La adquisición no se registró. Intenta de nuevo.')
      return { ...queued, dataset_id: 'risk-events' }
    })
    const close = vi.fn(), user = userEvent.setup()
    renderApp(<AcquisitionDialog open onClose={close} existingDatasets={[{ id: 'existing', name: 'Other', domain: 'Comercial' }]}/>)
    await user.upload(screen.getByLabelText('Archivo'), new File(['fixture'], 'risk_events.csv'))
    await screen.findByText(/Transferencia completa/)
    await user.selectOptions(screen.getByLabelText('Macrodominio (opcional)'), 'macro-risk')
    await screen.findByRole('option', { name: 'Crédito' })
    await user.selectOptions(screen.getByLabelText('Dominio (opcional)'), 'domain-credit')
    await user.click(screen.getByRole('button', { name: 'Registrar adquisición' }))
    await screen.findByText('La adquisición no se registró. Intenta de nuevo.')
    expect(screen.getByLabelText('Nombre del dataset')).toBeDisabled()
    expect(screen.getByText(/La clasificación vigente pertenece al dataset/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Registrar adquisición' }))
    await waitFor(() => expect(close).toHaveBeenCalledOnce())
    const creates = vi.mocked(api).mock.calls.filter(([path]) => path === '/datasets')
    expect(creates).toHaveLength(1)
    expect(JSON.parse(String(creates[0][1]?.body))).toEqual({ name: 'risk events', macro_domain_id: 'macro-risk', domain_id: 'domain-credit' })
    const registrations = vi.mocked(api).mock.calls.filter(([path]) => path === '/datasets/risk-events/acquisitions')
    expect(registrations).toHaveLength(2)
    expect(registrations[0][1]?.headers).toEqual(registrations[1][1]?.headers)
  })

  it('shows the exact inherited area for matching names and fixed new versions', async () => {
    const user = userEvent.setup()
    const view = renderApp(<AcquisitionDialog open onClose={vi.fn()} existingDatasets={[{ id: 'orders', name: 'Pedidos', domain: 'Área histórica' }]}/>)
    await user.type(screen.getByLabelText('Nombre del dataset'), '  PEDIDOS  ')
    expect(screen.getByText(/Área heredada: Área histórica/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Área de negocio')).not.toBeInTheDocument()
    view.unmount()
    renderApp(<AcquisitionDialog open onClose={vi.fn()} datasetId="orders" datasetName="Pedidos" datasetDomain="Área histórica"/>)
    expect(screen.getByText(/Área heredada: Área histórica/)).toBeInTheDocument()
    expect(screen.queryByLabelText('Área de negocio')).not.toBeInTheDocument()
  })

  it('retains a known persisted failure, separates counters and does not infer a total from zero materialized rows', async () => {
    const message = 'La fuente supera el límite efectivo de 1,000,000 registros de datos. No se publicó una versión parcial.'
    mockApi({ items: [{ ...queued, status: 'FAILED', stage: 'FAILED', received_bytes: 2048,
      materialized_rows: 0, materialized_bytes: 0, published_rows: null,
      error: { code: 'ACQUISITION_ROW_LIMIT', message, details: { limit: 'data_rows', maximum: 1000000 }, reference: 'safe-reference' } }], total: 1 })
    renderApp(<AcquisitionHistory datasetId="orders"/>, { permissions: ['datasets:read'] })
    expect(await screen.findByText(message, { exact: false })).toBeInTheDocument()
    expect(screen.getByText(/Referencia: safe-reference/)).toBeInTheDocument()
    expect(screen.getByText(/0 registros materializados/)).toBeInTheDocument()
    expect(screen.getByText(/no determinan el total de registros de la fuente/)).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: 'Ver versión' })).not.toBeInTheDocument()
  })

  it('does not start a raw transfer after closing while effective limits are still pending', async () => {
    let finish = () => {}
    vi.mocked(api).mockImplementation(() => new Promise(resolve => { finish = () => resolve(acquisitionLimitsFixture('/acquisitions/limits?format=CSV&route=ASYNC_ACQUISITION')!) }))
    const user = userEvent.setup()
    const view = renderApp(<AcquisitionDialog open onClose={vi.fn()} datasetId="orders" datasetName="Pedidos"/>)
    await user.upload(screen.getByLabelText('Archivo'), new File(['fixture'], 'orders.csv'))
    view.unmount()
    await act(async () => { finish(); await Promise.resolve() })
    expect(uploadBinary).not.toHaveBeenCalled()
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  })

  it.each(['XLSX', 'JSON'])('rejects an oversized %s before sending raw bytes using the backend limit', async format => {
    vi.mocked(api).mockImplementation(async path => {
      const descriptor = acquisitionLimitsFixture(path)
      if (descriptor) descriptor.limits.compressed_bytes.max = 4
      return descriptor || {}
    })
    const user = userEvent.setup()
    renderApp(<AcquisitionDialog open onClose={vi.fn()} datasetId="orders" datasetName="Pedidos"/>)
    await user.upload(screen.getByLabelText('Archivo'), new File(['oversized'], `orders.${format.toLowerCase()}`))
    expect(await screen.findByText(/El archivo supera el máximo configurado para adquisición: 4 bytes/)).toBeInTheDocument()
    expect(uploadBinary).not.toHaveBeenCalled()
    expect(vi.mocked(api).mock.calls.some(([, options]) => options?.method === 'POST')).toBe(false)
  })

  it('preserves JSON Lines in a .json filename instead of assuming the tabular JSON cap', async () => {
    vi.mocked(api).mockImplementation(async path => {
      const descriptor = acquisitionLimitsFixture(path)
      if (descriptor?.format === 'JSON') descriptor.limits.compressed_bytes.max = 4
      return descriptor || {}
    })
    vi.mocked(uploadBinary).mockResolvedValue({ ...received, inspection: { ...received.inspection, format: 'JSON_LINES' } })
    const user = userEvent.setup()
    renderApp(<AcquisitionDialog open onClose={vi.fn()} datasetId="orders" datasetName="Pedidos"/>)
    const file = new File(['{"id":"001"}\n{"id":"002"}\n'], 'orders.json')
    await user.upload(screen.getByLabelText('Archivo'), file)
    await screen.findByText(/Transferencia completa/)
    expect(uploadBinary).toHaveBeenCalledWith('/datasets/uploads/stage?filename=orders.json', file, expect.any(Function), expect.any(AbortSignal))
  })
})
