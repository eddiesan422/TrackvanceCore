import { act, screen, waitFor, within } from '@testing-library/react'
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
    if (path === '/exceptions/assignees') return { items: [{ id: 'stable-user-id', name: 'Equipo Trackvance', role: 'Administrator' }], total: 1 }
    if (path === '/exceptions') return { items: [detail], total: 1 }
    if (path === '/exceptions/case-1' && !options) return detail
    if (path === '/exceptions/case-1/validate') return detail
    if (path === '/exceptions/case-1' && options?.method === 'PATCH') return detail
    throw new Error(`Unexpected API request: ${path}`)
  })
}

describe('Exception technical resolution flow', () => {
  it('blocks next edits until the saved revision finishes refreshing', async () => {
    const user = userEvent.setup()
    const original = { ...baseException, state: 'OPEN' }
    const assigned = { ...original, state: 'ASSIGNED', assigned_user_id: 'stable-user-id', version: 3 }
    let releasePatch!: (value: typeof assigned) => void
    let releaseRefresh!: (value: typeof assigned) => void
    let detailReads = 0
    vi.mocked(api).mockImplementation(async (path, options) => {
      if (path === '/exceptions/assignees') return { items: [{ id: 'stable-user-id', name: 'Equipo Trackvance' }], total: 1 }
      if (path === '/exceptions') return { items: [original], total: 1 }
      if (path === '/exceptions/case-1' && !options) {
        detailReads += 1
        return detailReads === 1 ? original : new Promise<typeof assigned>(resolve => { releaseRefresh = resolve })
      }
      if (path === '/exceptions/case-1' && options?.method === 'PATCH') return new Promise<typeof assigned>(resolve => { releasePatch = resolve })
      throw new Error(`Unexpected request ${path}`)
    })
    renderApp(<ExceptionsPage/>, { path: '/exceptions?id=case-1', route: '/exceptions' })
    const state = await screen.findByLabelText('Estado de gestión')
    await user.selectOptions(screen.getByLabelText('Responsable'), 'stable-user-id')
    await user.selectOptions(state, 'ASSIGNED')
    await user.click(screen.getByRole('button', { name: 'Guardar gestión' }))
    expect(state).toBeDisabled()
    expect(screen.getByLabelText('Causa raíz')).toBeDisabled()
    expect(screen.getByLabelText('Archivo de evidencia')).toBeDisabled()
    await act(async () => releasePatch(assigned))
    await waitFor(() => expect(detailReads).toBe(2))
    expect(state).toBeDisabled()
    await user.selectOptions(state, 'INVESTIGATING')
    expect(state).toHaveValue('ASSIGNED')
    await act(async () => releaseRefresh(assigned))
    await waitFor(() => expect(screen.getByLabelText('Estado de gestión')).toBeEnabled())
    expect(screen.getByLabelText('Estado de gestión')).toHaveValue('ASSIGNED')
    expect(screen.getByText(/^Versión 3 ·/)).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Estado de gestión'), 'INVESTIGATING')
    await user.click(screen.getByRole('button', { name: 'Guardar gestión' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/exceptions/case-1', expect.objectContaining({
      method: 'PATCH', body: expect.stringContaining('"version":3,"state":"INVESTIGATING"'),
    })))
  })

  it('saves stable assignment, priority, SLA and the explicit automatic policy', async () => {
    const user = userEvent.setup()
    mockExceptionApi({ ...baseException, state: 'OPEN', priority: 'HIGH', auto_resolve_enabled: false })
    renderApp(<ExceptionsPage/>, { path: '/exceptions?id=case-1', route: '/exceptions' })
    const dialog = await screen.findByRole('dialog')
    await user.selectOptions(await within(dialog).findByLabelText('Responsable'), 'stable-user-id')
    await user.selectOptions(within(dialog).getByLabelText('Estado de gestión'), 'ASSIGNED')
    await user.selectOptions(within(dialog).getByLabelText('Prioridad del caso'), 'CRITICAL')
    await user.type(within(dialog).getByLabelText('SLA (horas)'), '24')
    await user.click(within(dialog).getByLabelText('Resolver automáticamente tras validación técnica'))
    await user.click(within(dialog).getByRole('button', { name: 'Guardar gestión' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/exceptions/case-1', expect.objectContaining({ method: 'PATCH', body: expect.stringContaining('"assigned_user_id":"stable-user-id","priority":"CRITICAL","sla_hours":24,"auto_resolve_enabled":true') })))
  })

  it('uploads bounded evidence as multipart with the current case version', async () => {
    const user = userEvent.setup()
    mockExceptionApi({ ...baseException, attachments: [] })
    vi.mocked(api).mockImplementationOnce(async () => ({ items: [], total: 0 }))
    renderApp(<ExceptionsPage/>, { path: '/exceptions?id=case-1', route: '/exceptions' })
    const upload = await screen.findByLabelText('Archivo de evidencia')
    await user.upload(upload, new File(['Comprobante'], 'evidencia.txt', { type: 'text/plain' }))
    await user.click(screen.getByRole('button', { name: 'Adjuntar evidencia' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/exceptions/case-1/attachments', expect.objectContaining({ method: 'POST', body: expect.any(FormData) })))
    const call = vi.mocked(api).mock.calls.find(([path]) => path.endsWith('/attachments'))!
    expect((call[1]?.body as FormData).get('version')).toBe('2')
  })

  it('disables management and reopening for read-only roles', async () => {
    mockExceptionApi({ ...baseException, state: 'RESOLVED' })
    renderApp(<ExceptionsPage/>, { path: '/exceptions?id=case-1', route: '/exceptions', permissions: ['exceptions:read'] })
    expect(await screen.findByRole('button', { name: 'Reabrir excepción' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Agregar comentario' })).toBeDisabled()
    expect(screen.queryByLabelText('Archivo de evidencia')).not.toBeInTheDocument()
  })

  it('blocks technical and administrative closure when the role lacks close permission', async () => {
    const user = userEvent.setup()
    mockExceptionApi({ ...baseException, root_cause: 'Fuente', resolution: 'Corregida',
      technical_validation: { status: 'VALIDATED', eligible: true, validated: true, can_resolve: true } })
    renderApp(<ExceptionsPage/>, { path: '/exceptions?id=case-1', route: '/exceptions', permissions: ['exceptions:write'] })
    expect(await screen.findByRole('button', { name: 'Resolver excepción' })).toBeDisabled()
    expect(screen.getByText('Tu rol no tiene permiso para cerrar excepciones.')).toBeInTheDocument()
    await user.click(screen.getByText('Cierre administrativo'))
    await user.selectOptions(screen.getByLabelText('Decisión administrativa'), 'ACCEPTED')
    await user.type(screen.getByLabelText('Motivo administrativo'), 'Aceptada por negocio')
    expect(screen.getByRole('button', { name: 'Registrar cierre administrativo' })).toBeDisabled()
  })

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
    expect(screen.getByRole('button', { name: 'Reabrir excepción' })).toBeDisabled()
    await userEvent.setup().type(screen.getByLabelText('Comentario para el historial'), 'Se detectó recurrencia')
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
