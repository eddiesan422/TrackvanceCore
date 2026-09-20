import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { UsersPanel } from './UsersPanel'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn() }))
const account = { id: 'user-1', name: 'Ana', email: 'ana@example.test', role: 'Data Analyst', active: true, version: 2, permissions: ['datasets:read', 'runs:execute'] }
beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === '/users' && !options) return { items: [account], total: 1 }
    if (path === '/users/roles') return { items: [{ name: 'Data Analyst', permissions: ['datasets:read', 'runs:execute'] }, { name: 'Auditor', permissions: ['audit:read'] }], total: 2 }
    return account
  })
})

describe('Local user administration', () => {
  it('creates a user using a selected role and an explicit local password', async () => {
    const user = userEvent.setup()
    renderApp(<UsersPanel/>, { permissions: ['users:read', 'users:write'] })
    await user.click(await screen.findByRole('button', { name: 'Nuevo usuario' }))
    const dialog = screen.getByRole('dialog')
    await user.type(within(dialog).getByLabelText('Nombre del usuario'), 'Nueva persona')
    await user.type(within(dialog).getByLabelText('Correo electrónico'), 'nueva@example.test')
    await user.selectOptions(within(dialog).getByLabelText('Rol del usuario'), 'Auditor')
    await user.type(within(dialog).getByLabelText('Contraseña inicial'), 'Local passphrase 2048!')
    await user.click(within(dialog).getByRole('button', { name: 'Crear usuario' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/users', expect.objectContaining({ method: 'POST', body: JSON.stringify({ name: 'Nueva persona', email: 'nueva@example.test', role: 'Auditor', active: true, password: 'Local passphrase 2048!' }) })))
    expect(await screen.findByText('El cambio del usuario quedó registrado.')).toBeInTheDocument()
  })

  it('edits activity with optimistic version and shows role permissions', async () => {
    const user = userEvent.setup()
    renderApp(<UsersPanel/>, { permissions: ['users:read', 'users:write'] })
    await user.click(await screen.findByRole('button', { name: 'Editar Ana' }))
    const dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByLabelText('Usuario activo'))
    await user.click(within(dialog).getByRole('button', { name: 'Guardar usuario' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/users/user-1', expect.objectContaining({ method: 'PATCH', body: expect.stringContaining('"active":false,"version":2') })))
  })

  it('requires matching reset passwords and never prepopulates the old credential', async () => {
    const user = userEvent.setup()
    renderApp(<UsersPanel/>, { permissions: ['users:read', 'users:write'] })
    await user.click(await screen.findByRole('button', { name: 'Restablecer contraseña de Ana' }))
    expect(screen.getByLabelText('Nueva contraseña')).toHaveValue('')
    await user.type(screen.getByLabelText('Nueva contraseña'), 'Changed passphrase 4096!')
    await user.type(screen.getByLabelText('Confirmar contraseña'), 'wrong')
    expect(screen.getByRole('button', { name: 'Restablecer contraseña' })).toBeDisabled()
    await user.clear(screen.getByLabelText('Confirmar contraseña'))
    await user.type(screen.getByLabelText('Confirmar contraseña'), 'Changed passphrase 4096!')
    await user.click(screen.getByRole('button', { name: 'Restablecer contraseña' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/users/user-1/reset-password', expect.objectContaining({ method: 'POST', body: JSON.stringify({ version: 2, password: 'Changed passphrase 4096!' }) })))
  })

  it('keeps auditor access read-only and renders errors without inventing accounts', async () => {
    renderApp(<UsersPanel/>, { permissions: ['users:read'] })
    expect(await screen.findByText('Ana')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Nuevo usuario' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Editar Ana' })).not.toBeInTheDocument()
  })

  it('shows a loading state followed by the server error', async () => {
    let rejectRequest: (reason: Error) => void = () => undefined
    vi.mocked(api).mockImplementation(() => new Promise((_resolve, reject) => { rejectRequest = reject }))
    renderApp(<UsersPanel/>, { permissions: ['users:read'] })
    expect(screen.getByText('Cargando información…')).toBeInTheDocument()
    rejectRequest(new Error('Servicio no disponible'))
    expect(await screen.findByText('Servicio no disponible')).toBeInTheDocument()
  })
})
