import { act, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, download, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { DeliveriesPage, DeliveryRunDetail } from './Delivery'
import { DeliveryBuilder } from './DeliveryBuilder'
import { DestinationDialog, DestinationsPage } from './Destinations'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn(), download: vi.fn() }))

const permissions = ['connections:read', 'connections:manage', 'configurations:write', 'runs:execute', 'artifacts:download']
const destination = {
  id: 'destination-1', name: 'Warehouse', sink_type: 'POSTGRESQL' as const, enabled: true, version: 2,
  destination_version_id: 'destination-version-2', host: 'warehouse.local', port: 5432, database: 'analytics', username: 'writer', options: { connect_timeout: 5, query_timeout: 60, sslmode: 'require' },
  last_test_status: 'SUCCESS', last_test_at: '2026-09-22T12:00:00Z', updated_at: '2026-09-22T12:00:00Z',
}

beforeEach(() => vi.resetAllMocks())

describe('Delivery destinations', () => {
  it('requires a successful test of the current write configuration before saving', async () => {
    vi.mocked(post).mockResolvedValueOnce({ status: 'SUCCESS', message: 'Acceso de escritura verificado.', tested_at: '2026-09-22T12:00:00Z' }).mockResolvedValueOnce(destination)
    const saved = vi.fn(), user = userEvent.setup()
    renderApp(<DestinationDialog close={vi.fn()} saved={saved}/>, { permissions })

    expect(screen.getByRole('button', { name: 'Guardar destino' })).toBeDisabled()
    await user.type(screen.getByLabelText('Nombre del destino'), 'Warehouse')
    await user.type(screen.getByLabelText('Host'), 'warehouse.local')
    await user.type(screen.getByLabelText('Base de datos'), 'analytics')
    await user.type(screen.getByLabelText('Usuario de escritura'), 'writer')
    await user.type(screen.getByLabelText('Contraseña'), 'disposable-secret')
    await user.click(screen.getByRole('button', { name: 'Probar destino' }))

    expect(await screen.findByText('Destino verificado')).toBeInTheDocument()
    expect(post).toHaveBeenCalledWith('/delivery/destinations/test', expect.objectContaining({
      name: 'Warehouse', sink_type: 'POSTGRESQL', password: 'disposable-secret',
      host: 'warehouse.local', port: 5432, database: 'analytics', username: 'writer',
      options: expect.objectContaining({ connect_timeout: 5, query_timeout: 60, sslmode: 'require' }),
    }))
    await user.click(screen.getByRole('button', { name: 'Guardar destino' }))
    await waitFor(() => expect(saved).toHaveBeenCalled())
    expect(saved.mock.calls[0]?.[0]).toEqual(destination)
    expect(post).toHaveBeenCalledWith('/delivery/destinations', expect.objectContaining({
      host: 'warehouse.local', port: 5432, database: 'analytics', username: 'writer', password: 'disposable-secret',
      options: { connect_timeout: 5, query_timeout: 60, sslmode: 'require' },
    }))
  })

  it('keeps read-only access visible and management disabled', async () => {
    vi.mocked(api).mockResolvedValue({ items: [destination], total: 1 })
    renderApp(<DestinationsPage/>, { permissions: ['connections:read'] })
    expect(await screen.findByRole('link', { name: 'Warehouse' })).toHaveAttribute('href', '/delivery/destinations/destination-1')
    expect(screen.getByRole('button', { name: 'Nuevo destino' })).toBeDisabled()
    expect(screen.queryByText('disposable-secret')).not.toBeInTheDocument()
  })
})

describe('Guided delivery builder', () => {
  it('uses real metadata, publishes an immutable mapping and requires a passing preflight', async () => {
    const publishedConfiguration = { id: 'configuration-1', name: 'Publicar ventas', version: 1, dataset_id: 'dataset-1', config: { schema_version: 1, dataset_version_id: 'version-3', destination_id: 'destination-1', destination_version_id: 'destination-version-2' } }
    let finishPublication: (() => void) | undefined
    const publication = new Promise<typeof publishedConfiguration>(resolve => { finishPublication = () => resolve(publishedConfiguration) })
    vi.mocked(api).mockImplementation(async path => {
      if (path === '/datasets') return { items: [{ id: 'dataset-1', name: 'Ventas' }], total: 1 }
      if (path === '/datasets/dataset-1') return { id: 'dataset-1', name: 'Ventas', versions: [{ id: 'version-3', version: 3, filename: 'ventas.parquet', row_count: 2, source_type: 'UPLOAD', canonical_artifact_id: 'artifact-3' }] }
      if (path === '/dataset-versions/version-3/profile') return { profile: { columns: [{ name: 'sale_id', logical_type: 'STRING', nullable: false }, { name: 'amount', logical_type: 'DECIMAL', nullable: true, precision: 12, scale: 2 }, { name: 'quantity', logical_type: 'DECIMAL', nullable: true, precision: 0, scale: 0 }] }, sample: [{ sale_id: 'A-01', amount: '10.50', quantity: 2 }] }
      if (path === '/delivery/destinations') return { items: [destination], total: 1 }
      if (path === '/delivery/destinations/destination-1') return destination
      if (path === '/delivery/destinations/destination-1/schemas') return { items: ['public'], total: 1 }
      if (path.includes('/delivery/destinations/destination-1/tables?')) return { items: [{ name: 'sales' }], total: 1 }
      if (path.includes('/delivery/destinations/destination-1/table-metadata?')) return { schema_name: 'public', table_name: 'sales', columns: [{ name: 'sale_id', native_type: 'numeric', logical_type: 'DECIMAL', nullable: false, has_default: false, identity: false, generated: false, precision: 18, scale: 0 }, { name: 'amount', native_type: 'numeric', logical_type: 'DECIMAL', nullable: true, has_default: false, identity: false, generated: false, precision: 12, scale: 2 }], constraints: [{ type: 'PRIMARY_KEY', columns: ['sale_id'] }], destination_version_id: 'destination-version-2' }
      throw new Error(`Unexpected request: ${path}`)
    })
    vi.mocked(post).mockImplementation(async path => {
      if (path === '/delivery/preview') return { dataset_version_id: 'version-3', sampled_rows: 1, columns: [], source_rows: [{ sale_id: 'A-01', amount: '10.50' }], destination_rows: [{ sale_id: 'A-01', total_amount: '10.50' }] }
      if (path === '/delivery/preflight') return { status: 'PASS', checks: [{ code: 'TARGET_COMPATIBLE', status: 'PASS', message: 'La tabla es compatible.' }], source: {}, destination: {}, target: {} }
      if (path === '/delivery/configurations') return publication
      throw new Error(`Unexpected write: ${path}`)
    })
    const user = userEvent.setup()
    renderApp(<DeliveryBuilder/>, { permissions, path: '/delivery/new' })

    await user.type(screen.getByLabelText('Nombre de la entrega'), 'Publicar ventas')
    await user.selectOptions(await screen.findByLabelText('Dataset'), 'dataset-1')
    await user.selectOptions(await screen.findByLabelText('DatasetVersion exacta'), 'version-3')
    await user.click(screen.getByRole('button', { name: /Continuar/ }))

    await user.selectOptions(await screen.findByLabelText('Destino de publicación'), 'destination-1')
    await screen.findByText('destination-version-2')
    await user.click(screen.getByRole('button', { name: /Continuar/ }))

    await user.selectOptions(await screen.findByLabelText('Schema'), 'public')
    await user.selectOptions(await screen.findByLabelText('Tabla existente'), 'sales')
    await screen.findByText('public.sales')
    await user.click(screen.getByRole('button', { name: /Continuar/ }))

    const saleIdType = await screen.findByLabelText('Tipo destino de sale_id')
    expect(saleIdType).toBeDisabled()
    expect(saleIdType).toHaveValue('STRING')
    expect(within(saleIdType).getAllByRole('option').map(option => option.textContent)).toEqual(['STRING'])
    expect(within(saleIdType).queryByRole('option', { name: 'DECIMAL' })).not.toBeInTheDocument()
    expect(within(saleIdType).queryByRole('option', { name: 'DATE' })).not.toBeInTheDocument()
    const saleIdLength = screen.getByLabelText('Longitud de sale_id')
    expect(saleIdLength).toBeEnabled()
    await user.type(saleIdLength, '40')
    const amountName = await screen.findByLabelText('Nombre destino de amount')
    await user.clear(amountName)
    await user.type(amountName, 'total_amount')
    await user.click(screen.getByRole('button', { name: /Continuar/ }))

    await user.click(screen.getByRole('button', { name: /Actualizar existentes y agregar nuevos/ }))
    const keyPicker = screen.getByRole('group', { name: 'Clave de UPSERT' })
    await user.click(within(keyPicker).getByRole('checkbox', { name: /^sale_id/ }))
    await user.click(screen.getByRole('button', { name: /Continuar/ }))

    await user.click(screen.getByRole('button', { name: 'Generar preview' }))
    expect((await screen.findAllByText(/total_amount/)).length).toBeGreaterThan(0)
    const previewCall = vi.mocked(post).mock.calls.find(([path]) => path === '/delivery/preview')
    const previewColumns = (previewCall?.[1] as { columns: Record<string, unknown>[] }).columns
    expect(previewColumns.find(column => column.source_name === 'sale_id')).toEqual(expect.objectContaining({ target_type: 'STRING', length: 40 }))
    const quantity = previewColumns.find(column => column.source_name === 'quantity')
    expect(quantity).toEqual(expect.objectContaining({ source_name: 'quantity', target_type: 'DECIMAL' }))
    expect(quantity).not.toHaveProperty('precision')
    expect(quantity).not.toHaveProperty('scale')
    await user.click(screen.getByRole('button', { name: 'Ejecutar preflight' }))
    expect(await screen.findByText('Preflight aprobado')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Continuar/ }))
    const publishButton = screen.getByRole('button', { name: 'Publicar configuración' })
    await user.click(publishButton)
    expect(await screen.findByRole('button', { name: 'Publicando…' })).toBeDisabled()
    expect(screen.getByRole('button', { name: /Dataset y versión/ })).toBeDisabled()

    await waitFor(() => expect(post).toHaveBeenCalledWith('/delivery/configurations', expect.objectContaining({
      name: 'Publicar ventas', owner: 'Equipo de datos', dataset_version_id: 'version-3', destination_id: 'destination-1', destination_version_id: 'destination-version-2',
      schema_version: 1, write_strategy: 'UPSERT', upsert_keys: ['sale_id'], target: { mode: 'EXISTING_TABLE', schema_name: 'public', table_name: 'sales', create_schema: false }, columns: expect.arrayContaining([expect.objectContaining({ source_name: 'amount', target_name: 'total_amount', target_type: 'DECIMAL', precision: 12, scale: 2 })]),
    })))
    await act(async () => finishPublication?.())
    expect(await screen.findByText(/Publicada como versión 1/)).toBeInTheDocument()
  })
})

describe('Delivery runs and evidence', () => {
  it('creates a run with an Idempotency-Key header', async () => {
    const configuration = { id: 'configuration-1', name: 'Publicar ventas', version: 1, dataset_id: 'dataset-1', dataset_name: 'Ventas', config: { schema_version: 1, dataset_version_id: 'version-3', destination_id: 'destination-1', destination_version_id: 'destination-version-2', target: { mode: 'EXISTING_TABLE', schema_name: 'public', table_name: 'sales' }, write_strategy: 'APPEND', columns: [{ source_name: 'sale_id' }] }, status: 'PUBLISHED' }
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path === '/delivery/configurations') return { items: [configuration], total: 1 }
      if (path === '/delivery/destinations') return { items: [destination], total: 1 }
      if (path === '/delivery/runs' && options?.method === 'POST') return { id: 'run-1' }
      throw new Error(`Unexpected request: ${path}`)
    })
    const user = userEvent.setup()
    renderApp(<DeliveriesPage/>, { permissions })
    await user.click(await screen.findByRole('button', { name: 'Ejecutar' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/delivery/runs', expect.objectContaining({ method: 'POST', headers: { 'Idempotency-Key': expect.any(String) }, body: JSON.stringify({ configuration_id: 'configuration-1', dataset_version_id: 'version-3' }) })))
  })

  it('keeps UNKNOWN distinct and never presents it as a retryable failure', async () => {
    vi.mocked(api).mockImplementation(async path => {
      if (path === '/delivery/runs/run-unknown/attempts') return { items: [{ id: 'attempt-1', attempt_number: 1, status: 'UNKNOWN', rows_attempted: 2, rows_written: null, started_at: '2026-09-22T12:00:00Z', finished_at: '2026-09-22T12:00:01Z' }], total: 1 }
      throw new Error(`Unexpected request: ${path}`)
    })
    renderApp(<DeliveryRunDetail data={{ id: 'run-unknown', module: 'DELIVERY', status: 'UNKNOWN', name: 'Publicar ventas', dataset_name: 'Ventas', dataset_version_id: 'version-3', created_at: '2026-09-22T12:00:00Z' }}/>, { permissions })
    expect(await screen.findByText('Confirmación remota desconocida')).toBeInTheDocument()
    expect(screen.getByText(/UNKNOWN no equivale a FAILED ni COMMITTED/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /reintentar/i })).not.toBeInTheDocument()
    expect(vi.mocked(api).mock.calls.some(([path]) => path === '/delivery/runs/run-unknown/receipt')).toBe(false)
  })

  it('shows duration and the controlled cause of a failed execution preflight', async () => {
    vi.mocked(api).mockResolvedValue({ items: [], total: 0 })
    renderApp(<DeliveryRunDetail data={{ id: 'run-preflight', module: 'DELIVERY', status: 'FAILED_PRECONDITION', error: 'El target cambió desde la publicación.', name: 'Publicar ventas', dataset_name: 'Ventas', dataset_version_id: 'version-3', created_at: '2026-09-22T12:00:00Z', started_at: '2026-09-22T12:00:01Z', finished_at: '2026-09-22T12:00:03Z' }}/>, { permissions })
    expect(await screen.findByText('El target cambió desde la publicación.')).toBeInTheDocument()
    expect(screen.getByText('Duración 2 s')).toBeInTheDocument()
  })

  it('loads the immutable receipt only for a committed attempt', async () => {
    vi.mocked(api).mockImplementation(async path => {
      if (path === '/delivery/runs/run-committed/attempts') return { items: [{ id: 'attempt-1', attempt_number: 1, status: 'COMMITTED', rows_attempted: 2, rows_written: 2, rows_inserted: 2, rows_updated: 0 }], total: 1 }
      if (path === '/delivery/runs/run-committed/receipt') return { kind: 'DELIVERY_RECEIPT', run_id: 'run-committed', dataset_version_id: 'version-3', destination_version_id: 'destination-version-2', source_sha256: 'abc123', target: { mode: 'EXISTING_TABLE', schema_name: 'public', table_name: 'sales', create_schema: false }, write_strategy: 'APPEND', result: 'COMMITTED', rows_written: 2 }
      if (path === '/audit-events') return { items: [{ id: 'audit-1', run_id: 'run-committed', event_type: 'DELIVERY_COMMITTED', message: 'Entrega confirmada', actor: 'delivery-worker' }], total: 1 }
      throw new Error(`Unexpected request: ${path}`)
    })
    renderApp(<DeliveryRunDetail data={{ id: 'run-committed', module: 'DELIVERY', status: 'SUCCESS', name: 'Publicar ventas', dataset_name: 'Ventas', dataset_version_id: 'version-3', created_at: '2026-09-22T12:00:00Z', execution_plan: { destination_name: 'Warehouse', destination_version: 2, destination_version_id: 'destination-version-2', write_strategy: 'APPEND', target: { schema_name: 'public', table_name: 'sales' } } }}/>, { permissions: [...permissions, 'audit:read'] })
    expect(await screen.findByText('Receipt inmutable')).toBeInTheDocument()
    expect(screen.getByText('Warehouse')).toBeInTheDocument()
    expect(screen.getByText('v2 · destination-version-2')).toBeInTheDocument()
    expect(await screen.findByText('public.sales')).toBeInTheDocument()
    expect(await screen.findByText('DELIVERY_COMMITTED')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Receipt' })).toBeEnabled()
    expect(download).not.toHaveBeenCalled()
  })

  it('refreshes attempts once when a pending run reaches its terminal state', async () => {
    let attemptRequests = 0
    let finishInitialAttempt: ((value: { items: never[]; total: number }) => void) | undefined
    const initialAttempt = new Promise<{ items: never[]; total: number }>(resolve => { finishInitialAttempt = resolve })
    vi.mocked(api).mockImplementation(async path => {
      if (path === '/delivery/runs/run-race/attempts') {
        attemptRequests += 1
        return attemptRequests === 1
          ? initialAttempt
          : { items: [{ id: 'attempt-race', attempt_number: 1, status: 'COMMITTED', rows_attempted: 2, rows_written: 2 }], total: 1 }
      }
      if (path === '/delivery/runs/run-race/receipt') return { kind: 'DELIVERY_RECEIPT', run_id: 'run-race', target_locator: 'public.race_result', result: 'COMMITTED', rows_written: 2 }
      throw new Error(`Unexpected request: ${path}`)
    })
    function RunTransition() {
      const [status, setStatus] = useState('QUEUED')
      return <><button onClick={() => setStatus('SUCCESS')}>Terminar run</button><DeliveryRunDetail data={{ id: 'run-race', module: 'DELIVERY', status, name: 'Entrega concurrente', created_at: '2026-09-22T12:00:00Z' }}/></>
    }
    const user = userEvent.setup()
    renderApp(<RunTransition/>, { permissions })
    await waitFor(() => expect(attemptRequests).toBe(1))

    await user.click(screen.getByRole('button', { name: 'Terminar run' }))

    expect((await screen.findAllByText('Confirmado')).length).toBeGreaterThan(0)
    expect(await screen.findByText('public.race_result')).toBeInTheDocument()
    expect(attemptRequests).toBe(2)
    await act(async () => finishInitialAttempt?.({ items: [], total: 0 }))
    expect(screen.getByText('public.race_result')).toBeInTheDocument()
  })
})
