import { act, render, screen } from '@testing-library/react'
import { lazy } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { RouteContent } from './RouteContent'

describe('Lazy route content', () => {
  it('uses the shared accessible loader without hiding the surrounding shell', async () => {
    let finish: ((module: { default: () => React.ReactNode }) => void) | undefined
    const Feature = lazy(() => new Promise<{ default: () => React.ReactNode }>(resolve => { finish = resolve }))
    render(<><nav>Workspace</nav><RouteContent><Feature/></RouteContent></>)
    expect(screen.getByText('Workspace')).toBeVisible()
    expect(screen.getByRole('status')).toHaveTextContent('Cargando sección…')
    await act(async () => finish?.({ default: () => <h1>Feature cargada</h1> }))
    expect(await screen.findByRole('heading', { name: 'Feature cargada' })).toBeVisible()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('contains chunk-loading errors and recovers when navigating to a different route', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined)
    const Feature = lazy(() => Promise.reject(new Error('Simulated missing feature chunk')))
    const rendered = render(<><nav>Workspace</nav><RouteContent key="failed"><Feature/></RouteContent></>)
    expect(await screen.findByRole('alert')).toHaveTextContent('No se pudo cargar esta sección')
    expect(screen.getByRole('button', { name: 'Recargar página' })).toBeVisible()
    expect(screen.getByText('Workspace')).toBeVisible()
    rendered.rerender(<><nav>Workspace</nav><RouteContent key="next"><h1>Otra sección</h1></RouteContent></>)
    expect(screen.getByRole('heading', { name: 'Otra sección' })).toBeVisible()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    consoleError.mockRestore()
  })
})
