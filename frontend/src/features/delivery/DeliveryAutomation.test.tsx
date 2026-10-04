import { fireEvent, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { DeliveryAutomationPage, zonedStart } from './DeliveryAutomation'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api).mockImplementation(async path => path === '/delivery/configurations' ? { items: [{ id: 'configuration', name: 'Publicar ventas', version: 1 }], total: 1 } : path === '/intake/contracts' ? { items: [{ id: 'contract', name: 'Validar ventas', version: 1 }], total: 1 } : { items: [], total: 0 })
})

it('resolves IANA starts and chooses the first ambiguous instant while rejecting nonexistent hours', () => {
  expect(zonedStart('2026-10-03T09:30', 'America/Bogota')).toBe('2026-10-03T14:30:00.000Z')
  expect(zonedStart('2026-11-01T01:30', 'America/New_York')).toBe('2026-11-01T05:30:00.000Z')
  expect(() => zonedStart('2026-03-08T02:30', 'America/New_York')).toThrow(/no existe/)
})

it('requires an explicit chain and defaults to approved, nonempty, unrepeated output', async () => {
  vi.mocked(post).mockRejectedValue(new Error('Controlled fixture stop'))
  const user = userEvent.setup()
  renderApp(<DeliveryAutomationPage/>, { permissions: ['delivery:read', 'delivery:schedule', 'intake:read'] })
  await user.type(await screen.findByLabelText('Nombre'), 'Publicar aceptados')
  await user.selectOptions(screen.getByLabelText('Disparador'), 'CHAINED')
  await user.selectOptions(await screen.findByLabelText('Contrato Intake disparador'), 'contract')
  fireEvent.change(screen.getByLabelText('Inicio en la zona seleccionada'), { target: { value: '2030-01-01T10:30' } })
  await user.click(screen.getByRole('button', { name: /^Crear automatización$/ }))
  expect(post).toHaveBeenCalledWith('/delivery/automations', expect.objectContaining({ settings: expect.objectContaining({ mode: 'CHAINED', source_policy: 'INTAKE_OUTPUT', intake_configuration_id: 'contract', allow_warnings: false, allow_empty: false, repeat_versions: false, starts_at: '2030-01-01T15:30:00.000Z' }) }))
})

it('keeps the automation list readable without exposing write controls to a viewer', async () => {
  renderApp(<DeliveryAutomationPage/>, { permissions: ['delivery:read'] })
  expect(await screen.findByText('Sin automatizaciones')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Crear automatización' })).not.toBeInTheDocument()
  expect(screen.getByText(/Tu rol permite consultar/)).toBeInTheDocument()
})
