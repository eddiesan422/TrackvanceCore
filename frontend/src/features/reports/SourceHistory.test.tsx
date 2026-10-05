import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { SourceHistory } from './SourceHistory'
import type { ReportSource, SourceCandidate } from './types'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn() }))
beforeEach(() => vi.clearAllMocks())
const source: ReportSource = { alias: 'base', input_dataset_id: 'input', contract_id: 'contract', contract_revision_ids: ['revision-selected'], policy: 'SPECIFIC', input_version_id: 'selected-version' }
const candidate: SourceCandidate = { input_dataset_id: 'input', name: 'Fuente', contract_id: 'contract', contract_name: 'Calidad', contract_revision_ids: ['revision-selected'], versions: [{ id: 'selected-version', version: 300 }], contract_revision_total: 150, contract_revision_limit: 100, version_total: 300, version_limit: 100, schema: [], eligibility: { eligible: true, reasons: [] } }

it('loads only the requested page for the exact contract and preserves admitted selections', async () => {
  const loaded = { ...candidate, version_offset: 100, versions: [{ id: 'older-version', version: 199 }] }, onLoaded = vi.fn()
  vi.mocked(api).mockResolvedValue({ items: [loaded], total: 1 })
  renderApp(<SourceHistory source={source} candidate={candidate} onLoaded={onLoaded}/>)
  const user = userEvent.setup()
  await user.click(within(screen.getByRole('group', { name: 'Página de versiones de entrada' })).getByRole('button', { name: 'Página siguiente' }))
  await waitFor(() => expect(onLoaded).toHaveBeenCalledWith(loaded))
  expect(api).toHaveBeenCalledWith('/reports/sources?contract_id=contract&offset=0&limit=1&revision_offset=0&version_offset=100')
  expect(source.input_version_id).toBe('selected-version')
  expect(source.contract_revision_ids).toEqual(['revision-selected'])
})

it('reports a removed or unauthorized source instead of replacing it with another source', async () => {
  vi.mocked(api).mockResolvedValue({ items: [], total: 0 })
  const onLoaded = vi.fn(); renderApp(<SourceHistory source={source} onLoaded={onLoaded}/>)
  await userEvent.setup().click(screen.getByRole('button', { name: 'Consultar historial de esta fuente' }))
  expect(await screen.findByText(/La fuente ya no aparece en el catálogo autorizado/)).toBeVisible()
  expect(onLoaded).not.toHaveBeenCalled()
})
