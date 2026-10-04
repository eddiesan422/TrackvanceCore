import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import { NotificationIndicator, NotificationsPage } from './Notifications'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))
beforeEach(() => vi.resetAllMocks())

it('does not fetch or expose the inbox without permission', () => {
  renderApp(<><NotificationIndicator/><NotificationsPage/></>, { permissions: [] })
  expect(api).not.toHaveBeenCalled()
  expect(screen.queryByRole('link', { name: /Ver notificaciones/ })).not.toBeInTheDocument()
})

it('separates technical SUCCESS from a rejected business result and marks only its own notification', async () => {
  vi.mocked(api).mockImplementation(async path => path.includes('unread-count') ? { unread_count: 1 } : { items: [{ id: 'notification', module: 'intake', origin: 'MANUAL', status: 'SUCCESS', decision: 'REJECTED', description: 'La validación terminó y el dataset fue rechazado.', created_at: '2026-10-03T10:00:00Z', read_at: null, detail_url: '/runs/validated' }], total: 1 })
  vi.mocked(post).mockResolvedValue({})
  const user = userEvent.setup()
  renderApp(<><NotificationIndicator/><NotificationsPage/></>, { permissions: ['notifications:read'] })
  expect(await screen.findByRole('link', { name: 'Ver notificaciones, 1 sin leer' })).toHaveAttribute('href', '/notifications')
  expect(await screen.findByText('Completada', { selector: 'span' })).toBeInTheDocument()
  expect(screen.getByText('Rechazado')).toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Ver detalle' })).toHaveAttribute('href', '/runs/validated')
  await user.click(screen.getByRole('button', { name: /^Marcar leída$/ }))
  expect(post).toHaveBeenCalledWith('/notifications/inbox/notification/read')
})

it('filters unread and origin through the scoped API', async () => {
  vi.mocked(api).mockResolvedValue({ items: [], total: 0 })
  const user = userEvent.setup()
  renderApp(<NotificationsPage/>, { permissions: ['notifications:read'] })
  await screen.findByText('Sin notificaciones')
  await user.selectOptions(screen.getByLabelText('Lectura'), 'UNREAD')
  await user.selectOptions(screen.getByLabelText('Origen'), 'CHAINED')
  expect(api).toHaveBeenLastCalledWith(expect.stringContaining('read_state=UNREAD&module=&origin=CHAINED'))
  await user.selectOptions(screen.getByLabelText('Lectura'), 'READ')
  expect(api).toHaveBeenLastCalledWith(expect.stringContaining('read_state=READ&module=&origin=CHAINED'))
  await user.selectOptions(screen.getByLabelText('Lectura'), 'ALL')
  expect(api).toHaveBeenLastCalledWith(expect.stringContaining('read_state=ALL&module=&origin=CHAINED'))
  await user.selectOptions(screen.getByLabelText('Estado técnico'), 'UNKNOWN')
  expect(api).toHaveBeenLastCalledWith(expect.stringContaining('&status=UNKNOWN'))
})

it('preserves distinct detail destinations for omitted and committed chained notifications', async () => {
  vi.mocked(api).mockResolvedValue({ items: [
    { id: 'blocked', module: 'DELIVERY', origin: 'CHAINED', status: 'SKIPPED', decision: null, description: 'La entrega automática se bloqueó porque la entrada está vacía.', created_at: '2026-10-04T05:30:00Z', read_at: null, detail_url: '/delivery/automation/empty-output' },
    { id: 'committed', module: 'DELIVERY', origin: 'CHAINED', status: 'SUCCESS', decision: 'COMMITTED', description: 'Entrega confirmada en el destino.', created_at: '2026-10-04T05:29:00Z', read_at: null, detail_url: '/runs/exact-accepted' },
  ], total: 2 })
  renderApp(<NotificationsPage/>, { permissions: ['notifications:read'] })
  const blocked = await screen.findByRole('row', { name: /La entrega automática se bloqueó porque la entrada está vacía\./ })
  expect(within(blocked).getByRole('link', { name: 'Ver detalle' })).toHaveAttribute('href', '/delivery/automation/empty-output')
  const committed = screen.getByRole('row', { name: /Entrega confirmada en el destino\./ })
  expect(within(committed).getByText('Confirmado', { exact: true })).toBeInTheDocument()
  expect(within(committed).getByText('DELIVERY · Encadenado', { exact: true })).toBeInTheDocument()
  expect(within(committed).getByRole('link', { name: 'Ver detalle' })).toHaveAttribute('href', '/runs/exact-accepted')
})

it('confirms read/unread changes in filtered lists and the counter without waiting for polling, and survives reload', async () => {
  let readAt: string | null = '2030-01-01T10:00:00Z'
  const item = { id: 'personal', module: 'DELIVERY', origin: 'CHAINED', status: 'SUCCESS', decision: 'COMMITTED', description: 'Entrega confirmada.', created_at: '2030-01-01T09:00:00Z', detail_url: '/runs/delivered' }
  vi.mocked(api).mockImplementation(async path => {
    if (path.includes('unread-count')) return { unread_count: readAt ? 0 : 1 }
    const filter = new URLSearchParams(path.split('?')[1]).get('read_state')
    const included = filter === 'ALL' || (filter === 'READ' ? !!readAt : !readAt)
    return { items: included ? [{ ...item, read_at: readAt }] : [], total: included ? 1 : 0 }
  })
  vi.mocked(post).mockImplementation(async path => { readAt = path.endsWith('/unread') ? null : '2030-01-01T11:00:00Z'; return { ...item, read_at: readAt } })
  const user = userEvent.setup()
  const view = renderApp(<><NotificationIndicator/><NotificationsPage/></>, { permissions: ['notifications:read'] })
  await screen.findByRole('button', { name: 'Marcar como no leída' })
  await user.selectOptions(screen.getByLabelText('Lectura'), 'READ')
  await user.click(await screen.findByRole('button', { name: 'Marcar como no leída' }))
  expect(await screen.findByText('Sin notificaciones')).toBeInTheDocument()
  expect(await screen.findByRole('link', { name: 'Ver notificaciones, 1 sin leer' })).toBeInTheDocument()
  expect(post).toHaveBeenLastCalledWith('/notifications/inbox/personal/unread')
  await user.selectOptions(screen.getByLabelText('Lectura'), 'UNREAD')
  await user.click(await screen.findByRole('button', { name: 'Marcar leída' }))
  expect(await screen.findByText('Sin notificaciones')).toBeInTheDocument()
  await waitFor(() => expect(screen.getByRole('link', { name: 'Ver notificaciones' })).toBeInTheDocument())
  await user.selectOptions(screen.getByLabelText('Lectura'), 'ALL')
  await user.click(await screen.findByRole('button', { name: 'Marcar como no leída' }))
  expect(await screen.findByRole('button', { name: 'Marcar leída' })).toBeInTheDocument()
  view.unmount()
  renderApp(<><NotificationIndicator/><NotificationsPage/></>, { permissions: ['notifications:read'] })
  expect(await screen.findByText('Sin leer ·')).toBeInTheDocument()
  expect(await screen.findByRole('link', { name: 'Ver notificaciones, 1 sin leer' })).toBeInTheDocument()
})

it('keeps confirmed read state and counter unchanged when marking unread fails', async () => {
  vi.mocked(api).mockImplementation(async path => path.includes('unread-count') ? { unread_count: 0 } : { items: [{ id: 'personal', module: 'intake', origin: 'MANUAL', status: 'SUCCESS', decision: 'APPROVED', description: 'Validación aprobada.', created_at: '2030-01-01T09:00:00Z', read_at: '2030-01-01T10:00:00Z', detail_url: '/runs/approved' }], total: 1 })
  vi.mocked(post).mockRejectedValue(new Error('No tienes permiso para modificar esta notificación.'))
  const user = userEvent.setup()
  renderApp(<><NotificationIndicator/><NotificationsPage/></>, { permissions: ['notifications:read'] })
  await user.click(await screen.findByRole('button', { name: 'Marcar como no leída' }))
  expect(await screen.findByText('No tienes permiso para modificar esta notificación.')).toBeInTheDocument()
  expect(screen.queryByText('Sin leer ·')).not.toBeInTheDocument()
  expect(screen.getByRole('link', { name: 'Ver notificaciones' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Marcar como no leída' })).toBeEnabled()
})

it('shows the exact public acquisition diagnostic and leaves historical notifications uninterpreted', async () => {
  const message = 'La fuente supera el límite efectivo de 100,000 registros de datos.'
  vi.mocked(api).mockResolvedValue({ items: [
    { id: 'new', module: 'acquisition', origin: 'MANUAL', status: 'FAILED', decision: null, description: message, created_at: '2030-01-01T09:00:00Z', read_at: null, detail_url: '/datasets/source', error: { code: 'ACQUISITION_ROW_LIMIT', message, details: { limit: 'data_rows', maximum: 100000 }, reference: 'safe-reference' } },
    { id: 'old', module: 'acquisition', origin: 'MANUAL', status: 'FAILED', decision: null, description: 'La adquisición histórica falló.', created_at: '2029-01-01T09:00:00Z', read_at: null, detail_url: '/datasets/source', error: null },
  ], total: 2 })
  renderApp(<NotificationsPage/>, { permissions: ['notifications:read'] })
  const item = await screen.findByRole('row', { name: new RegExp(message.replaceAll('.', '\\.')) })
  expect(within(item).getByText(/ACQUISITION_ROW_LIMIT · Referencia: safe-reference/)).toBeInTheDocument()
  expect(within(item).getAllByText(message, { exact: false })).toHaveLength(1)
  expect(screen.getByRole('row', { name: /La adquisición histórica falló/ })).not.toHaveTextContent('ACQUISITION_ROW_LIMIT')
})
