import { fireEvent, screen, waitFor } from '@testing-library/react'
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

it('allows empty, partial and invalid timezone drafts without losing fields and saves the selected valid instant', async () => {
  vi.mocked(post).mockResolvedValue({ id: 'created', version: 1 })
  const user = userEvent.setup()
  renderApp(<DeliveryAutomationPage/>, { permissions: ['delivery:read', 'delivery:schedule'] })
  await user.type(await screen.findByLabelText('Nombre'), 'Horario seguro')
  fireEvent.change(screen.getByLabelText('Inicio en la zona seleccionada'), { target: { value: '2030-01-01T10:30' } })
  const zone = screen.getByLabelText('Zona horaria')
  await user.clear(zone)
  expect(await screen.findByRole('alert')).toHaveTextContent('zona horaria IANA válida')
  expect(screen.getByRole('button', { name: 'Crear automatización' })).toBeDisabled()
  await user.type(zone, 'America/Bogot')
  expect(zone).toHaveValue('America/Bogot')
  expect(screen.getByRole('button', { name: 'Crear automatización' })).toBeDisabled()
  fireEvent.change(zone, { target: { value: 'Invalid/Zone' } })
  expect(zone).toHaveAttribute('aria-invalid', 'true')
  expect(screen.getByLabelText('Nombre')).toHaveValue('Horario seguro')
  expect(screen.getByLabelText('Inicio en la zona seleccionada')).toHaveValue('2030-01-01T10:30')
  expect(screen.getByLabelText('Intervalo (minutos)')).toHaveValue(60)
  expect(post).not.toHaveBeenCalled()
  await user.clear(zone)
  await user.type(zone, 'America/Bogota')
  expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Crear automatización' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/delivery/automations', expect.objectContaining({ name: 'Horario seguro', settings: expect.objectContaining({ timezone: 'America/Bogota', starts_at: '2030-01-01T15:30:00.000Z' }) })))
})

it('reopens the persisted local start and preserves seconds and microseconds when editing other fields', async () => {
  const saved = { id: 'saved', name: 'Horario persistido', configuration_id: 'configuration', responsible_user_id: 'owner', responsible_name: 'Responsable', version: 3, enabled: true, next_run_at: '2030-01-01T15:30:17.123456Z', settings: { mode: 'INTERVAL', timezone: 'America/Bogota', starts_at: '2030-01-01T15:30:17.123456Z', interval_seconds: 7200, local_time: '09:00', weekdays: [0], source_policy: 'FIXED_VERSION', intake_configuration_id: null, allow_warnings: false, allow_empty: false, repeat_versions: false } }
  vi.mocked(api).mockImplementation(async path => path === '/delivery/automations/saved' ? saved : path === '/delivery/configurations' ? { items: [{ id: 'configuration', name: 'Publicar ventas', version: 1 }], total: 1 } : { items: [], total: 0 })
  vi.mocked(post).mockResolvedValue({ ...saved, version: 4 })
  const user = userEvent.setup()
  renderApp(<DeliveryAutomationPage/>, { permissions: ['delivery:read', 'delivery:schedule'], path: '/delivery/automation/saved', route: '/delivery/automation/:id' })
  expect(await screen.findByLabelText('Inicio en la zona seleccionada')).toHaveValue('2030-01-01T10:30')
  const zone = screen.getByLabelText('Zona horaria')
  await user.clear(zone)
  await user.type(zone, 'America/Bogota')
  await user.clear(screen.getByLabelText('Nombre'))
  await user.type(screen.getByLabelText('Nombre'), 'Nombre actualizado')
  await user.click(screen.getByRole('button', { name: 'Guardar nueva revisión' }))
  expect(post).toHaveBeenCalledWith('/delivery/automations/saved/versions', expect.objectContaining({ name: 'Nombre actualizado', expected_version: 3, settings: expect.objectContaining({ starts_at: saved.settings.starts_at, timezone: 'America/Bogota', interval_seconds: 7200 }) }))
})

it('rejects invalid calendar dates and offset strings without replacing an invalid zone', () => {
  expect(() => zonedStart('2030-02-31T10:30', 'America/Bogota')).toThrow(/fecha de inicio válida/)
  expect(() => zonedStart('2030-01-01T10:30', '')).toThrow(/zona horaria IANA válida/)
  expect(() => zonedStart('2030-01-01T10:30', '-05:00')).toThrow(/zona horaria IANA válida/)
})
