import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { DatasetsPage, UploadDialog } from './Datasets'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))

beforeEach(() => vi.clearAllMocks())

describe('Identifier override during upload', () => {
  it('sends selected identifier tags with the untouched CSV bytes and no manual-name field', async () => {
    vi.mocked(api).mockImplementation(async path => path === '/datasets/uploads/inspect' ? {
      format: 'CSV', format_label: 'CSV delimitado', filename: 'customers.csv', columns: [{ name: 'document_id', logical_type: 'STRING', semantic_tag: 'IDENTIFIER' }], row_count: 2,
    } : { id: 'version' })
    const user = userEvent.setup()
    renderApp(<UploadDialog open onClose={vi.fn()} datasetId="customers" datasetName="Clientes"/>)
    const file = new File(['document_id,name\n001234567,Cliente\n1234567,Otro\n'], 'customers.csv', { type: 'text/csv' })
    await user.upload(screen.getByLabelText('Seleccionar archivo de datos'), file)
    await screen.findByText('CSV delimitado')
    expect(screen.queryByLabelText('Otros identificadores por nombre')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Columnas identificadoras (opcional): abrir selector' }))
    await user.click(screen.getByRole('checkbox', { name: 'document_id (STRING)' }))
    await user.click(screen.getByRole('button', { name: 'Cargar y analizar' }))
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => path === '/datasets/customers/versions/upload')).toBe(true))
    const [path, options] = vi.mocked(api).mock.calls.find(([candidate]) => candidate === '/datasets/customers/versions/upload')!
    expect(path).toBe('/datasets/customers/versions/upload')
    const body = options?.body as FormData
    expect(body.get('file')).toBe(file)
    expect(JSON.parse(String(body.get('column_overrides')))).toEqual({ document_id: { logical_type: 'STRING', semantic_tag: 'IDENTIFIER' } })
    expect(JSON.parse(String(body.get('reader_options')))).toEqual({})
  })

  it('uploads a duplicate dataset name as a new immutable version', async () => {
    vi.mocked(api).mockImplementation(async path => path === '/datasets/uploads/inspect' ? {
      format: 'CSV', format_label: 'CSV delimitado', filename: 'trackvance_dataset_prueba.csv', columns: [{ name: 'order_id', logical_type: 'STRING' }],
    } : { id: 'version-2' })
    const user = userEvent.setup()
    renderApp(<UploadDialog open onClose={vi.fn()} existingDatasets={[{ id: 'orders', name: 'trackvance dataset prueba', version_count: 1 }]}/>)
    const file = new File(['order_id,amount\nA-01,10\n'], 'trackvance_dataset_prueba.csv', { type: 'text/csv' })

    await user.upload(screen.getByLabelText('Seleccionar archivo de datos'), file)
    await screen.findByText('CSV delimitado')

    expect(screen.getByText(/Ya existe “trackvance dataset prueba”/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Cargar como nueva versión' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/datasets/orders/versions/upload', expect.objectContaining({ method: 'POST' })))
    expect(post).not.toHaveBeenCalled()
  })

  it('sends corrected logical types and identifiers selected from the inspected schema', async () => {
    vi.mocked(api).mockImplementation(async path => path === '/datasets/uploads/inspect' ? {
      format: 'CSV',
      format_label: 'CSV delimitado',
      filename: 'customers.csv',
      columns: [
        { name: 'document_id', logical_type: 'STRING', semantic_tag: 'IDENTIFIER' },
        { name: 'amount', logical_type: 'DECIMAL', numeric: true },
      ],
    } : { id: 'version' })
    const user = userEvent.setup()
    renderApp(<UploadDialog open onClose={vi.fn()} datasetId="customers" datasetName="Clientes"/>)

    await user.upload(screen.getByLabelText('Seleccionar archivo de datos'), new File(['document_id,amount\n001,10\n'], 'customers.csv', { type: 'text/csv' }))
    await screen.findByText('CSV delimitado')
    await user.selectOptions(screen.getByLabelText('Tipo de amount'), 'INT64')
    await user.click(screen.getByRole('button', { name: 'Columnas identificadoras (opcional): abrir selector' }))
    const selectAll = screen.getByRole('checkbox', { name: 'Todos (2 columnas)' }) as HTMLInputElement
    await user.click(selectAll)
    expect(selectAll).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'document_id (STRING)' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'amount (DECIMAL)' })).toBeChecked()
    await user.click(screen.getByRole('checkbox', { name: 'amount (DECIMAL)' }))
    expect(selectAll.indeterminate).toBe(true)
    expect(screen.getByText('STRING · IDENTIFIER')).toBeInTheDocument()

    expect(screen.getByLabelText('Tipo de document_id')).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Cargar y analizar' }))

    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => path === '/datasets/customers/versions/upload')).toBe(true))
    const uploadCall = vi.mocked(api).mock.calls.find(([path]) => path === '/datasets/customers/versions/upload')!
    expect(JSON.parse(String((uploadCall[1]?.body as FormData).get('column_overrides')))).toEqual({
      amount: { logical_type: 'INT64' },
      document_id: { logical_type: 'STRING', semantic_tag: 'IDENTIFIER' },
    })
  })

  it('creates a dataset with a new business area entered in the upload flow', async () => {
    vi.mocked(api).mockImplementation(async path => path === '/datasets/uploads/inspect' ? {
      format: 'CSV', format_label: 'CSV delimitado', filename: 'risk_events.csv', columns: [{ name: 'event_id', logical_type: 'STRING' }, { name: 'risk_score', logical_type: 'DECIMAL', numeric: true }],
    } : { id: 'version' })
    vi.mocked(post).mockResolvedValue({ id: 'risk-events' })
    const user = userEvent.setup()
    renderApp(<UploadDialog open onClose={vi.fn()} existingDatasets={[{ id: 'existing', name: 'Ventas', domain: 'Comercial' }]}/>)

    await user.upload(screen.getByLabelText('Seleccionar archivo de datos'), new File(['event_id,risk_score\nA-1,10\n'], 'risk_events.csv', { type: 'text/csv' }))
    await screen.findByText('CSV delimitado')
    await user.selectOptions(screen.getByLabelText('Tipo de risk_score'), 'INT64')
    expect(screen.getByRole('option', { name: 'Comercial' })).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Área de negocio'), '__new_domain__')
    await user.type(screen.getByLabelText('Nueva área de negocio'), 'Riesgos')
    await user.click(screen.getByRole('button', { name: 'Cargar y analizar' }))

    await waitFor(() => expect(post).toHaveBeenCalledWith('/datasets', {
      name: 'risk events', description: '', domain: 'Riesgos',
    }))
    expect(api).toHaveBeenCalledWith('/datasets/risk-events/versions/upload', expect.objectContaining({ method: 'POST' }))
    const uploadCall = vi.mocked(api).mock.calls.find(([path]) => path === '/datasets/risk-events/versions/upload')!
    expect(JSON.parse(String((uploadCall[1]?.body as FormData).get('column_overrides')))).toEqual({ risk_score: { logical_type: 'INT64' } })
  })

  it.each([
    ['events.json', 'application/json', 'JSON', 'JSON tabular'],
    ['warehouse.parquet', 'application/vnd.apache.parquet', 'PARQUET', 'Apache Parquet'],
  ])('detects and previews %s before upload', async (filename, mime, format, formatLabel) => {
    vi.mocked(api).mockResolvedValue({
      format, format_label: formatLabel, filename, columns: [{ name: 'amount', logical_type: 'DECIMAL', native_type: 'Float64', numeric: true }], row_count: 25,
    })
    const user = userEvent.setup()
    renderApp(<UploadDialog open onClose={vi.fn()} datasetId="source" datasetName="Fuente"/>)

    await user.upload(screen.getByLabelText('Seleccionar archivo de datos'), new File(['content'], filename, { type: mime }))

    expect(await screen.findByText(formatLabel)).toBeInTheDocument()
    expect(screen.getByText('amount')).toBeInTheDocument()
    expect(screen.getByText(/25 filas/)).toBeInTheDocument()
    expect(api).toHaveBeenCalledWith('/datasets/uploads/inspect', expect.objectContaining({ method: 'POST' }))
  })

  it('selects an Excel sheet and sends it to inspection and upload', async () => {
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path !== '/datasets/uploads/inspect') return { id: 'version-xlsx' }
      const readerOptions = JSON.parse(String((options?.body as FormData).get('reader_options')))
      const selected = readerOptions.sheet_name || 'Resumen'
      return { format: 'XLSX', format_label: 'Excel XLSX', filename: 'reporte.xlsx', sheets: ['Resumen', 'Datos'], selected_sheet: selected, columns: [{ name: selected === 'Datos' ? 'transaction_id' : 'metric', logical_type: 'STRING' }] }
    })
    const user = userEvent.setup()
    renderApp(<UploadDialog open onClose={vi.fn()} datasetId="source" datasetName="Fuente"/>)
    const file = new File(['xlsx'], 'reporte.xlsx', { type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' })

    await user.upload(screen.getByLabelText('Seleccionar archivo de datos'), file)
    const sheet = await screen.findByLabelText('Hoja de Excel')
    expect(sheet).toHaveValue('Resumen')
    await user.selectOptions(sheet, 'Datos')
    await screen.findByText('transaction_id')

    const lastInspection = vi.mocked(api).mock.calls.filter(([path]) => path === '/datasets/uploads/inspect').at(-1)!
    expect(JSON.parse(String((lastInspection[1]?.body as FormData).get('reader_options')))).toEqual({ sheet_name: 'Datos' })
    await user.click(screen.getByRole('button', { name: 'Cargar y analizar' }))
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => path === '/datasets/source/versions/upload')).toBe(true))
    const uploadCall = vi.mocked(api).mock.calls.find(([path]) => path === '/datasets/source/versions/upload')!
    expect(JSON.parse(String((uploadCall[1]?.body as FormData).get('reader_options')))).toEqual({ sheet_name: 'Datos' })
  })

  it('shows the detected TXT delimiter and permits an explicit override', async () => {
    vi.mocked(api).mockImplementation(async path => path === '/datasets/uploads/inspect' ? {
      format: 'TXT', format_label: 'TXT delimitado', filename: 'movimientos.txt', detected_delimiter: ';', columns: [{ name: 'amount', logical_type: 'DECIMAL', numeric: true }],
    } : { id: 'version-txt' })
    const user = userEvent.setup()
    renderApp(<UploadDialog open onClose={vi.fn()} datasetId="source" datasetName="Fuente"/>)
    const file = new File(['id;amount\n1;10'], 'movimientos.txt', { type: 'text/plain' })

    await user.upload(screen.getByLabelText('Seleccionar archivo de datos'), file)
    const delimiter = await screen.findByLabelText('Delimitador del TXT')
    expect(screen.getByRole('option', { name: 'Automático · punto y coma (;)' })).toBeInTheDocument()
    await user.selectOptions(delimiter, '|')
    await waitFor(() => expect(vi.mocked(api).mock.calls.filter(([path]) => path === '/datasets/uploads/inspect')).toHaveLength(2))
    await user.click(screen.getByRole('button', { name: 'Cargar y analizar' }))
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => path === '/datasets/source/versions/upload')).toBe(true))
    const uploadCall = vi.mocked(api).mock.calls.find(([path]) => path === '/datasets/source/versions/upload')!
    expect(JSON.parse(String((uploadCall[1]?.body as FormData).get('reader_options')))).toEqual({ delimiter: '|' })
  })

  it('keeps upload disabled and offers retry when inspection fails', async () => {
    vi.mocked(api).mockRejectedValue(new Error('El archivo no contiene una estructura tabular válida.'))
    const user = userEvent.setup()
    renderApp(<UploadDialog open onClose={vi.fn()} datasetId="source" datasetName="Fuente"/>)

    await user.upload(screen.getByLabelText('Seleccionar archivo de datos'), new File(['{}'], 'invalid.json', { type: 'application/json' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('El archivo no contiene una estructura tabular válida.')
    expect(screen.getByRole('button', { name: 'Cargar y analizar' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Volver a intentar' })).toBeInTheDocument()
  })
})

describe('Dataset list sorting', () => {
  const datasets = [
    { id: 'alpha', name: 'Alpha', domain: 'Ventas', row_count: 30, version_count: 2, status: 'INACTIVE', updated_at: '2026-09-12T10:00:00Z' },
    { id: 'beta', name: 'Beta', domain: 'Finanzas', row_count: 5, version_count: 3, status: 'ACTIVE', updated_at: '2026-09-14T10:00:00Z' },
    { id: 'gamma', name: 'Gamma', domain: 'Operaciones', row_count: 10, version_count: 1, status: 'ACTIVE', updated_at: '2026-09-13T10:00:00Z' },
  ]

  const renderedOrder = () => screen.getAllByRole('row').slice(1).map(row => row.querySelector('.table-primary')?.textContent)

  it.each([
    ['Área', ['Beta', 'Gamma', 'Alpha'], ['Alpha', 'Gamma', 'Beta']],
    ['Registros', ['Beta', 'Gamma', 'Alpha'], ['Alpha', 'Gamma', 'Beta']],
    ['Versiones', ['Gamma', 'Alpha', 'Beta'], ['Beta', 'Alpha', 'Gamma']],
    ['Estado', ['Beta', 'Gamma', 'Alpha'], ['Alpha', 'Beta', 'Gamma']],
    ['Última actualización', ['Alpha', 'Gamma', 'Beta'], ['Beta', 'Gamma', 'Alpha']],
  ])('orders %s in both directions', async (column, ascending, descending) => {
    vi.mocked(api).mockResolvedValue({ items: datasets, total: datasets.length })
    const user = userEvent.setup()
    renderApp(<DatasetsPage/>)
    await screen.findByText('Alpha')

    const ascendingButton = screen.getByRole('button', { name: `Ordenar ${column} ascendente` })
    await user.click(ascendingButton)
    expect(renderedOrder()).toEqual(ascending)
    expect(ascendingButton.closest('th')).toHaveAttribute('aria-sort', 'ascending')

    await user.click(screen.getByRole('button', { name: `Ordenar ${column} descendente` }))
    expect(renderedOrder()).toEqual(descending)
    expect(ascendingButton.closest('th')).toHaveAttribute('aria-sort', 'descending')
  })

  it('shows the dataset origin between the dataset and area columns', async () => {
    vi.mocked(api).mockResolvedValue({
      items: [
        { id: 'manual', name: 'Carga mensual', domain: 'Finanzas', version_count: 1, origin_label: 'Manual', status: 'ACTIVE' },
        { id: 'intake', name: 'Aprobados', domain: 'Ventas', version_count: 1, source_type: 'INTAKE_OUTPUT', status: 'ACTIVE' },
        { id: 'empty', name: 'Pendiente', domain: 'Operaciones', version_count: 0, status: 'ACTIVE' },
      ],
      total: 3,
    })

    renderApp(<DatasetsPage/>)

    await screen.findByText('Carga mensual')
    const headers = screen.getAllByRole('columnheader').map(header => header.textContent)
    expect(headers.slice(0, 3)).toEqual(['Dataset', 'Origen', 'Área'])
    expect(screen.getByText('Manual')).toBeInTheDocument()
    expect(screen.getByText('Data Intake')).toBeInTheDocument()
    expect(screen.getByText('Sin versiones')).toBeInTheDocument()
  })
})
