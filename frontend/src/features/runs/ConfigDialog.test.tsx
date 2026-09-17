import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { ConfigDialog } from './ConfigDialog'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))

const datasets = { items: [{ id: 'source', name: 'Origen' }, { id: 'target', name: 'Destino' }], total: 2 }
const sourceSchema = {
  dataset_id: 'source', dataset_name: 'Origen', version_id: 'version-1', version: 1,
  schema_hash: 'schema-hash', scan_mode: 'PERSISTED_PROFILE', scanned_rows: 0,
  columns: [
    { name: 'transaction_id', logical_type: 'STRING', semantic_tag: 'IDENTIFIER', numeric: false },
    { name: 'transaction_date', logical_type: 'DATE', numeric: false },
    { name: 'amount', logical_type: 'DECIMAL', numeric: true },
    { name: 'quantity', logical_type: 'INT64', numeric: true },
    { name: 'note', logical_type: 'STRING', numeric: false },
  ],
}

async function openColumns(user: ReturnType<typeof userEvent.setup>, label: string) {
  await user.click(await screen.findByRole('button', { name: `${label}: abrir selector` }))
  return screen.getByRole('group', { name: `Opciones de ${label}` })
}

describe('Configuration publication', () => {
  beforeEach(() => {
    vi.mocked(api).mockImplementation(async path => path === '/datasets' ? datasets : path.includes('/schema') ? sourceSchema : { versions: [{ schema: sourceSchema.columns }] })
    vi.mocked(post).mockResolvedValue({ id: 'new-config' })
  })

  it('publishes Intake date rules in the versioned API payload', async () => {
    const user = userEvent.setup(), close = vi.fn()
    renderApp(<ConfigDialog module="intake" open close={close}/>)
    await user.type(await screen.findByLabelText('Nombre del contrato'), 'Fechas de ventas')
    await user.selectOptions(screen.getByLabelText('Dataset'), 'source')
    const required = await openColumns(user, 'Columnas obligatorias')
    await user.click(within(required).getByRole('checkbox', { name: /^transaction_id / }))
    await user.click(within(required).getByRole('checkbox', { name: /^transaction_date / }))
    await user.click(screen.getByRole('button', { name: 'Agregar regla' }))
    await user.type(screen.getByLabelText('Columna de la regla'), 'transaction_date')
    await user.click(screen.getByRole('button', { name: 'Crear contrato' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/intake/contracts', expect.objectContaining({ name: 'Fechas de ventas', dataset_id: 'source', config: expect.objectContaining({ schema_version: 2, required_columns: ['transaction_id', 'transaction_date'], rules: [{ type: 'date_rule', column: 'transaction_date', severity: 'ERROR', parameters: { not_future: true, timezone: 'UTC', null_policy: 'ALLOW' } }] }) })))
    expect(close).toHaveBeenCalledOnce()
  })

  it('uses detected columns in multi-selectors and restricts positive values to numeric types', async () => {
    const user = userEvent.setup()
    renderApp(<ConfigDialog module="intake" open close={vi.fn()}/>)
    await user.type(await screen.findByLabelText('Nombre del contrato'), 'Esquema guiado')
    await user.selectOptions(screen.getByLabelText('Dataset'), 'source')
    expect(await screen.findByText('Versión 1 · 5 columnas')).toBeInTheDocument()

    const required = await openColumns(user, 'Columnas obligatorias')
    await user.click(within(required).getByRole('checkbox', { name: /^transaction_id / }))
    const unique = await openColumns(user, 'Columnas sin duplicados')
    await user.click(within(unique).getByRole('checkbox', { name: /^transaction_id / }))
    const numeric = await openColumns(user, 'Columnas numéricas')
    expect(within(numeric).getAllByRole('checkbox').slice(1, 3).map(option => option.getAttribute('aria-label'))).toEqual(['amount (DECIMAL)', 'quantity (INT64)'])
    await user.click(within(numeric).getByRole('checkbox', { name: /^amount / }))
    await user.click(within(numeric).getByRole('checkbox', { name: /^note / }))
    const positive = await openColumns(user, 'Columnas con valores positivos')
    expect(within(positive).getAllByRole('checkbox').slice(1).map(option => option.getAttribute('aria-label'))).toEqual(['amount (DECIMAL)', 'quantity (INT64)'])
    await user.click(within(positive).getByRole('checkbox', { name: /^amount / }))

    await user.click(screen.getByRole('button', { name: 'Crear contrato' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/intake/contracts', expect.objectContaining({ config: expect.objectContaining({ required_columns: ['transaction_id'], unique_columns: ['transaction_id'], numeric_columns: ['amount', 'note'], positive_columns: ['amount'] }) })))
  })

  it('selects and clears every eligible Intake column with Todos', async () => {
    const user = userEvent.setup()
    renderApp(<ConfigDialog module="intake" open close={vi.fn()}/>)
    await user.type(await screen.findByLabelText('Nombre del contrato'), 'Todas las columnas')
    await user.selectOptions(screen.getByLabelText('Dataset'), 'source')

    const required = await openColumns(user, 'Columnas obligatorias')
    const allRequired = within(required).getByRole('checkbox', { name: 'Todos (5 columnas)' }) as HTMLInputElement
    await user.click(allRequired)
    expect(allRequired).toBeChecked()
    expect(within(required).getAllByRole('checkbox').slice(1)).toHaveLength(5)
    expect(within(required).getAllByRole('checkbox').slice(1).every(option => (option as HTMLInputElement).checked)).toBe(true)
    await user.click(within(required).getByRole('checkbox', { name: /^transaction_id / }))
    expect(allRequired.indeterminate).toBe(true)
    await user.click(allRequired)
    expect(allRequired).toBeChecked()
    await user.click(allRequired)
    expect(allRequired).not.toBeChecked()
    expect(within(required).getAllByRole('checkbox').slice(1).every(option => !(option as HTMLInputElement).checked)).toBe(true)
    await user.click(allRequired)

    const unique = await openColumns(user, 'Columnas sin duplicados')
    await user.click(within(unique).getByRole('checkbox', { name: 'Todos (5 columnas)' }))
    const numeric = await openColumns(user, 'Columnas numéricas')
    await user.click(within(numeric).getByRole('checkbox', { name: 'Todos (5 columnas)' }))
    const positive = await openColumns(user, 'Columnas con valores positivos')
    const allPositive = within(positive).getByRole('checkbox', { name: 'Todos (2 columnas)' })
    await user.click(allPositive)
    expect(within(positive).getAllByRole('checkbox').slice(1).map(option => option.getAttribute('aria-label'))).toEqual(['amount (DECIMAL)', 'quantity (INT64)'])

    await user.click(screen.getByRole('button', { name: 'Crear contrato' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/intake/contracts', expect.objectContaining({ config: expect.objectContaining({
      required_columns: ['transaction_id', 'transaction_date', 'amount', 'quantity', 'note'],
      unique_columns: ['transaction_id', 'transaction_date', 'amount', 'quantity', 'note'],
      numeric_columns: ['transaction_id', 'transaction_date', 'amount', 'quantity', 'note'],
      positive_columns: ['amount', 'quantity'],
    }) })))
  })

  it('refreshes the schema metadata on demand', async () => {
    const refreshed = { ...sourceSchema, version_id: 'version-2', version: 2, scan_mode: 'CANONICAL_PARQUET_METADATA', columns: [...sourceSchema.columns, { name: 'currency', logical_type: 'STRING', numeric: false }] }
    vi.mocked(api).mockImplementation(async path => path === '/datasets' ? datasets : path.includes('refresh=true') ? refreshed : path.includes('/schema') ? sourceSchema : { versions: [{ schema: sourceSchema.columns }] })
    const user = userEvent.setup()
    renderApp(<ConfigDialog module="intake" open close={vi.fn()}/>)
    await user.selectOptions(await screen.findByLabelText('Dataset'), 'source')

    await user.click(await screen.findByRole('button', { name: 'Actualizar esquema' }))

    await waitFor(() => expect(api).toHaveBeenCalledWith('/datasets/source/schema?refresh=true'))
    expect(await screen.findByText('Versión 2 · 6 columnas')).toBeInTheDocument()
    expect(screen.getByText('currency')).toBeInTheDocument()
    expect(screen.getByText('Metadata de la última versión verificada sin leer filas del dataset.')).toBeInTheDocument()
  })

  it('publishes explicit Recon normalization, tolerance and 1:N aggregation', async () => {
    const user = userEvent.setup()
    renderApp(<ConfigDialog module="recon" open close={vi.fn()}/>)
    await user.type(await screen.findByLabelText('Nombre del control'), 'Conciliación 1:N')
    await user.selectOptions(screen.getByLabelText('Dataset de origen'), 'source')
    await user.selectOptions(screen.getByLabelText('Dataset de destino'), 'target')
    await user.type(screen.getByLabelText('Columnas clave'), 'transaction_id')
    await user.selectOptions(screen.getByLabelText('Claves: espacios externos'), 'TRIM')
    await user.type(screen.getByLabelText('Columna de origen'), 'amount')
    await user.type(screen.getByLabelText('Columna de destino'), 'total')
    await user.selectOptions(screen.getByLabelText('Conciliación 1:N'), 'AGGREGATE')
    await user.type(screen.getByLabelText('Columna a sumar'), 'payment')
    await user.type(screen.getByLabelText('Columna del resultado agregado'), 'total')
    await user.click(screen.getByRole('button', { name: 'Crear control' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/recon/controls', expect.objectContaining({ config: expect.objectContaining({ schema_version: 2, key_columns: ['transaction_id'], key_normalization: { trim: true, case: 'NONE', unicode_normalization: 'NONE' }, comparison_rules: [expect.objectContaining({ type: 'numeric_tolerance', source_column: 'amount', target_column: 'total' })], aggregation: { side: 'TARGET', operation: 'sum', column: 'payment', output_column: 'total' } }) })))
    const request = vi.mocked(post).mock.calls[0][1] as { config: Record<string, unknown> }
    expect(request.config).not.toHaveProperty('rules')
  })

  it.each([{}, { schema_version: 1 }])('preserves legacy TRIM when publishing a new immutable version (%j)', async marker => {
    const user = userEvent.setup()
    renderApp(<ConfigDialog module="recon" open close={vi.fn()} initial={{ id: 'old-control', name: 'Control histórico', version: 4, dataset_id: 'source', target_dataset_id: 'target', config: { ...marker, key_columns: ['transaction_id'], amount_column: 'amount', tolerance: '0.5' } }}/>)
    expect(await screen.findByLabelText('Claves: espacios externos')).toHaveValue('TRIM')
    expect(screen.getByLabelText('Nombre del control')).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Publicar nueva versión' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/recon/controls/old-control/versions', expect.objectContaining({ config: expect.objectContaining({ schema_version: 2, key_normalization: { trim: true, case: 'NONE', unicode_normalization: 'NONE' } }) })))
  })

  it('keeps backend validation errors visible and allows editing before retry', async () => {
    vi.mocked(post).mockRejectedValue(new Error('Patrón regex no portable.'))
    const user = userEvent.setup()
    renderApp(<ConfigDialog module="sentinel" open close={vi.fn()} initial={{ id: 'monitor', name: 'Monitoreo', dataset_id: 'source', version: 1, config: {} }}/>)
    await user.click(await screen.findByRole('button', { name: 'Publicar nueva versión' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Patrón regex no portable.')
    expect(screen.getByRole('button', { name: 'Publicar nueva versión' })).toBeEnabled()
  })

  it('publishes schema drift against the previous version without inventing an expected type', async () => {
    const user = userEvent.setup()
    renderApp(<ConfigDialog module="sentinel" open close={vi.fn()} initial={{ id: 'monitor', name: 'Tipos de ventas', dataset_id: 'source', version: 2, config: {} }}/>)
    await user.click(await screen.findByRole('button', { name: 'Agregar regla' }))
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), 'schema_type')
    await user.type(screen.getByLabelText('Columna de la regla'), 'transaction_date')
    await user.selectOptions(screen.getByLabelText('Tipo lógico esperado'), 'PREVIOUS')
    await user.click(screen.getByRole('button', { name: 'Publicar nueva versión' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/monitors/monitor/versions', expect.objectContaining({ config: expect.objectContaining({ rules: [{ type: 'schema_type', code: 'SCHEMA_TYPE', column: 'transaction_date', severity: 'ERROR', parameters: {} }] }) })))
  })
})
