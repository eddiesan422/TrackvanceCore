import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { DeliveryValidation } from './DeliveryValidation'
import { DeliveryValidationDetail } from './DeliveryValidationDetail'
import type { DeliveryDraft } from './types'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))
const draft: DeliveryDraft = { schema_version: 1, audit_columns_enabled: false, dataset_version_id: 'version', destination_id: 'destination', destination_version_id: 'revision', target: { mode: 'EXISTING_TABLE', schema_name: 'public', table_name: 'orders', create_schema: false }, columns: [{ source_name: 'id', target_name: 'id', target_type: 'STRING', ordinal: 0, nullable: false }], write_strategy: 'APPEND', upsert_keys: [] }
beforeEach(() => vi.resetAllMocks())

it('uses a completed validation only for its exact immutable draft', async () => {
  const result = { status: 'PASS' as const, checks: [] }, onUse = vi.fn()
  vi.mocked(api).mockResolvedValue({ items: [{ id: 'validation', status: 'SUCCESS', dataset_version_id: 'version', created_at: '2026-10-03T12:00:00Z', result, draft }], total: 1 })
  const user = userEvent.setup()
  function ChangingDraft() {
    const [value, setValue] = useState(draft)
    return <><button onClick={() => setValue({ ...draft, write_strategy: 'OVERWRITE' })}>Cambiar borrador</button><DeliveryValidation draft={value} ready onUse={onUse}/></>
  }
  renderApp(<ChangingDraft/>)
  const use = await screen.findByRole('button', { name: 'Ver y usar resultado' })
  await user.click(use)
  expect(onUse).toHaveBeenCalledWith(result, 'validation')
  await user.click(screen.getByRole('button', { name: 'Cambiar borrador' }))
  expect(screen.getByRole('button', { name: 'Ver y usar resultado' })).toBeDisabled()
})

it('registers and cancels persistent work independently of the preview', async () => {
  vi.mocked(api).mockResolvedValue({ items: [{ id: 'active', status: 'RUNNING', stage: 'Validando toda la población', dataset_version_id: 'version', created_at: '2026-10-03T12:00:00Z' }], total: 1 })
  vi.mocked(post).mockResolvedValue({ id: 'active' })
  const user = userEvent.setup()
  renderApp(<DeliveryValidation draft={draft} ready onUse={vi.fn()}/>)
  await user.click(screen.getByRole('button', { name: 'Registrar preflight completo' }))
  expect(post).toHaveBeenCalledWith('/delivery/validations', draft)
  await user.click(await screen.findByRole('button', { name: 'Cancelar validación' }))
  expect(post).toHaveBeenCalledWith('/delivery/validations/active/cancel')
})

it('keeps technical success and a failing complete preflight visible after navigation', async () => {
  vi.mocked(api).mockResolvedValue({ id: 'failed-check', status: 'SUCCESS', stage: 'Terminado', dataset_version_id: 'version', created_at: '2026-10-03T12:00:00Z', result: { status: 'FAIL', checks: [{ code: 'VALUES', status: 'FAIL', message: 'La población contiene valores incompatibles.' }] } })
  renderApp(<DeliveryValidationDetail/>, { path: '/delivery/validation/failed-check', route: '/delivery/validation/:id', permissions: ['delivery:read'] })
  expect(await screen.findByText('La población contiene valores incompatibles.')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Cancelar validación' })).not.toBeInTheDocument()
  expect(screen.getByText(/SUCCESS indica que la validación terminó/)).toBeInTheDocument()
})
