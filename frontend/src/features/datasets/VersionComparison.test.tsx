import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { VersionComparison } from './VersionComparison'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))
const columns = [{ name: 'id', logical_type: 'STRING' }, { name: 'amount', logical_type: 'DECIMAL' }]
const versions = [{ id: 'v2', version: 2, created_at: '2026-10-16T13:00:00Z', row_count: 1000000, schema: columns }, { id: 'v1', version: 1, created_at: '2026-10-15T13:00:00Z', row_count: 1000000, schema: columns }]
const comparison = { id: 'report', status: 'SUCCESS', before_version_id: 'v1', after_version_id: 'v2', created_at: '2026-10-16T13:01:00Z', progress_percent: 100, progress_stage: 'Informe completo disponible', summary: { mode: 'KEYED', before_rows: 1000000, after_rows: 1000000, counts: { ADDED: 0, ABSENT: 0, MODIFIED: 1, UNCHANGED: 999999 }, warnings: ['INDEPENDENT_SCOPES: La ausencia no demuestra eliminación.'], schema_changes: [], elapsed_seconds: 20 } }

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api).mockImplementation(async path => {
    if (path === '/datasets/orders') return { id: 'orders', name: 'Órdenes', versions }
    if (path === '/datasets/orders/comparisons') return { items: [] }
    if (path === '/version-comparisons/report') return comparison
    if (path.startsWith('/version-comparisons/report/rows')) return { total: 1, records: 1, items: [{ id: 1, kind: 'MODIFIED', multiplicity: 1, before: { id: '000001', amount: '1.01' }, after: { id: '000001', amount: '1.02' }, changed_columns: ['amount'] }] }
    throw new Error(`Unexpected request ${path}`)
  })
})

it('queues declared keys with immutable version identities and only fetches a page of evidence', async () => {
  const user = userEvent.setup()
  vi.mocked(post).mockResolvedValue(comparison)
  renderApp(<VersionComparison />, { route: '/datasets/:datasetId/versions/compare', path: '/datasets/orders/versions/compare?from=v1' })
  await screen.findByText('id (STRING)')
  await user.click(screen.getByRole('checkbox', { name: 'id (STRING)' }))
  await user.click(screen.getByRole('button', { name: 'Comparar poblaciones completas' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/datasets/orders/comparisons', { before_version_id: 'v1', after_version_id: 'v2', keys: ['id'] }))
  expect(await screen.findByText('INDEPENDENT_SCOPES: La ausencia no demuestra eliminación.')).toBeVisible()
  expect(screen.getByRole('link', { name: 'Descargar informe completo (SQLite)' })).toHaveAttribute('href', '/api/v1/version-comparisons/report/download')
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/rows?') && path.includes('limit=50'))).toBe(true)
  expect(vi.mocked(api).mock.calls.some(([path]) => path.includes('/profile') || path.includes('/data?'))).toBe(false)
})

it('does not allow a comparison with a single version', async () => {
  vi.mocked(api).mockImplementation(async path => path === '/datasets/orders' ? { id: 'orders', name: 'Órdenes', versions: [versions[0]] } : { items: [] })
  renderApp(<VersionComparison />, { route: '/datasets/:datasetId/versions/compare', path: '/datasets/orders/versions/compare' })
  expect(await screen.findByRole('heading', { name: 'Necesitas dos versiones' })).toBeVisible()
  expect(screen.queryByRole('button', { name: 'Comparar poblaciones completas' })).not.toBeInTheDocument()
})

it('keeps a denied evidence report visible as an error without offering downloads', async () => {
  vi.mocked(api).mockImplementation(async path => {
    if (path === '/version-comparisons/report') throw new Error('Acceso a la población revocado')
    return path === '/datasets/orders' ? { id: 'orders', name: 'Órdenes', versions } : { items: [] }
  })
  renderApp(<VersionComparison />, { route: '/datasets/:datasetId/versions/compare', path: '/datasets/orders/versions/compare?report=report' })
  expect(await screen.findByText('Acceso a la población revocado')).toBeVisible()
  expect(screen.queryByRole('link', { name: 'Descargar informe completo (SQLite)' })).not.toBeInTheDocument()
})
