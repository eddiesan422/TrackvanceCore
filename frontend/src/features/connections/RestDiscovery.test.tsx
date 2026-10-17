import { useState } from 'react'
import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { RestDiscovery } from './RestDiscovery'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn() }))
beforeEach(() => vi.clearAllMocks())
const field = { name: 'later', path: 'later', logical_type: 'STRING', nullable: true, null_policy: 'ALLOW', missing_policy: 'ERROR', value_count: 1, null_count: 0, missing_count: 1, incompatible_count: 0, array_count: 0, examples: ['000001'], warnings: [], selectable: true }

it('requires collection selection, separates apply from save and retains manually selected fields', async () => {
  vi.mocked(api).mockImplementation(async (_path, options) => ({ draft_id: JSON.parse(String(options?.body)).draft_id,
    collections: [{ path: 'customers', sampled_rows: 2, warnings: [], fields: [field] }, { path: 'orders', sampled_rows: 1, warnings: [], fields: [field] }], warnings: [], selection_required: true }))
  const apply = vi.fn(), user = userEvent.setup()
  renderApp(<RestDiscovery draft={{ name: 'Draft', source_type: 'REST', options: { url: 'https://example.test' } }} fields={[{ name: 'My identifier', path: 'id', logical_type: 'STRING', nullable: false }]} apply={apply}/>)
  await user.click(screen.getByRole('button', { name: 'Consultar API y detectar estructura' }))
  await screen.findByLabelText('Seleccionar colección')
  expect(screen.queryByRole('button', { name: 'Aplicar propuesta al formulario' })).not.toBeInTheDocument()
  await user.selectOptions(screen.getByLabelText('Seleccionar colección'), 'customers')
  expect(screen.getByRole('table', { name: 'Propuesta de estructura REST' })).toHaveTextContent('000001')
  await user.click(screen.getByRole('button', { name: 'Aplicar propuesta al formulario' }))
  expect(apply).toHaveBeenCalledWith('customers', [expect.objectContaining({ name: 'My identifier', path: 'id' }), expect.objectContaining({ path: 'later', logical_type: 'STRING' })])
  expect(api).toHaveBeenCalledTimes(1)
  expect(vi.mocked(api).mock.calls[0][0]).toBe('/connections/discover-rest')
})

it('rejects a late response after changing URL and invalidates a proposal immediately', async () => {
  let release: ((value: unknown) => void) | undefined
  let requestBody: { draft_id: string } | undefined
  vi.mocked(api).mockImplementation((_path, options) => { requestBody = JSON.parse(String(options?.body)); return new Promise(resolve => { release = resolve }) })
  function Harness() {
    const [url, setUrl] = useState('https://first.example.test')
    return <><input aria-label="URL" value={url} onChange={event => setUrl(event.target.value)}/><RestDiscovery draft={{ name: 'Draft', source_type: 'REST', options: { url } }} fields={[]} apply={vi.fn()}/></>
  }
  const user = userEvent.setup()
  renderApp(<Harness/>)
  await user.click(screen.getByRole('button', { name: 'Consultar API y detectar estructura' }))
  await user.clear(screen.getByLabelText('URL')); await user.type(screen.getByLabelText('URL'), 'https://second.example.test')
  await act(async () => release?.({ draft_id: requestBody!.draft_id, collections: [{ path: 'records', fields: [field], sampled_rows: 2, warnings: [] }], warnings: [] }))
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('El borrador cambió'))
  expect(screen.queryByRole('table', { name: 'Propuesta de estructura REST' })).not.toBeInTheDocument()
})

it('keeps saved field decisions when rediscovery proposes different names and types', async () => {
  vi.mocked(api).mockImplementation(async (_path, options) => ({ draft_id: JSON.parse(String(options?.body)).draft_id,
    collections: [{ path: 'records', sampled_rows: 2, warnings: [], fields: [field] }], warnings: [], selection_required: false }))
  const apply = vi.fn(), user = userEvent.setup()
  const manual = { name: 'Manual alias', path: 'later', logical_type: 'INT64', nullable: false }
  renderApp(<RestDiscovery draft={{ name: 'Draft' }} fields={[manual]} apply={apply}/>)
  await user.click(screen.getByRole('button', { name: 'Consultar API y detectar estructura' }))
  expect(await screen.findByText(/Propuestas distintas/)).toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Aplicar propuesta al formulario' }))
  expect(apply).toHaveBeenCalledWith('records', [manual])
})
