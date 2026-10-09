import { act, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { CatalogDataset, CatalogRoutes } from './Catalog'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn() }))
beforeEach(() => vi.clearAllMocks())

it('links the exact approved child while keeping the original unapproved', async () => {
  vi.mocked(api).mockImplementation(async path => path.endsWith('/blocks') ? { items: [], total: 0 } : {
    dataset: { id: 'input', name: 'Entrada' }, governance: {}, items: [], total: 0,
    eligibility: { strict_approval: { approved: false }, eligible: false, reasons: [{ message: 'La entrada tiene una salida aprobada exacta.' }],
      related_approved_output: { output_dataset_id: 'approved-output', output_version_id: 'frozen-version', approval_run_id: 'verified-run' } },
  })
  renderApp(<CatalogDataset/>, { path: '/catalog/datasets/input?version_id=input-version', route: '/catalog/datasets/:id', permissions: ['catalog:read'] })
  expect(await screen.findByRole('link', { name: 'Ver salida aprobada' })).toHaveAttribute('href', '/catalog/datasets/approved-output?version_id=frozen-version')
  expect(screen.getByRole('link', { name: 'Ver validación Intake' })).toHaveAttribute('href', '/runs/verified-run')
  expect(screen.getByText('Cierre histórico sin validar')).toBeVisible()
})

it('loads tree branches only when expanded and keeps resource type leaves scoped to their domain', async () => {
  vi.mocked(api).mockImplementation(async path => {
    if (path.startsWith('/catalog/resources')) return { items: [], total: 0 }
    if (path.includes('parent_type=macro_domain')) return { items: [{ id: 'domain', label: 'Ventas', type: 'domain', count: 2, has_children: true }], total: 1 }
    if (path.includes('parent_type=domain')) return { items: [{ id: 'DATASET', label: 'Datasets', type: 'resource_type', count: 2, has_children: true }], total: 1 }
    return { items: [{ id: 'macro', label: 'Finanzas', type: 'macro_domain', count: 2, has_children: true }], total: 1 }
  })
  const user = userEvent.setup(); renderApp(<CatalogRoutes/>, { path: '/catalog', route: '/catalog/*', permissions: ['catalog:read'] })
  const root = await screen.findByRole('button', { name: /Finanzas/ })
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('parent_type='))).toBe(false)
  await user.click(root); await user.click(await screen.findByRole('button', { name: /Ventas/ })); await user.click(await screen.findByRole('button', { name: /Datasets 2/ }))
  await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path]) => path.startsWith('/catalog/resources?') && path.includes('domain_id=domain') && path.includes('resource_type=DATASET'))).toBe(true))
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('parent_type=resource_type'))).toBe(false)
})

it('keeps deep section links and server pagination without fetching all historical versions', async () => {
  vi.mocked(api).mockResolvedValue({ dataset: { id: 'dataset', name: 'Fuente' }, governance: {}, items: [{ id: 'v3', version: 3, source_type: 'REPORT_OUTPUT', row_count: 12 }], total: 90 })
  const user = userEvent.setup(); renderApp(<CatalogDataset/>, { path: '/catalog/datasets/dataset?section=versions&offset=25', route: '/catalog/datasets/:id' })
  expect(await screen.findByText('v3')).toBeVisible()
  expect(api).toHaveBeenCalledWith('/catalog/datasets/dataset?section=versions&offset=25&limit=25')
  await user.click(screen.getByRole('button', { name: 'Página siguiente' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/catalog/datasets/dataset?section=versions&offset=50&limit=25'))
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/profile'))).toBe(false)
})

it('keeps historical strict approval and current eligibility separate and hides unauthorized edits', async () => {
  vi.mocked(api).mockImplementation(async path => path.endsWith('/blocks') ? { items: [], total: 0 } : { dataset: { id: 'dataset', name: 'Fuente', domain: 'Área histórica' }, governance: { classification_complete: true }, items: [], total: 0, eligibility: { strict_approval: { approved: false }, classification_complete: true, availability: { available: true, verified_bytes: false }, eligible: false, reasons: [{ code: 'NO_STRICT_APPROVAL', message: 'Evidencia insuficiente.' }] } })
  renderApp(<CatalogDataset/>, { path: '/catalog/datasets/dataset', route: '/catalog/datasets/:id', permissions: ['catalog:read'] })
  expect(await screen.findByText('Evidencia insuficiente.')).toBeVisible()
  expect(screen.getByText('Cierre histórico sin validar')).toBeVisible()
  expect(screen.getByText('Bloqueada')).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Editar gobierno' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Bloquear uso' })).not.toBeInTheDocument()
})

it('documents the exact selected version without changing physical schema or glossary identity', async () => {
  vi.mocked(api).mockImplementation(async path => path.startsWith('/governance/glossary') ? { items: [], total: 0 } : { dataset: { id: 'dataset', name: 'Fuente' }, governance: {}, version_id: 'historical-version', dataset_terms: [], items: [{ name: 'customer_id', logical_type: 'STRING', documentation: { description: 'Identidad histórica', version: 3 }, terms: [{ id: 'term-id', name: 'Cliente', active: false }] }], total: 1 })
  renderApp(<CatalogDataset/>, { path: '/catalog/datasets/dataset?section=columns&version_id=historical-version', route: '/catalog/datasets/:id', permissions: ['catalog:read', 'governance:write', 'glossary:read'] })
  const user = userEvent.setup()
  await user.click(await screen.findByRole('button', { name: 'Documentar columna' }))
  expect(await screen.findByRole('checkbox', { name: 'Cliente (inactivo; asociación histórica)' })).toBeChecked()
  await user.clear(screen.getByLabelText('Descripción funcional de la columna'))
  await user.type(screen.getByLabelText('Descripción funcional de la columna'), 'Identificador del cliente')
  await user.click(screen.getByRole('button', { name: 'Guardar documentación' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/catalog/datasets/dataset/columns', { method: 'PATCH', body: JSON.stringify({ version_id: 'historical-version', column_name: 'customer_id', description: 'Identificador del cliente', term_ids: ['term-id'], expected_version: 3 }) }))
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/profile'))).toBe(false)
})

it('preserves keyboard focus while a different deep section is loading', async () => {
  const payload = { dataset: { id: 'dataset', name: 'Fuente' }, governance: {}, items: [], total: 0 }
  let finish: ((value: typeof payload) => void) | undefined
  vi.mocked(api).mockImplementation(async path => path.includes('section=versions') ? await new Promise<typeof payload>(resolve => { finish = resolve }) : payload)
  renderApp(<CatalogDataset/>, { path: '/catalog/datasets/dataset?section=columns', route: '/catalog/datasets/:id', permissions: ['catalog:read'] })
  const user = userEvent.setup(), columns = await screen.findByRole('tab', { name: 'Columnas y glosario' })
  columns.focus()
  await user.keyboard('{ArrowRight}')
  await waitFor(() => expect(finish).toBeDefined())
  expect(screen.getByRole('tab', { name: 'Versiones' })).toHaveFocus()
  expect(screen.getByText('Consultando sección…')).toBeVisible()
  await act(async () => { finish!(payload) })
  expect(screen.getByRole('tab', { name: 'Versiones' })).toHaveFocus()
  await user.keyboard('{Home}')
  expect(await screen.findByRole('tab', { name: 'Resumen y gobierno' })).toHaveFocus()
})
