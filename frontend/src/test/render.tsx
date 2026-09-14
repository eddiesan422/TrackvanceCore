import type { ReactElement } from 'react'
import { render } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { AuthContext } from '../app/session'
import type { Session } from '../api/client'

export function renderApp(ui: ReactElement, options: { permissions?: string[]; path?: string; route?: string } = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  const session: Session = {
    user: { id: 'stable-user-id', name: 'Equipo Trackvance', email: 'local@example.test', role: 'ADMIN', permissions: options.permissions ?? ['exports:download', 'artifacts:download', 'runs:execute', 'configurations:write', 'datasets:write'] },
    organization: { id: 'organization', name: 'Trackvance' }, csrf_token: 'test-csrf',
  }
  return render(<QueryClientProvider client={client}><AuthContext.Provider value={session}><MemoryRouter initialEntries={[options.path || '/']}><Routes><Route path={options.route || '*'} element={ui}/></Routes></MemoryRouter></AuthContext.Provider></QueryClientProvider>)
}
