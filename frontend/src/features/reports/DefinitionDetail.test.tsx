import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, ApiError, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { DefinitionDetail } from './Reports'
import { emptyDraft, executableDraft } from './types'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn(), download: vi.fn() }))
const draft = { ...emptyDraft(), mode: 'SQL' as const, sql: 'SELECT id FROM base ORDER BY id', sources: [{ alias: 'base', input_dataset_id: 'input', contract_id: 'contract', contract_revision_ids: ['admitted'], policy: 'LATEST_APPROVED' as const }], expected_schemas: { base: [{ name: 'id', logical_type: 'STRING' }] }, output_preferences: { format: 'XLSX' as const } }
const old = { id: 'revision-old', version: 1, draft, query_hash: 'old-hash', created_at: '2026-01-01T00:00:00Z' }
const latest = { ...old, id: 'revision-current', version: 50, draft: { ...draft, sql: 'SELECT amount FROM base ORDER BY amount' }, query_hash: 'new-hash' }
const definition = { id: 'saved', name: 'Reporte guardado', version: 50, revision_total: 50, revisions: [latest], selected_revision: old }
const permissions = ['reports:read', 'reports:write', 'reports:preview']
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockImplementation(async path => path.startsWith('/reports/definitions/saved?') ? definition : path === '/reports/limits' ? { profiles: {} } : { items: [], total: 0 })
  vi.mocked(post).mockImplementation(async path => path === '/reports/resolve' ? { context_id: 'frozen', expires_at: '2099-01-01T00:00:00Z', sources: [], warnings: [], query_hash: 'old-hash' } : { id: 'saved' })
})

it('resolves the exact linked historical revision and saves from the current optimistic version', async () => {
  renderApp(<DefinitionDetail/>, { permissions, path: '/reports/definitions/saved?revision_id=revision-old', route: '/reports/definitions/:id' })
  const user = userEvent.setup()
  await screen.findByRole('heading', { name: 'Reporte guardado · revisión 1' })
  expect(api).toHaveBeenCalledWith('/reports/definitions/saved?revision_offset=0&revision_limit=25&revision_id=revision-old')
  expect(screen.getByText(/La revisión vigente es 50/)).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Resolver fuentes y validar' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/reports/resolve', { draft: executableDraft(draft), revision_id: 'revision-old' }))
  expect(screen.getByRole('combobox', { name: 'Formato de descarga' })).toHaveValue('XLSX')
  await user.click(screen.getByRole('button', { name: 'Guardar definición' }))
  await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Guardar definición' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/reports/definitions/saved/revisions', { expected_version: 50, draft: executableDraft(draft) }))
})

it('pages revision history without replacing the selected revision or discarding local edits', async () => {
  vi.mocked(api).mockImplementation(async path => path.startsWith('/reports/definitions/saved?') ? { ...definition, selected_revision: latest, revisions: path.includes('revision_offset=25') ? [old] : [latest] } : { items: [], total: 0 })
  renderApp(<DefinitionDetail/>, { permissions, path: '/reports/definitions/saved', route: '/reports/definitions/:id' })
  const user = userEvent.setup()
  await screen.findByRole('heading', { name: 'Reporte guardado · revisión 50' })
  await user.click(screen.getByRole('button', { name: '3. Columnas y filtros' }))
  const sql = screen.getByRole('textbox', { name: 'Consulta SQL' })
  await user.clear(sql); await user.type(sql, 'SELECT id FROM base ORDER BY id DESC')
  await user.click(screen.getByText('Historial de revisiones · 50'))
  await user.click(screen.getByRole('button', { name: 'Página siguiente' }))
  await screen.findByRole('link', { name: 'Revisión 1' })
  expect(screen.getByRole('heading', { name: 'Reporte guardado · revisión 50' })).toBeVisible()
  expect(screen.getByRole('textbox', { name: 'Consulta SQL' })).toHaveValue('SELECT id FROM base ORDER BY id DESC')
  await user.click(screen.getByRole('link', { name: 'Revisión 1' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/reports/definitions/saved?revision_offset=25&revision_limit=25&revision_id=revision-old'))
})

it('does not fall back to the latest revision when an exact link is unauthorized', async () => {
  vi.mocked(api).mockRejectedValue(new ApiError('Revisión no encontrada.', 404))
  renderApp(<DefinitionDetail/>, { permissions, path: '/reports/definitions/saved?revision_id=foreign', route: '/reports/definitions/:id' })
  await screen.findByText('Revisión no encontrada.')
  expect(screen.queryByRole('button', { name: 'Resolver fuentes y validar' })).not.toBeInTheDocument()
})
