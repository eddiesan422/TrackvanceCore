import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { RestEndpointDialog } from '../connections/RestEndpointDialog'
import { RestDeliveryBuilder } from './RestDeliveryBuilder'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))

beforeEach(() => vi.resetAllMocks())

describe('REST endpoint controls', () => {
  it('keeps secrets separate from options and never tests a business POST', async () => {
    const saved = vi.fn(), user = userEvent.setup()
    vi.mocked(post).mockResolvedValueOnce({ status: 'SUCCESS' }).mockResolvedValueOnce({ id: 'rest-output' })
    renderApp(<RestEndpointDialog mode="DESTINATION" close={vi.fn()} saved={saved}/>)
    await user.type(screen.getByLabelText('Nombre'), 'REST output')
    await user.type(screen.getByLabelText('URL del endpoint'), 'https://receiver.example/write')
    await user.selectOptions(screen.getByLabelText('Autenticación'), 'BEARER')
    await user.type(screen.getByLabelText('Credencial'), 'output-secret')
    expect(screen.getByLabelText('Credencial')).toHaveAttribute('type', 'password')
    expect(screen.getByRole('button', { name: 'Guardar' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Probar acceso HEAD' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Guardar' })).toBeEnabled())
    expect(post).toHaveBeenCalledWith('/delivery/destinations/test', expect.objectContaining({ sink_type: 'REST', password: 'output-secret', options: expect.objectContaining({ auth: 'BEARER' }) }))
    const payload = vi.mocked(post).mock.calls[0][1] as { options: Record<string, unknown> }
    expect(JSON.stringify(payload.options)).not.toContain('output-secret')
    expect(payload.options).not.toHaveProperty('fields')
    expect(screen.queryByLabelText('Schema')).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Guardar' }))
    await waitFor(() => expect(saved).toHaveBeenCalledWith('rest-output'))
    expect(post).not.toHaveBeenCalledWith('/delivery/runs', expect.anything())
  })

  it('does not approve an old successful read after changing the URL', async () => {
    let resolve!: (value: unknown) => void
    vi.mocked(post).mockImplementation(() => new Promise(done => { resolve = done }))
    const user = userEvent.setup()
    renderApp(<RestEndpointDialog mode="SOURCE" close={vi.fn()} saved={vi.fn()}/>)
    await user.type(screen.getByLabelText('Nombre'), 'REST source')
    await user.type(screen.getByLabelText('URL del endpoint'), 'https://source.example/first')
    await user.click(screen.getByRole('button', { name: 'Probar lectura GET' }))
    await user.type(screen.getByLabelText('URL del endpoint'), '-changed')
    await act(async () => resolve({ status: 'SUCCESS' }))
    expect(screen.getByRole('button', { name: 'Guardar' })).toBeDisabled()
    expect(post).toHaveBeenCalledTimes(1)
    const payload = vi.mocked(post).mock.calls[0][1] as { options: Record<string, unknown> }
    expect(payload.options.fields).toEqual([{ name: 'id', path: 'id', logical_type: 'STRING', nullable: false }])
  })
})

function setupBuilder() {
  vi.mocked(api).mockImplementation(async path => {
    if (path === '/datasets') return { items: [{ id: 'dataset', name: 'Orders' }] }
    if (path === '/datasets/dataset') return { id: 'dataset', name: 'Orders', versions: [{ id: 'version', version: 2, row_count: 2 }] }
    if (path === '/delivery/destinations') return { items: [{ id: 'rest-dest', name: 'REST receiver', sink_type: 'REST', enabled: true, version: 1, destination_version_id: 'rest-dest-v1' }] }
    if (path === '/dataset-versions/version/profile') return { schema: [{ name: 'id', logical_type: 'STRING', nullable: false }] }
    if (path === '/delivery/validations/preflight') return { id: 'preflight', status: 'SUCCESS', result: { status: 'PASS' } }
    throw new Error(`Unexpected request ${path}`)
  })
  renderApp(<RestDeliveryBuilder/>, { permissions: ['delivery:configure'], path: '/delivery/rest' })
}

async function completeBuilder(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText('Nombre de la entrega'), 'REST publication')
  await screen.findByRole('option', { name: 'Orders' })
  await user.selectOptions(screen.getByLabelText('Dataset'), 'dataset')
  await screen.findByRole('option', { name: 'v2 · 2 filas' })
  await user.selectOptions(screen.getByLabelText('Versión exacta'), 'version')
  await screen.findByRole('option', { name: 'REST receiver · revisión 1' })
  await user.selectOptions(screen.getByLabelText('Destino REST'), 'rest-dest')
  await screen.findByLabelText('Clave de salida id')
}

describe('REST delivery publication', () => {
  it('preview and preflight remain local API operations and cannot execute without permission', async () => {
    setupBuilder()
    const user = userEvent.setup()
    await completeBuilder(user)
    vi.mocked(post).mockImplementation(async path => {
      if (path === '/delivery/preview') return { dataset_version_id: 'version', sampled_rows: 1, columns: [], source_rows: [{ id: '0001' }], destination_rows: [{ id: '0001' }] }
      if (path === '/delivery/validations') return { id: 'preflight', status: 'QUEUED' }
      if (path.startsWith('/delivery/configurations?')) return { id: 'publication' }
      throw new Error(`Unexpected POST ${path}`)
    })
    await user.click(screen.getByRole('button', { name: 'Preview local' }))
    await screen.findByText('Muestra del payload')
    expect(screen.getByRole('button', { name: 'Publicar configuración' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Preflight completo' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Publicar configuración' })).toBeEnabled())
    await user.click(screen.getByRole('button', { name: 'Publicar configuración' }))
    expect(await screen.findByRole('button', { name: 'Ejecutar entrega' })).toBeDisabled()
    const paths = vi.mocked(post).mock.calls.map(call => call[0])
    expect(paths).toEqual(['/delivery/preview', '/delivery/validations', '/delivery/configurations?validation_run_id=preflight'])
    const draft = vi.mocked(post).mock.calls[0][1] as Record<string, unknown>
    expect(draft).not.toHaveProperty('write_strategy')
    expect(draft.target).toEqual(expect.objectContaining({ mode: 'REST', confirmation_value: true }))
    expect(draft.target).not.toHaveProperty('table_name')
  })

  it('binds a passing preflight to the original draft when fields change during validation', async () => {
    setupBuilder()
    const user = userEvent.setup()
    await completeBuilder(user)
    let resolve!: (value: unknown) => void
    vi.mocked(post).mockImplementation(() => new Promise(done => { resolve = done }))
    await user.click(screen.getByRole('button', { name: 'Preflight completo' }))
    await user.clear(screen.getByLabelText('Clave JSON de filas'))
    await user.type(screen.getByLabelText('Clave JSON de filas'), 'items')
    await act(async () => resolve({ id: 'preflight', status: 'QUEUED' }))
    await screen.findByText('Preflight:')
    expect(screen.getByRole('button', { name: 'Publicar configuración' })).toBeDisabled()
    expect(post).toHaveBeenCalledTimes(1)
  })
})
