import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { RolesPanel } from './RolesPanel'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn() }))
const permissions = ['roles:read', 'roles:manage', 'delivery:read', 'delivery:execute', 'delivery:overwrite']
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api).mockImplementation(async path => {
    if (path === '/roles/permissions') return { items: [
      { code: 'delivery:read', group: 'Data Delivery', label: 'Consultar', dependencies: [], delegable: true },
      { code: 'delivery:execute', group: 'Data Delivery', label: 'Ejecutar', dependencies: ['delivery:read'], delegable: true },
      { code: 'delivery:overwrite', group: 'Data Delivery', label: 'Reemplazar contenido', dependencies: ['delivery:execute'], delegable: true },
    ], total: 3 }
    return { items: [
      { id: 'admin', name: 'Administrator', active: true, protected: true, user_count: 1, version: 1, permissions },
      { id: 'regional', name: 'Regional', active: true, protected: false, user_count: 2, version: 1, permissions: ['delivery:read'] },
      { id: 'vacant', name: 'Vacante', active: true, protected: false, user_count: 0, version: 3, permissions: [] },
    ], total: 3 }
  })
})
describe('Dynamic role catalogue', () => {
  it('includes transitive dependencies and removes dependent permissions when their prerequisite is unchecked', async () => {
    const user = userEvent.setup()
    renderApp(<RolesPanel/>, { permissions })
    await user.click(await screen.findByRole('button', { name: 'Nuevo rol' }))
    await user.type(screen.getByLabelText('Nombre del rol'), 'Publicación')
    await user.click(await screen.findByRole('checkbox', { name: /Reemplazar contenido/ }))
    expect(screen.getByRole('checkbox', { name: /^Ejecutar/ })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: /^Consultar/ })).toBeChecked()
    await user.click(screen.getByRole('checkbox', { name: /^Consultar/ }))
    expect(screen.getByRole('checkbox', { name: /^Ejecutar/ })).not.toBeChecked()
    expect(screen.getByRole('checkbox', { name: /Reemplazar contenido/ })).not.toBeChecked()
    await user.click(screen.getByRole('checkbox', { name: /Reemplazar contenido/ }))
    await user.click(screen.getByRole('button', { name: 'Crear rol' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/roles', expect.objectContaining({ method: 'POST', body: expect.stringContaining('"permissions":["delivery:overwrite","delivery:execute","delivery:read"]') })))
  })
  it('protects Administrator and blocks deactivation/deletion of associated roles', async () => {
    const user = userEvent.setup()
    renderApp(<RolesPanel/>, { permissions })
    expect(await screen.findByRole('button', { name: 'Eliminar Regional' })).toBeDisabled()
    expect(screen.queryByRole('button', { name: 'Eliminar Administrator' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Ver permisos' }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByLabelText('Nombre del rol')).toBeDisabled()
    expect(within(dialog).getByLabelText('Rol activo')).toBeDisabled()
    expect(within(dialog).queryByRole('button', { name: 'Guardar rol' })).not.toBeInTheDocument()
  })
  it('uses a revision-checked logical deletion for an unassigned role', async () => {
    const user = userEvent.setup()
    renderApp(<RolesPanel/>, { permissions })
    await user.click(await screen.findByRole('button', { name: 'Eliminar Vacante' }))
    await user.click(screen.getByRole('button', { name: 'Confirmar baja del rol' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/roles/vacant', { method: 'DELETE', body: JSON.stringify({ version: 3 }) }))
  })
})
