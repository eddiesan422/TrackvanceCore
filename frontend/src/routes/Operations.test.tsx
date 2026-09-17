import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import { label } from '../components/ui'
import { renderApp } from '../test/render'
import { ExceptionsPage, RulesPage } from './Operations'

vi.mock('../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../api/client')>(), api: vi.fn() }))

beforeEach(() => vi.clearAllMocks())

const baseException = {
  id: 'case-1', display_id: 'EXC-0001', finding_id: 'finding-1', run_id: 'run-origin', origin_run_id: 'run-origin',
  configuration_id: 'config-1', configuration_name: 'Contrato clientes', configuration_version: 3,
  title: 'customer_id obligatorio', module: 'intake', severity: 'HIGH', state: 'PENDING_VALIDATION',
  owner: 'Equipo Trackvance', root_cause: '', resolution: '', administrative_reason: '', version: 2,
  created_at: '2026-09-15T10:00:00Z', updated_at: '2026-09-15T11:00:00Z', events: [
    { timestamp: '2026-09-15T10:00:00Z', actor: 'Equipo Trackvance', to_state: 'OPEN', comment: 'Excepción creada' },
    { timestamp: '2026-09-15T11:00:00Z', actor: 'Equipo Trackvance', to_state: 'PENDING_VALIDATION', comment: 'Corrección aplicada' },
  ],
}

function mockExceptionApi(detail: Record<string, unknown>) {
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === '/exceptions') return { items: [detail], total: 1 }
    if (path === '/exceptions/case-1' && !options) return detail
    if (path === '/exceptions/case-1/validate') return detail
    if (path === '/exceptions/case-1' && options?.method === 'PATCH') return detail
    throw new Error(`Unexpected API request: ${path}`)
  })
}

describe('Exception technical resolution flow', () => {
  it('blocks resolution without later technical evidence and keeps administrative closure separate', async () => {
    const user = userEvent.setup()
    mockExceptionApi({ ...baseException, technical_validation: { status: 'NO_LATER_RUN', eligible: false, validated: false, can_resolve: false, reason: 'Aún no existe una ejecución posterior del mismo control.' } })
    renderApp(<ExceptionsPage/>, { path: '/exceptions?id=case-1', route: '/exceptions' })

    expect(await screen.findByText('Contrato clientes')).toBeInTheDocument()
    expect(screen.getByText('config-1')).toBeInTheDocument()
    expect(screen.getAllByRole('link', { name: /Ver ejecución de origen/ })[0]).toHaveAttribute('href', '/runs/run-origin')
    expect(screen.getByText('Resolución bloqueada')).toBeInTheDocument()
    expect(screen.getAllByText('Aún no existe una ejecución posterior del mismo control.').length).toBeGreaterThan(0)
    expect(screen.getByRole('button', { name: 'Resolver excepción' })).toBeDisabled()
    expect(screen.queryByRole('option', { name: 'Resuelta' })).not.toBeInTheDocument()

    await user.click(screen.getByText('Cierre administrativo'))
    expect(screen.getByText(/no equivale a una resolución verificada/i)).toBeInTheDocument()
    const close = screen.getByRole('button', { name: 'Registrar cierre administrativo' })
    expect(close).toBeDisabled()
    await user.selectOptions(screen.getByLabelText('Decisión administrativa'), 'DISCARDED')
    await user.type(screen.getByLabelText('Motivo administrativo'), 'Registro fuera del alcance acordado')
    expect(close).toBeEnabled()
    await user.click(close)
    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalledWith('/exceptions/case-1', expect.objectContaining({
      method: 'PATCH',
      body: expect.stringContaining('"state":"DISCARDED"'),
    })))
    expect(vi.mocked(api)).toHaveBeenCalledWith('/exceptions/case-1', expect.objectContaining({ body: expect.stringContaining('"administrative_reason":"Registro fuera del alcance acordado"') }))
  })

  it('asks the server to validate against the latest eligible run', async () => {
    const user = userEvent.setup()
    mockExceptionApi({ ...baseException, technical_validation: { status: 'NOT_REQUESTED', eligible: true, validated: false, can_resolve: false, reason: 'Hay una ejecución posterior disponible.', candidate_run_id: 'run-candidate' } })
    renderApp(<ExceptionsPage/>, { path: '/exceptions?id=case-1', route: '/exceptions' })

    const validate = await screen.findByRole('button', { name: 'Validar corrección' })
    expect(validate).toBeEnabled()
    await user.click(validate)
    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalledWith('/exceptions/case-1/validate', {
      method: 'POST', body: JSON.stringify({ version: 2 }),
    }))
  })

  it('enables resolution only after positive technical validation and links both runs', async () => {
    const user = userEvent.setup()
    mockExceptionApi({
      ...baseException, root_cause: 'Contrato de origen incompleto', resolution: 'Se corrigió la fuente',
      validation_run_id: 'run-validation', validated_at: '2026-09-16T08:00:00Z',
      technical_validation: { status: 'VALIDATED', eligible: true, validated: true, can_resolve: true, reason: 'La regla REQUIRED pasó.', validation_run_id: 'run-validation' },
    })
    renderApp(<ExceptionsPage/>, { path: '/exceptions?id=case-1', route: '/exceptions' })

    expect(await screen.findByText('Validada técnicamente')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Ver ejecución de validación/ })).toHaveAttribute('href', '/runs/run-validation')
    const resolve = screen.getByRole('button', { name: 'Resolver excepción' })
    expect(resolve).toBeEnabled()
    await user.click(resolve)
    await waitFor(() => expect(vi.mocked(api)).toHaveBeenCalledWith('/exceptions/case-1', expect.objectContaining({
      method: 'PATCH', body: expect.stringContaining('"state":"RESOLVED"'),
    })))
  })

  it('identifies legacy resolved cases without claiming structured validation', async () => {
    mockExceptionApi({ ...baseException, state: 'RESOLVED', technical_validation: { status: 'NOT_REQUESTED', eligible: false, validated: false, can_resolve: false } })
    renderApp(<ExceptionsPage/>, { path: '/exceptions?id=case-1', route: '/exceptions' })

    expect(await screen.findByText('Cierre histórico sin evidencia técnica estructurada')).toBeInTheDocument()
    expect(screen.getByText(/no se afirma que Trackvance haya verificado la corrección/i)).toBeInTheDocument()
    expect(screen.queryByText('Validada técnicamente')).not.toBeInTheDocument()
  })

  it('keeps validated evidence visible after resolution even when it can no longer resolve', async () => {
    mockExceptionApi({
      ...baseException, state: 'RESOLVED', validation_run_id: 'run-validation',
      technical_validation: { status: 'VALIDATED', eligible: true, validated: true, can_resolve: false, reason: 'La corrección fue confirmada.', validation_run_id: 'run-validation' },
    })
    renderApp(<ExceptionsPage/>, { path: '/exceptions?id=case-1', route: '/exceptions' })

    expect(await screen.findByText('Validada técnicamente')).toBeInTheDocument()
    expect(screen.getByText('Esta excepción fue resuelta después de una validación técnica.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /Ver ejecución de validación/ })).toHaveAttribute('href', '/runs/run-validation')
    expect(screen.queryByRole('button', { name: 'Resolver excepción' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Reabrir excepción' })).toBeEnabled()
  })
})

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
