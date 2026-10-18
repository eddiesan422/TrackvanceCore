import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { renderApp } from '../test/render'
import OperationTracking from './OperationTracking'

vi.mock('../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../api/client')>(), api: vi.fn() }))
beforeEach(() => vi.clearAllMocks())

it('keeps uncertain delivery on an intervention path without a replay action', async () => {
  vi.mocked(api).mockResolvedValue({ items: [{ id: 'run:uncertain', title: 'Entrega por confirmar', kind: 'run',
    state: 'UNKNOWN', tracking_id: 'acquisition:first', owner: 'Ana', created_at: '2026-10-15T12:00:00Z',
    diagnosis: 'No se recibió confirmación', impact: 'El destino podría haber aplicado la escritura.',
    next_action: 'Revisa la confirmación con el destino.', stages: [{ id: 'run:uncertain', title: 'Data Delivery', state: 'UNKNOWN', href: '/runs/uncertain' }],
    actions: [{ title: 'Revisar ejecución', href: '/runs/uncertain' }],
  }], has_more: false })
  const user = userEvent.setup()
  renderApp(<OperationTracking/>, { permissions: ['exceptions:read'], path: '/operations', route: '/operations' })
  expect(await screen.findByText('Entrega por confirmar')).toBeVisible()
  expect(screen.getByRole('link', { name: 'Revisar ejecución' })).toHaveAttribute('href', '/runs/uncertain')
  expect(screen.queryByRole('button', { name: /reenviar|repetir|reintentar/i })).not.toBeInTheDocument()
  await user.click(screen.getByRole('button', { name: 'Necesita mi intervención' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/operations?queue=intervention&search=&offset=0&limit=25&grouped=true'))
  expect(screen.getByRole('button', { name: 'Necesita mi intervención' })).toHaveAttribute('aria-pressed', 'true')
})

it('uses the server metadata cursor when authorized filtering returns an empty page', async () => {
  vi.mocked(api).mockResolvedValue({ items: [], has_more: true, next_offset: 1000 })
  const user = userEvent.setup()
  renderApp(<OperationTracking/>, { permissions: ['exceptions:read'], path: '/operations', route: '/operations' })
  expect(await screen.findByText('Sin operaciones en esta vista')).toBeVisible()
  await user.click(screen.getByRole('button', { name: 'Operaciones anteriores' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/operations?queue=all&search=&offset=1000&limit=25&grouped=true'))
  await user.click(screen.getByRole('button', { name: 'Operaciones más recientes' }))
  await waitFor(() => expect(api).toHaveBeenCalledWith('/operations?queue=all&search=&offset=0&limit=25&grouped=true'))
})

it('preserves personal updates without granting or querying exception operations', async () => {
  vi.mocked(api).mockResolvedValue({ groups: [], items: [], total: 0 })
  renderApp(<OperationTracking/>, { permissions: ['notifications:read', 'intake:read'], path: '/operations?view=news&read_state=UNREAD', route: '/operations' })
  expect(await screen.findByText('Sin notificaciones')).toBeVisible()
  expect(api).toHaveBeenCalledWith(expect.stringContaining('/notifications/inbox?'))
  expect(api).toHaveBeenCalledWith(expect.stringContaining('read_state=UNREAD'))
  expect(vi.mocked(api).mock.calls.some(([path]) => path.startsWith('/operations?'))).toBe(false)
  expect(screen.queryByRole('link', { name: 'Gestionar excepciones' })).not.toBeInTheDocument()
})

it('propagates the exact catalogue version scope for resource readers without exception management', async () => {
  vi.mocked(api).mockResolvedValue({ items: [], has_more: false })
  renderApp(<OperationTracking/>, { permissions: ['datasets:read'], path: '/operations?dataset_id=dataset&version_id=exact-version', route: '/operations' })
  expect(await screen.findByText('Sin operaciones en esta vista')).toBeVisible()
  expect(api).toHaveBeenCalledWith('/operations?queue=all&search=&offset=0&limit=25&grouped=true&dataset_id=dataset&version_id=exact-version')
  expect(screen.queryByRole('link', { name: 'Gestionar excepciones' })).not.toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Novedades personales' })).not.toBeInTheDocument()
})
