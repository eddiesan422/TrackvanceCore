import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, ApiError, download, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { ReportBuilder } from './Reports'
import { draftError, emptyDraft, executableDraft, mapFilterSources } from './types'
import type { ReportDraft } from './types'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn(), download: vi.fn() }))
const draft: ReportDraft = { ...emptyDraft(), sources: [{ alias: 'base', input_dataset_id: 'input', contract_id: 'contract', contract_revision_ids: ['revision'], policy: 'LATEST_APPROVED' }], columns: [{ source_alias: 'base', column: 'id', alias: 'identifier' }], order_by: [{ source_alias: 'base', column: 'id', direction: 'ASC' }], expected_schemas: { base: [{ name: 'id', logical_type: 'STRING' }] } }
const context = { context_id: 'frozen', expires_at: '2099-01-01T00:00:00Z', sources: [{ alias: 'base', input_version_id: 'v-input', output_version_id: 'v-output', contract_id: 'contract', contract_revision_id: 'revision', approval_run_id: 'approval' }], warnings: ['Existe una entrada posterior pendiente.'], query_hash: 'query' }
const preview = { execution_id: 'preview-run', status: 'SUCCESS', rows: [{ identifier: '001234567', amount: '1234567890123456.78' }], columns: [{ name: 'identifier', type: 'VARCHAR' }, { name: 'amount', type: 'DECIMAL(38,2)' }], cardinality: [], warnings: [] }
const permissions = ['reports:read', 'reports:write', 'reports:preview', 'reports:download', 'reports:generate', 'datasets:read']
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockImplementation(async path => path === '/reports/limits' ? { profiles: { PREVIEW: { max_sources: 8, max_rows: 10 } } } : { items: [], total: 0 })
  vi.mocked(post).mockImplementation(async path => path === '/reports/resolve' ? context : preview)
  vi.mocked(download).mockResolvedValue(undefined)
})

async function resolveAndPreview() {
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'Resolver fuentes y validar' }))
  await user.click(await screen.findByRole('button', { name: 'Ejecutar vista previa' }))
  await screen.findByText('001234567')
  return user
}

it('uses frozen approved outputs for separate preview and download actions while preserving exact text', async () => {
  renderApp(<ReportBuilder initial={draft}/>, { permissions })
  const user = await resolveAndPreview()
  expect(post).toHaveBeenCalledWith('/reports/resolve', { draft: executableDraft(draft) })
  expect(post).toHaveBeenCalledWith('/reports/preview', { context_id: 'frozen' })
  expect(screen.getByText('1234567890123456.78')).toBeInTheDocument()
  expect(screen.getByText('Existe una entrada posterior pendiente.')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Descargar reporte' }))
  await waitFor(() => expect(download).toHaveBeenCalledWith('/reports/download', 'reporte.csv', expect.objectContaining({ method: 'POST', body: JSON.stringify({ context_id: 'frozen', format: 'CSV' }), signal: expect.any(AbortSignal) })))
  expect(vi.mocked(post).mock.calls.some(([path]) => path === '/reports/definitions' || path === '/reports/datasets')).toBe(false)
})

it('invalidates preview and output actions when query, aliases or parameter values change', async () => {
  renderApp(<ReportBuilder initial={draft}/>, { permissions })
  const user = await resolveAndPreview()
  expect(screen.getByRole('button', { name: 'Generar dataset' })).toBeEnabled()
  await user.click(screen.getByRole('button', { name: 'Modo SQL' }))
  expect(screen.queryByText('001234567')).not.toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Generar dataset' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Descargar reporte' })).toBeDisabled()
})

it.each([[true, 'REPORT_MEMORY_LIMIT'], [false, 'REPORT_SQL_INVALID']])('allows durable generation only for a resource failure (%s)', async (allow, code) => {
  vi.mocked(post).mockImplementation(async path => { if (path === '/reports/resolve') return context; throw new ApiError('Consulta no completada.', 422, undefined, code, { context_id: 'frozen', allow_generate: allow }) })
  renderApp(<ReportBuilder initial={draft}/>, { permissions })
  const user = userEvent.setup(); await user.click(screen.getByRole('button', { name: 'Resolver fuentes y validar' })); await user.click(await screen.findByRole('button', { name: 'Ejecutar vista previa' })); await screen.findByText('Consulta no completada.')
  if (allow) expect(screen.getByRole('button', { name: 'Generar dataset' })).toBeEnabled(); else expect(screen.getByRole('button', { name: 'Generar dataset' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Descargar reporte' })).toBeDisabled()
})

it('separates download from publish permission even with a valid preview', async () => {
  renderApp(<ReportBuilder initial={draft}/>, { permissions: ['reports:read', 'reports:preview', 'reports:download'] })
  await resolveAndPreview()
  expect(screen.getByRole('button', { name: 'Descargar reporte' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Generar dataset' })).toBeDisabled()
  expect(screen.queryByRole('button', { name: 'Guardar definición' })).not.toBeInTheDocument()
})

it('resolves and downloads independently of preview permission', async () => {
  renderApp(<ReportBuilder initial={draft}/>, { permissions: ['reports:read', 'reports:download'] })
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'Resolver fuentes y validar' }))
  expect(await screen.findByRole('button', { name: 'Ejecutar vista previa' })).toBeDisabled()
  expect(screen.getByRole('button', { name: 'Descargar reporte' })).toBeEnabled()
  expect(screen.getByRole('button', { name: 'Generar dataset' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Descargar reporte' }))
  await waitFor(() => expect(download).toHaveBeenCalled())
  expect(post).not.toHaveBeenCalledWith('/reports/preview', expect.anything())
})

it('cancels the client transfer without publishing a dataset', async () => {
  let signal: AbortSignal | undefined
  vi.mocked(download).mockImplementation(async (_path, _name, options) => {
    signal = options?.signal as AbortSignal
    await new Promise<void>((_resolve, reject) => signal!.addEventListener('abort', () => reject(new ApiError('Transferencia cancelada.', 0, undefined, 'TRANSFER_CANCELLED'))))
  })
  renderApp(<ReportBuilder initial={draft}/>, { permissions })
  const user = await resolveAndPreview()
  await user.click(screen.getByRole('button', { name: 'Descargar reporte' }))
  await user.click(await screen.findByRole('button', { name: 'Cancelar transferencia' }))
  expect(await screen.findByText('Transferencia cancelada.')).toBeVisible()
  expect(signal?.aborted).toBe(true)
  expect(vi.mocked(post).mock.calls.some(([path]) => path === '/reports/datasets')).toBe(false)
})

it('renames and removes source references through nested filters', () => {
  const group = { operator: 'AND' as const, conditions: [{ source_alias: 'base', column: 'id', operator: 'EQ' as const, value: '001' }, { operator: 'OR' as const, conditions: [{ source_alias: 'second', column: 'id', operator: 'IS_NULL' as const }] }] }
  expect(mapFilterSources(group, alias => alias === 'base' ? 'renamed' : alias)?.conditions[0]).toMatchObject({ source_alias: 'renamed', value: '001' })
  expect(mapFilterSources(group, alias => alias === 'second' ? null : alias)?.conditions).toEqual([group.conditions[0]])
})

it('restores and saves the output preference in the reusable definition', async () => {
  vi.mocked(post).mockImplementation(async path => path === '/reports/definitions' ? { id: 'saved' } : context)
  const value: ReportDraft = { ...draft, output_preferences: { format: 'XLSX' } }
  renderApp(<ReportBuilder initial={value}/>, { permissions })
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: '4. Vista previa y salida' }))
  expect(screen.getByLabelText('Formato de descarga')).toHaveValue('XLSX')
  await user.click(screen.getByRole('button', { name: 'Guardar definición' }))
  const dialog = within(screen.getByRole('dialog'))
  await user.type(dialog.getByLabelText('Nombre del reporte'), 'Preferencia exacta')
  await user.click(dialog.getByRole('button', { name: 'Guardar definición' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/reports/definitions', { name: 'Preferencia exacta', description: '', draft: executableDraft(value) }))
  expect(post).not.toHaveBeenCalledWith('/reports/preview', expect.anything())
})

it('validates three source chains and blocks incomplete, duplicate alias or unauthorized N:M joins', () => {
  const three: ReportDraft = { ...draft, sources: ['base', 'second', 'third'].map(alias => ({ ...draft.sources[0], alias })), joins: [{ left_alias: 'base', right_alias: 'second', type: 'LEFT', keys: [{ left_column: 'id', right_column: 'id' }, { left_column: 'date', right_column: 'date' }], expected_cardinality: '1:N', allow_many_to_many: false }, { left_alias: 'second', right_alias: 'third', type: 'FULL', keys: [{ left_column: 'id', right_column: 'id' }], expected_cardinality: 'N:1', allow_many_to_many: false }] }
  expect(draftError(three)).toBe('')
  expect(draftError({ ...three, joins: three.joins.slice(0, 1) })).toMatch(/cruces completos/)
  expect(draftError({ ...three, sources: three.sources.map((source, index) => index === 1 ? { ...source, alias: 'BASE' } : source) })).toMatch(/alias único/)
  expect(draftError({ ...three, joins: three.joins.map((join, index) => index === 0 ? { ...join, expected_cardinality: 'N:M' } : join) })).toMatch(/autorización explícita/)
  expect(executableDraft({ ...draft, source_filters: { base: { operator: 'AND', conditions: [] } }, post_filter: { operator: 'OR', conditions: [{ operator: 'AND', conditions: [] }] } })).toMatchObject({ source_filters: {}, post_filter: undefined })
})
