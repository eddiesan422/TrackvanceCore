import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { SettingsPage } from '../../routes/Operations'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn() }))
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api).mockImplementation(async path => path === '/auth/providers' ? { items: [], local_enabled: true, statuses: { microsoft: 'DISABLED', google: 'DISABLED' } } : { items: [] })
})

it('keeps authentication status with providers disabled and removes notification settings', async () => {
  const user = userEvent.setup()
  renderApp(<SettingsPage/>, { permissions: ['system:read', 'users:read', 'roles:read', 'notifications:read'] })
  expect(screen.queryByRole('button', { name: 'Notificaciones' })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Autenticación' }))
  expect(within(await screen.findByRole('row', { name: /Login local/ })).getByText('Habilitado')).toBeVisible()
  for (const name of ['Microsoft', 'Google']) expect(within(screen.getByRole('row', { name: new RegExp(name) })).getByText('Deshabilitado')).toBeVisible()
  expect(vi.mocked(api).mock.calls.some(([path]) => path.startsWith('/notifications'))).toBe(false)
})

it('does not expose a legacy notification permission as an operational settings tab', () => {
  renderApp(<SettingsPage/>, { permissions: ['notifications:read'] })
  expect(screen.getByText('Tu rol no permite administrar este entorno.')).toBeVisible()
  expect(api).not.toHaveBeenCalled()
})
