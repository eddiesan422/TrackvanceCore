import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { AutomaticMonitorPanel } from './AutomaticMonitorPanel'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))

const automatic = { id: 'automatic', version: 4, enabled: true, review_interval_seconds: 86400,
  responsible_user_id: 'owner', state: 'ACTIVE', next_review_at: '2026-10-15T10:00:00Z', reviews: [] }

describe('Automatic Sentinel metadata loading', () => {
  beforeEach(() => { vi.clearAllMocks(); vi.mocked(api).mockResolvedValue(automatic) })

  it('leaves 173 closed monitors idle and requests only the exact panel opened', async () => {
    const user = userEvent.setup()
    renderApp(<>{Array.from({ length: 173 }, (_, index) => <AutomaticMonitorPanel key={index} monitorId={`monitor-${index}`}/>)}</>)
    const panels = screen.getAllByText('Monitoreo automático por dataset')
    expect(panels).toHaveLength(173)
    expect(api).not.toHaveBeenCalled()
    expect(screen.queryByText('Cargando monitoreo automático…')).not.toBeInTheDocument()
    await user.click(panels[86])
    expect(await screen.findByLabelText('Revisión ligera (horas)')).toHaveValue(24)
    expect(api).toHaveBeenCalledTimes(1)
    expect(api).toHaveBeenCalledWith('/monitors/monitor-86/automatic')
    expect(post).not.toHaveBeenCalled()
  })

  it('keeps retrieval retry actionable and preserves viewers configuration guards', async () => {
    vi.mocked(api).mockRejectedValueOnce(new Error('Consulta temporalmente no disponible')).mockResolvedValueOnce(automatic)
    const user = userEvent.setup()
    renderApp(<AutomaticMonitorPanel monitorId="read-only-monitor"/>, { permissions: ['sentinel:read'] })
    await user.click(screen.getByText('Monitoreo automático por dataset'))
    expect(await screen.findByRole('alert')).toHaveTextContent('Consulta temporalmente no disponible')
    await user.click(screen.getByRole('button', { name: 'Volver a intentar' }))
    expect(await screen.findByText('Monitoreo automático activo')).toBeInTheDocument()
    expect(api).toHaveBeenCalledTimes(2)
    expect(vi.mocked(api).mock.calls.every(([path]) => path === '/monitors/read-only-monitor/automatic')).toBe(true)
    expect(screen.getByLabelText('Revisión ligera (horas)')).toBeDisabled()
    expect(screen.getByLabelText('Evaluar nuevas versiones y revisar frescura')).toBeDisabled()
    expect(screen.getByLabelText('Asignarme como responsable')).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Guardar monitoreo automático' })).toBeDisabled()
    expect(post).not.toHaveBeenCalled()
  })

  it('explains an absent automatic configuration only after opening its panel', async () => {
    vi.mocked(api).mockResolvedValue(null)
    const user = userEvent.setup()
    renderApp(<AutomaticMonitorPanel monitorId="manual-monitor"/>)
    expect(api).not.toHaveBeenCalled()
    await user.click(screen.getByText('Monitoreo automático por dataset'))
    expect(await screen.findByText('Este monitor no tiene monitoreo automático por dataset.')).toBeInTheDocument()
    expect(api).toHaveBeenCalledWith('/monitors/manual-monitor/automatic')
    expect(screen.queryByRole('button', { name: 'Guardar monitoreo automático' })).not.toBeInTheDocument()
  })
})
