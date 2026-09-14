import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { label } from '../components/ui'
import { renderApp } from '../test/render'
import { RulesPage } from './Operations'

vi.mock('../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../api/client')>(), api: vi.fn() }))

describe('Rule library taxonomy', () => {
  it('keeps EXACT_COMPARE separate and finds numeric tolerance by its historical alias', async () => {
    vi.mocked(api).mockResolvedValue({ items: [
      { id: 'exact', module: 'recon', code: 'EXACT_COMPARE', name: 'Igualdad exacta', description: 'Igualdad después de la normalización declarada.', legacy_aliases: [] },
      { id: 'numeric', module: 'recon', code: 'NUMERIC_TOLERANCE', name: 'Tolerancia numérica', description: 'Tolerancia absoluta y porcentual.', legacy_aliases: ['EXACT_MATCH'] },
    ], total: 2 })
    const user = userEvent.setup()
    renderApp(<RulesPage/>)
    expect(await screen.findByText('EXACT_COMPARE')).toBeInTheDocument()
    await user.type(screen.getByRole('textbox', { name: 'Buscar una regla…' }), 'EXACT_MATCH')
    const row = screen.getByRole('row', { name: /Tolerancia numérica/ })
    expect(within(row).getByText('NUMERIC_TOLERANCE')).toBeInTheDocument()
    expect(within(row).getByText('EXACT_MATCH')).toBeInTheDocument()
    expect(within(row).getByRole('link', { name: /Configurar/ })).toHaveAttribute('href', '/recon')
    expect(screen.queryByText('Igualdad exacta')).not.toBeInTheDocument()
  })

  it('presents historical EXACT_MATCH evidence as numeric tolerance without renaming the stored code', async () => {
    vi.mocked(api).mockResolvedValue({ items: [{ id: 'legacy', module: 'recon', code: 'EXACT_MATCH', name: 'Conciliación por clave', description: 'Compara importes.' }], total: 1 })
    renderApp(<RulesPage/>)
    expect(await screen.findByText('Tolerancia numérica')).toBeInTheDocument()
    expect(screen.getByText('NUMERIC_TOLERANCE')).toBeInTheDocument()
    expect(screen.getByText('EXACT_MATCH')).toBeInTheDocument()
    expect(label('EXACT_MATCH')).toBe('Tolerancia numérica (histórica)')
  })
})
