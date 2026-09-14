import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { ConfigDialog } from './ConfigDialog'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))

const datasets = { items: [{ id: 'source', name: 'Origen' }, { id: 'target', name: 'Destino' }], total: 2 }

describe('Configuration publication', () => {
  beforeEach(() => {
    vi.mocked(api).mockImplementation(async path => path === '/datasets' ? datasets : { versions: [{ schema: [{ name: 'transaction_id' }, { name: 'transaction_date' }, { name: 'amount' }] }] })
    vi.mocked(post).mockResolvedValue({ id: 'new-config' })
  })

  it('publishes Intake date rules in the versioned API payload', async () => {
    const user = userEvent.setup(), close = vi.fn()
    renderApp(<ConfigDialog module="intake" open close={close}/>)
    await user.type(await screen.findByLabelText('Nombre del contrato'), 'Fechas de ventas')
    await user.selectOptions(screen.getByLabelText('Dataset'), 'source')
    await user.type(screen.getByLabelText('Columnas obligatorias'), 'transaction_id, transaction_date')
    await user.click(screen.getByRole('button', { name: 'Agregar regla' }))
    await user.type(screen.getByLabelText('Columna de la regla'), 'transaction_date')
    await user.click(screen.getByRole('button', { name: 'Crear contrato' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/intake/contracts', expect.objectContaining({ name: 'Fechas de ventas', dataset_id: 'source', config: expect.objectContaining({ schema_version: 2, required_columns: ['transaction_id', 'transaction_date'], rules: [{ type: 'date_rule', column: 'transaction_date', severity: 'ERROR', parameters: { not_future: true, timezone: 'UTC', null_policy: 'ALLOW' } }] }) })))
    expect(close).toHaveBeenCalledOnce()
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
