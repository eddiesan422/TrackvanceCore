import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { IntakeSchedulePanel } from './IntakeSchedulePanel'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))
const contract = { id: 'contract-v2', name: 'Ventas', version: 2, deleted: false }
const schedule = { id: 'schedule', version: 3, configuration_id: 'contract-v1', configuration_version: 1,
  responsible_user_id: 'stable-user-id', responsible_name: 'Equipo Trackvance', enabled: true,
  settings: { mode: 'EVENT', timezone: 'America/Bogota', starts_at: '2030-01-01T15:30:17.123456Z',
    interval_seconds: 3600, local_time: '09:00', weekdays: [0], requested_engine: 'AUTO' } }

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(api).mockImplementation(async path => path.endsWith('/schedule') ? schedule : { items: [], total: 0 })
  vi.mocked(post).mockResolvedValue({ ...schedule, version: 4 })
})

async function expand() {
  const user = userEvent.setup()
  renderApp(<IntakeSchedulePanel contract={contract}/>, { permissions: ['intake:read', 'intake:schedule'] })
  await user.click(screen.getByText('Programación, histórico y baja lógica'))
  await screen.findByLabelText('Disparo Intake')
  return user
}

it('preserves the pinned revision and exact persisted start until the user chooses the new revision', async () => {
  const user = await expand()
  expect(screen.getByLabelText('Desde (hora en la zona seleccionada)')).toHaveValue('2030-01-01T10:30')
  await user.click(screen.getByRole('button', { name: 'Guardar programación' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/intake/contracts/contract-v1/schedule', expect.objectContaining({
    expected_version: 3, responsible_user_id: 'stable-user-id', settings: expect.objectContaining({ starts_at: schedule.settings.starts_at, mode: 'EVENT' }),
  })))
  await user.click(screen.getByLabelText(/Actualizar la programación a esta revisión/))
  await user.click(screen.getByRole('button', { name: 'Guardar programación' }))
  await waitFor(() => expect(post).toHaveBeenLastCalledWith('/intake/contracts/contract-v2/schedule', expect.anything()))
})

it('saves calendar starts in the chosen IANA zone and rejects an invalid zone without losing the draft', async () => {
  const user = await expand()
  await user.selectOptions(screen.getByLabelText('Disparo Intake'), 'DAILY')
  fireEvent.change(screen.getByLabelText('Desde (hora en la zona seleccionada)'), { target: { value: '2030-02-01T08:15' } })
  fireEvent.change(screen.getByLabelText('Hora diaria'), { target: { value: '08:15' } })
  fireEvent.change(screen.getByLabelText('Zona horaria'), { target: { value: 'Invalid/Zone' } })
  expect(screen.getByRole('button', { name: 'Guardar programación' })).toBeDisabled()
  expect(screen.getByLabelText('Desde (hora en la zona seleccionada)')).toHaveValue('2030-02-01T08:15')
  fireEvent.change(screen.getByLabelText('Zona horaria'), { target: { value: 'America/Bogota' } })
  await user.click(screen.getByRole('button', { name: 'Guardar programación' }))
  await waitFor(() => expect(post).toHaveBeenCalledWith('/intake/contracts/contract-v1/schedule', expect.objectContaining({
    settings: expect.objectContaining({ mode: 'DAILY', starts_at: '2030-02-01T13:15:00.000Z', local_time: '08:15' }),
  })))
})

it('blocks deletion from the server impact and keeps viewer controls disabled', async () => {
  vi.mocked(api).mockImplementation(async path => path.endsWith('/schedule') ? schedule : path.endsWith('/deletion-impact') ? {
    revision_count: 2, intake_schedule_enabled: true, delivery_automation_ids: ['delivery'], pending_run_ids: ['active'], open_exception_ids: [], can_delete: false,
  } : { items: [], total: 0 })
  const user = userEvent.setup()
  renderApp(<IntakeSchedulePanel contract={contract}/>, { permissions: ['intake:read', 'intake:delete'] })
  await user.click(screen.getByText('Programación, histórico y baja lógica'))
  expect(await screen.findByRole('button', { name: 'Guardar programación' })).toBeDisabled()
  await user.click(screen.getByRole('button', { name: 'Revisar impacto y eliminar contrato' }))
  expect(await screen.findByText(/Bloqueado por 1 ejecuciones/)).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Confirmar baja lógica' })).toBeDisabled()
  expect(screen.getByText(/entregas ya registradas y los monitores Sentinel se conservan/)).toBeInTheDocument()
})
