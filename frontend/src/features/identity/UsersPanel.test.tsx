import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { UsersPanel } from './UsersPanel'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn() }))
const account = { id: 'user-1', first_name: 'Ana', last_name: 'Ruiz', name: 'Ana Ruiz', username: 'ana.ruiz', email: 'ana@example.test', role: 'Analista regional', role_id: 'regional', active: true, deleted: false, version: 2, permissions: ['datasets:read'], must_change_password: true, credential_delivery: { status: 'FAILED', error_code: 'NO_PROVIDER' }, external_identities: [{ id: 'external-1', provider: 'MICROSOFT_ENTRA', last_login_at: '2026-09-26T12:00:00Z' }] }
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === '/users' && !options) return { items: [account], total: 1 }
    if (path === '/users/roles') return { items: [{ id: 'regional', name: 'Analista regional', active: true, permissions: ['datasets:read'] }, { id: 'audit', name: 'Auditor', active: true, permissions: ['audit:read'] }], total: 2 }
    return account
  })
})
describe('User administration', () => {
  it('creates with a backend role identity and never requests a password from the administrator', async () => {
    const user = userEvent.setup()
    renderApp(<UsersPanel/>, { permissions: ['users:read', 'users:manage'] })
    await user.click(await screen.findByRole('button', { name: 'Nuevo usuario' }))
    const dialog = screen.getByRole('dialog')
    await user.type(within(dialog).getByLabelText('Nombres'), 'Nueva')
    await user.type(within(dialog).getByLabelText('Apellidos'), 'Persona')
    await user.type(within(dialog).getByLabelText('Username'), 'nueva.persona')
    await user.type(within(dialog).getByLabelText('Correo electrónico'), 'nueva@example.test')
    await user.selectOptions(within(dialog).getByLabelText('Rol del usuario'), 'regional')
    expect(dialog.querySelector('input[type=password]')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Crear usuario' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/users', { method: 'POST', body: JSON.stringify({ first_name: 'Nueva', last_name: 'Persona', username: 'nueva.persona', email: 'nueva@example.test', role_id: 'regional', active: true }) }))
  })
  it('sends the current revision when disabling a user', async () => {
    const user = userEvent.setup()
    renderApp(<UsersPanel/>, { permissions: ['users:read', 'users:manage'] })
    await user.click(await screen.findByRole('button', { name: 'Editar Ana Ruiz' }))
    const dialog = screen.getByRole('dialog')
    await user.click(within(dialog).getByLabelText('Usuario activo'))
    await user.click(within(dialog).getByRole('button', { name: 'Guardar usuario' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/users/user-1', expect.objectContaining({ method: 'PATCH', body: expect.stringContaining('"active":false,"version":2') })))
  })
  it('reports missing SMTP and requests regenerated credentials without a password payload', async () => {
    const user = userEvent.setup()
    renderApp(<UsersPanel/>, { permissions: ['users:read', 'users:manage'] })
    expect(await screen.findByText('SMTP no configurado')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Regenerar credenciales de Ana Ruiz' }))
    const dialog = screen.getByRole('dialog')
    expect(dialog.querySelector('input[type=password]')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Regenerar y reenviar' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/users/user-1/resend-credentials', { method: 'POST', body: JSON.stringify({ version: 2 }) }))
  })
  it('waits for the saved revision before allowing credential regeneration', async () => {
    const original = vi.mocked(api).getMockImplementation()!
    let reads = 0, finishRefresh: () => void = () => undefined
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path === '/users' && !options && ++reads > 1) return new Promise(resolve => { finishRefresh = () => resolve({ items: [{ ...account, version: 3 }], total: 1 }) })
      return original(path, options)
    })
    const user = userEvent.setup()
    renderApp(<UsersPanel/>, { permissions: ['users:read', 'users:manage'] })
    await user.click(await screen.findByRole('button', { name: 'Editar Ana Ruiz' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Guardar usuario' }))
    await waitFor(() => expect(reads).toBe(2))
    expect(screen.getByRole('dialog', { name: 'Editar usuario' })).toBeVisible()
    expect(screen.getByRole('button', { name: 'Guardando…' })).toBeDisabled()
    finishRefresh()
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: 'Regenerar credenciales de Ana Ruiz' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Regenerar y reenviar' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/users/user-1/resend-credentials', { method: 'POST', body: JSON.stringify({ version: 3 }) }))
    finishRefresh()
  })
  it('shows linked methods and unlinks only the external identity', async () => {
    const user = userEvent.setup()
    renderApp(<UsersPanel/>, { permissions: ['users:read', 'users:manage'] })
    await user.click(await screen.findByRole('button', { name: 'Métodos de acceso de Ana Ruiz' }))
    expect(screen.getByText('No vinculado')).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Desvincular Microsoft' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/users/user-1/external-identities/external-1', { method: 'DELETE' }))
  })
  it('keeps read-only access without mutation controls', async () => {
    renderApp(<UsersPanel/>, { permissions: ['users:read'] })
    expect(await screen.findByText('Ana Ruiz')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Nuevo usuario' })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Editar Ana Ruiz' })).not.toBeInTheDocument()
  })
  it('shows loading followed by the server error', async () => {
    let rejectRequest: (reason: Error) => void = () => undefined
    vi.mocked(api).mockImplementation(() => new Promise((_resolve, reject) => { rejectRequest = reject }))
    renderApp(<UsersPanel/>, { permissions: ['users:read'] })
    expect(screen.getByText('Cargando información…')).toBeVisible()
    rejectRequest(new Error('Servicio no disponible'))
    expect(await screen.findByText('Servicio no disponible')).toBeVisible()
  })
})
