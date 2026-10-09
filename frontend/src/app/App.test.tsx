import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../api/client'
import type { Session } from '../api/client'
import App from './App'

vi.mock('../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../api/client')>(), api: vi.fn(), post: vi.fn(), setCsrfToken: vi.fn() }))

const session: Session = {
  user: { id: 'user-1', name: 'Equipo Trackvance', email: 'local@example.test', role: 'Administrator', permissions: ['runs:read', 'datasets:read', 'intake:read', 'recon:read', 'sentinel:read', 'delivery:read', 'catalog:read', 'reports:read'] },
  organization: { id: 'organization', name: 'Trackvance' },
  csrf_token: 'csrf-token',
}

function CurrentLocation() {
  return <output data-testid="current-location">{useLocation().pathname}</output>
}

describe('App session', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(api).mockImplementation(async path => {
      if (path === '/me') return session
      if (path === '/datasets') return { items: [], total: 0 }
      if (path.startsWith('/dashboard?')) return { stats: {}, variations: {}, attention: [], datasets_attention: [], recent_runs: [], filter_options: {}, health_history: [], module_status: [] }
      return { items: [], total: 0 }
    })
    vi.mocked(post).mockResolvedValue({ ok: true })
  })

  it('returns to the Trackvance start screen after logout', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
    const user = userEvent.setup()
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/datasets']}><App/><CurrentLocation/></MemoryRouter></QueryClientProvider>)

    const logout = await screen.findByLabelText('Cerrar sesión')
    const links = screen.getAllByRole('link')
    const sentinel = links.findIndex(link => link.textContent?.includes('Sentinel'))
    const delivery = links.findIndex(link => link.textContent?.includes('Data Delivery'))
    expect(delivery).toBe(sentinel + 1)
    expect(screen.getByText(/v0\.8\.5/)).toBeInTheDocument()
    await user.click(logout)

    await waitFor(() => expect(post).toHaveBeenCalledWith('/auth/logout'))
    expect(await screen.findByRole('button', { name: 'Entrar al entorno demo' })).toBeInTheDocument()
    expect(screen.getByTestId('current-location')).toHaveTextContent('/')
  })

  it.each([
    ['/datasets', 'Datasets'],
    ['/catalog', 'Catálogo'],
    ['/reports', 'Reportes'],
    ['/delivery', 'Data Delivery'],
    ['/intake', 'Data Intake'],
    ['/recon', 'ReconOps'],
    ['/sentinel', 'Sentinel'],
  ])('loads the lazy route %s from a direct link', async (path, heading) => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={[path]}><App/></MemoryRouter></QueryClientProvider>)
    expect(await screen.findByRole('heading', { name: heading })).toBeVisible()
    expect(screen.getByLabelText('Cerrar sesión')).toBeVisible()
    expect(screen.queryByText('No se pudo cargar esta sección')).not.toBeInTheDocument()
  })

  it('preserves permission checks on the lazy Delivery builder deep link', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/delivery/new']}><App/></MemoryRouter></QueryClientProvider>)
    expect(await screen.findByText('No tienes permiso para publicar configuraciones de entrega.')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Publicar configuración' })).not.toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('preserves destination permissions on a nested lazy deep link', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/delivery/destinations/hidden']}><App/></MemoryRouter></QueryClientProvider>)
    expect(await screen.findByText('No tienes permisos para consultar este destino.')).toBeVisible()
    expect(api).not.toHaveBeenCalledWith('/delivery/destinations/hidden')
  })

  it('keeps first-access sessions outside every feature route', async () => {
    const original = vi.mocked(api).getMockImplementation()!
    vi.mocked(api).mockImplementation(async (path, options) => path === '/me' ? { ...session, user: { ...session.user, must_change_password: true } } : original(path, options))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/datasets']}><App/></MemoryRouter></QueryClientProvider>)
    expect(await screen.findByRole('heading', { name: 'Cambia tu contraseña' })).toBeVisible()
    expect(api).not.toHaveBeenCalledWith('/datasets')
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('offers only configured SSO providers and accepts a username for local login', async () => {
    const original = vi.mocked(api).getMockImplementation()!
    vi.mocked(api).mockImplementation(async (path, options) => path === '/me' ? null : path === '/auth/providers' ? { items: [{ id: 'microsoft', name: 'Microsoft', start_url: '/api/v1/auth/sso/microsoft/start' }] } : original(path, options))
    vi.mocked(post).mockResolvedValue(session)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } }), user = userEvent.setup()
    render(<QueryClientProvider client={client}><MemoryRouter><App/></MemoryRouter></QueryClientProvider>)
    expect(await screen.findByRole('link', { name: 'Continuar con Microsoft' })).toHaveAttribute('href', '/api/v1/auth/sso/microsoft/start')
    expect(screen.queryByRole('link', { name: 'Continuar con Google' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Tengo una cuenta local' }))
    await user.type(screen.getByLabelText('Usuario o correo'), 'ana.ruiz')
    await user.type(screen.getByLabelText('Contraseña'), 'Local phrase 2048!')
    await user.click(screen.getByRole('button', { name: 'Iniciar sesión' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/auth/login', { username: 'ana.ruiz', password: 'Local phrase 2048!' }))
  })

  it('refreshes effective permissions in the current browser session', async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><MemoryRouter initialEntries={['/datasets']}><App/></MemoryRouter></QueryClientProvider>)
    expect(await screen.findByRole('heading', { name: 'Datasets' })).toBeVisible()
    vi.mocked(api).mockResolvedValue({ ...session, user: { ...session.user, role_version: 2, permissions: ['runs:read'] } })
    window.dispatchEvent(new Event('trackvance:session-refresh'))
    expect(await screen.findByText(/Tu rol no permite consultar esta sección/)).toBeVisible()
    expect(screen.queryByRole('link', { name: 'Datasets' })).not.toBeInTheDocument()
    expect(post).not.toHaveBeenCalledWith('/auth/logout')
  })
})
