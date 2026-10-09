import { useState } from 'react'
import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { PersonSelect } from './PersonSelect'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))
beforeEach(() => vi.clearAllMocks())

function Form({ initial = null }: { initial?: string | null }) {
  const [person, setPerson] = useState<string | null>(initial)
  return <><input aria-label="Descripción pendiente" defaultValue="Sin guardar"/><PersonSelect label="Responsable de negocio" value={person} onChange={setPerson}/><output>{person}</output></>
}

it('creates a shared person and selects it while preserving unsaved surrounding form', async () => {
  vi.mocked(api).mockResolvedValue({ items: [], total: 0 })
  vi.mocked(post).mockResolvedValue({ id: 'new-person', name: 'Lucía', active: true, version: 1 })
  renderApp(<Form/>, { permissions: ['people:read', 'people:manage'] })
  const user = userEvent.setup()
  await user.type(screen.getByLabelText('Descripción pendiente'), ' · Continuar')
  await user.click(screen.getByRole('button', { name: 'Agregar nuevo · Responsable de negocio' }))
  await user.type(screen.getByLabelText('Nombre de la persona'), 'Lucía')
  await user.click(screen.getByRole('button', { name: 'Agregar persona' }))
  await waitFor(() => expect(screen.getByLabelText('Responsable de negocio')).toHaveValue('new-person'))
  expect(screen.getByLabelText('Descripción pendiente')).toHaveValue('Sin guardar · Continuar')
  expect(post).toHaveBeenCalledWith('/governance/people', { name: 'Lucía', reference: null, email: null, user_id: null })
})

it('shows inactive historical assignments and paginates the active catalog', async () => {
  vi.mocked(api).mockImplementation(async path => path === '/governance/people/history' ? { id: 'history', name: 'Persona histórica', active: false, version: 2 } : { items: [{ id: 'active', name: 'Persona nueva', active: true, version: 1 }], total: 40 })
  renderApp(<Form initial="history"/>, { permissions: ['people:read'] })
  expect(await screen.findByRole('option', { name: 'Persona histórica (inactiva; asignación conservada)' })).toBeVisible()
  expect(screen.getByLabelText('Responsable de negocio')).toHaveValue('history')
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: 'Página siguiente' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/governance/people?active=true&search=&offset=25&limit=25'))
  expect(screen.queryByRole('button', { name: /Agregar nuevo/ })).not.toBeInTheDocument()
  await user.selectOptions(screen.getByLabelText('Responsable de negocio'), '')
  expect(screen.getByLabelText('Responsable de negocio')).toHaveValue('')
})

it('reuses a linked access account by selection without a manual identity field', async () => {
  vi.mocked(api).mockImplementation(async path => path.startsWith('/governance/people/users') ? { items: [{ id: 'account', name: 'Ana', email: 'ana@example.test', person_id: 'existing-person' }], total: 1 } : { items: [], total: 0 })
  vi.mocked(post).mockResolvedValue({ id: 'existing-person', name: 'Ana', user_id: 'account', active: true, version: 1 })
  renderApp(<Form/>, { permissions: ['people:read', 'people:manage'] })
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: /Agregar nuevo/ }))
  await user.click(screen.getByRole('checkbox', { name: 'Vincular con una cuenta existente' }))
  await user.selectOptions(await screen.findByLabelText('Cuenta existente'), 'account')
  await user.click(screen.getByRole('button', { name: 'Agregar persona' }))
  await waitFor(() => expect(screen.getByLabelText('Responsable de negocio')).toHaveValue('existing-person'))
  expect(post).toHaveBeenCalledWith('/governance/people', { name: undefined, reference: null, email: null, user_id: 'account' })
})
