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
  user: { id: 'user-1', name: 'Equipo Trackvance', email: 'local@example.test', role: 'Administrator', permissions: [] },
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
    expect(screen.getByText(/v0\.5\.0/)).toBeInTheDocument()
    await user.click(logout)

    await waitFor(() => expect(post).toHaveBeenCalledWith('/auth/logout'))
    expect(await screen.findByRole('button', { name: 'Entrar al entorno demo' })).toBeInTheDocument()
    expect(screen.getByTestId('current-location')).toHaveTextContent('/')
  })
})
