import { useState } from 'react'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { ClassificationField, unclassified } from './ClassificationField'
import type { Classification } from './ClassificationField'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))
function Control() { const [value, setValue] = useState<Classification>(unclassified); return <><ClassificationField value={value} onChange={setValue}/><output>{JSON.stringify(value)}</output></> }
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockImplementation(async path => path.startsWith('/governance/macrodomains') ? { items: [{ id: 'finance', name: 'Finanzas', active: true, version: 1 }, { id: 'risk', name: 'Riesgos', active: true, version: 1 }] } : { items: [{ id: path.includes('finance') ? 'sales' : 'credit', name: path.includes('finance') ? 'Ventas' : 'Crédito', active: true, version: 1 }] })
})

it('allows incomplete classification and only fetches children after choosing their parent', async () => {
  const user = userEvent.setup(); renderApp(<Control/>)
  expect(screen.getByLabelText('Dominio (opcional)')).toBeDisabled()
  expect(vi.mocked(api).mock.calls.some(([path]) => path.startsWith('/governance/domains'))).toBe(false)
  await screen.findByRole('option', { name: 'Finanzas' })
  await user.selectOptions(screen.getByLabelText('Macrodominio (opcional)'), 'finance')
  await screen.findByRole('option', { name: 'Ventas' })
  await user.selectOptions(screen.getByLabelText('Dominio (opcional)'), 'sales')
  await user.selectOptions(screen.getByLabelText('Macrodominio (opcional)'), 'risk')
  await screen.findByRole('option', { name: 'Crédito' })
  expect(screen.getByLabelText('Dominio (opcional)')).toHaveValue('')
  expect(screen.getByText('{"macro_domain_id":"risk","domain_id":null}')).toBeInTheDocument()
})

it('uses shared creation only with the entity permission and keeps duplicate diagnostics inline', async () => {
  const user = userEvent.setup(); vi.mocked(post).mockRejectedValueOnce(new Error('Ya existe un macrodominio con ese nombre.')).mockResolvedValueOnce({ id: 'new', name: 'Cobranza', active: true, version: 1 })
  renderApp(<Control/>, { permissions: ['datasets:write', 'domains:manage'] })
  await user.click(screen.getByRole('button', { name: 'Crear macrodominio' }))
  await user.type(screen.getByLabelText('Nombre'), 'Cobranza')
  await user.click(screen.getByRole('button', { name: 'Crear' }))
  expect(await screen.findByText('Ya existe un macrodominio con ese nombre.')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Crear' }))
  await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  expect(post).toHaveBeenLastCalledWith('/governance/macrodomains', { name: 'Cobranza' })
  expect(screen.getByText('{"macro_domain_id":"new","domain_id":null}')).toBeInTheDocument()
})

it('does not offer creation without permission and preserves legacy area as read only', () => {
  renderApp(<ClassificationField value={unclassified} onChange={vi.fn()} fixed legacyValue="Operaciones históricas"/>, { permissions: ['datasets:write'] })
  expect(screen.queryByRole('button', { name: 'Crear macrodominio' })).not.toBeInTheDocument()
  expect(screen.queryByLabelText('Área de negocio')).not.toBeInTheDocument()
  expect(screen.getByText(/Área heredada: Operaciones históricas/)).toBeVisible()
  expect(api).not.toHaveBeenCalled()
})
