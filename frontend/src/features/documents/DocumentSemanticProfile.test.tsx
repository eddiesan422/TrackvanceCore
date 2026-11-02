import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, post } from '../../api/client'
import { renderApp } from '../../test/render'
import DocumentSemanticProfile from './DocumentSemanticProfile'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))
const invoice = { id: 'invoice-v1', name: 'Factura general', revision: 1, sha256: 'frozen-profile-sha', document_type: 'INVOICE', description: 'Datos impresos de la factura.',
  fields: [{ name: 'invoice_number', label: 'Número de factura', logical_type: 'STRING', required: true }, { name: 'reference', label: 'Referencia', logical_type: 'STRING', required: false }],
  tables: [{ name: 'lines', description: 'Una fila por concepto impreso.', kind: 'TABLE', required: true, fields: [{ name: 'quantity', logical_type: 'DECIMAL', label: 'Cantidad' }, { name: 'concept', logical_type: 'STRING', label: 'Concepto' }] }],
}
const available = { enabled: true, mode: 'SEMANTIC_GENERAL_V1', default_enabled: false, profiles: [invoice] }
beforeEach(() => { vi.clearAllMocks(); vi.mocked(api).mockResolvedValue(available); vi.mocked(post).mockResolvedValue({ profile: { id: invoice.id, revision: 1 } }) })

describe('General semantic profile configuration', () => {
  it('keeps the disabled experimental mode unavailable and does not infer activation from existing profiles', async () => {
    vi.mocked(api).mockResolvedValue({ ...available, enabled: false })
    renderApp(<DocumentSemanticProfile familyId="family" canConfigure refresh={vi.fn()}/> )
    expect(await screen.findByLabelText('Perfil semántico')).toBeDisabled()
    expect(screen.getByText(/está deshabilitada en este entorno/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' })).toBeDisabled()
    expect(post).not.toHaveBeenCalled()
  })

  it('shows the requested fields and explicit independent route before an audited profile selection', async () => {
    const user = userEvent.setup(), refresh = vi.fn()
    renderApp(<DocumentSemanticProfile familyId="family" canConfigure refresh={refresh}/> )
    expect(await screen.findByLabelText('Perfil semántico')).toHaveValue('')
    expect(screen.getByLabelText('Lectura del documento')).toHaveValue('QWEN_VISUAL')
    await user.selectOptions(screen.getByLabelText('Perfil semántico'), invoice.id)
    expect(screen.getByText('Número de factura')).toBeVisible()
    expect(screen.getByText(/Referencia/).parentElement).toHaveTextContent('Opcional')
    expect(screen.getByText('lines')).toBeVisible()
    expect(screen.getByText('Cantidad (Decimal) · Concepto (Texto)')).toBeVisible()
    expect(screen.getByText(/guardar este perfil no aprueba ni publica documentos/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' })).toBeDisabled()
    await user.selectOptions(screen.getByLabelText('Lectura del documento'), 'NATIVE_FIRST')
    await user.type(screen.getByLabelText('Motivo del cambio de perfil'), ' Información común sin depender del diseño ')
    await user.click(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/families/family/semantic-profile', { profile_id: invoice.id, route: 'NATIVE_FIRST', reason: 'Información común sin depender del diseño' }))
    expect(await screen.findByText(/Ya puedes cargar documentos en esta familia/)).toBeVisible()
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('Motivo del cambio de perfil')).toHaveValue('')
  })

  it('displays configuration read-only without offering an unauthorized mutation', async () => {
    renderApp(<DocumentSemanticProfile familyId="family" current={{ profile_name: 'Factura guardada', profile_revision: 2 }} canConfigure={false} refresh={vi.fn()}/> )
    expect(await screen.findByLabelText('Perfil semántico')).toBeDisabled()
    expect(screen.getByText('Factura guardada')).toBeVisible()
    expect(screen.getByText(/Los documentos anteriores conservan su contrato/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Usar perfil para documentos nuevos' })).not.toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('keeps a rejected profile save visible and preserves the current profile without reporting success', async () => {
    const user = userEvent.setup(), refresh = vi.fn()
    vi.mocked(post).mockRejectedValue(new Error('El servicio general está deshabilitado.'))
    renderApp(<DocumentSemanticProfile familyId="family" canConfigure refresh={refresh}/> )
    await user.selectOptions(await screen.findByLabelText('Perfil semántico'), invoice.id)
    await user.type(screen.getByLabelText('Motivo del cambio de perfil'), 'Nueva prueba controlada')
    await user.click(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('El servicio general está deshabilitado.')
    expect(screen.queryByText(/Ya puedes cargar documentos/)).not.toBeInTheDocument()
    expect(refresh).not.toHaveBeenCalled()
  })

  it('does not enable selection when availability cannot be verified', async () => {
    vi.mocked(api).mockRejectedValue(new Error('No hay conexión con la API.'))
    renderApp(<DocumentSemanticProfile familyId="family" canConfigure refresh={vi.fn()}/> )
    expect(await screen.findByRole('alert')).toHaveTextContent('No hay conexión con la API.')
    expect(screen.queryByLabelText('Perfil semántico')).not.toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it('submits a custom declared field and collection contract without copying a visual layout or expected values', async () => {
    const user = userEvent.setup()
    renderApp(<DocumentSemanticProfile familyId="family" canConfigure refresh={vi.fn()}/> )
    await user.selectOptions(await screen.findByLabelText('Perfil semántico'), invoice.id)
    await user.click(screen.getByRole('checkbox', { name: 'Personalizar información solicitada' }))
    await user.clear(screen.getByLabelText('Nombre del perfil personalizado'))
    await user.type(screen.getByLabelText('Nombre del perfil personalizado'), 'Orden documental')
    await user.clear(screen.getByLabelText('Información a extraer documento 1'))
    await user.type(screen.getByLabelText('Información a extraer documento 1'), 'Número de orden')
    await user.click(screen.getByRole('button', { name: 'Quitar colección 1' }))
    await user.click(screen.getByRole('button', { name: 'Añadir tabla o colección' }))
    await user.type(screen.getByLabelText('Información de colección 1'), 'Una fila por cada acción expresamente solicitada.')
    await user.clear(screen.getByLabelText('Información a extraer colección 1 1'))
    await user.type(screen.getByLabelText('Información a extraer colección 1 1'), 'Acción solicitada')
    await user.type(screen.getByLabelText('Motivo del cambio de perfil'), 'Esquema por significado sin ubicación particular')
    await user.click(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1))
    const [, body] = vi.mocked(post).mock.calls[0]
    expect(body).toMatchObject({ route: 'QWEN_VISUAL', reason: 'Esquema por significado sin ubicación particular', profile: {
      schema_version: 'SEMANTIC_GENERAL_V1', id: expect.stringMatching(/^custom-/), name: 'Orden documental', revision: 1,
      fields: [{ ...invoice.fields[0], label: 'Número de orden' }, invoice.fields[1]],
      tables: [{ name: 'collection_1', description: 'Una fila por cada acción expresamente solicitada.', kind: 'COLLECTION', required: false, fields: [{ name: 'field_1', label: 'Acción solicitada', logical_type: 'STRING' }] }],
    } })
    expect(body).not.toHaveProperty('profile_id')
    expect(body).not.toHaveProperty('profile.sha256')
    expect(body).not.toHaveProperty('profile.region')
    expect(invoice.fields[0].label).toBe('Número de factura')
    expect(invoice.tables[0].name).toBe('lines')
  })

  it('assigns a new revision to edits after saving and preserves the submitted earlier contract', async () => {
    const user = userEvent.setup()
    vi.mocked(post).mockImplementation(async (_path, body) => {
      const profile = (body as { profile: { id: string; revision: number } }).profile
      return { profile: { id: profile.id, revision: profile.revision } }
    })
    renderApp(<DocumentSemanticProfile familyId="family" canConfigure refresh={vi.fn()}/> )
    await user.selectOptions(await screen.findByLabelText('Perfil semántico'), invoice.id)
    await user.click(screen.getByRole('checkbox', { name: 'Personalizar información solicitada' }))
    await user.type(screen.getByLabelText('Motivo del cambio de perfil'), 'Primer contrato personalizado')
    await user.click(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' }))
    await screen.findByText(/Ya puedes cargar documentos/)
    const first = vi.mocked(post).mock.calls[0][1] as { profile: { id: string; revision: number; fields: typeof invoice.fields } }
    expect(first.profile.revision).toBe(1)
    await user.clear(screen.getByLabelText('Información a extraer documento 1'))
    await user.type(screen.getByLabelText('Información a extraer documento 1'), 'Identificador del documento')
    expect(screen.getByLabelText('Revisión del perfil personalizado')).toHaveValue(2)
    await user.type(screen.getByLabelText('Motivo del cambio de perfil'), 'Segundo contrato, sin sobrescribir el primero')
    await user.click(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    const second = vi.mocked(post).mock.calls[1][1] as typeof first
    expect(second.profile.id).toBe(first.profile.id)
    expect(second.profile.revision).toBe(2)
    expect(second.profile.fields[0].label).toBe('Identificador del documento')
    expect(first.profile.fields[0].label).toBe('Número de factura')
    expect(first.profile.revision).toBe(1)
  })

  it('keeps a version conflict visible and allows an explicit higher revision without changing the old request', async () => {
    const user = userEvent.setup()
    vi.mocked(post).mockRejectedValueOnce(new ApiError('La revisión ya existe con otro contrato.', 409, undefined, 'DOCUMENT_SEMANTIC_PROFILE_REVISION_CONFLICT'))
    renderApp(<DocumentSemanticProfile familyId="family" canConfigure refresh={vi.fn()}/> )
    await user.selectOptions(await screen.findByLabelText('Perfil semántico'), invoice.id)
    await user.click(screen.getByRole('checkbox', { name: 'Personalizar información solicitada' }))
    await user.type(screen.getByLabelText('Motivo del cambio de perfil'), 'Cambio solicitado')
    await user.click(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' }))
    expect(await screen.findByText(/Incrementa la revisión del perfil personalizado/)).toBeVisible()
    expect(screen.queryByText(/Ya puedes cargar documentos/)).not.toBeInTheDocument()
    await user.clear(screen.getByLabelText('Revisión del perfil personalizado'))
    await user.type(screen.getByLabelText('Revisión del perfil personalizado'), '2')
    await user.click(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' }))
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2))
    expect(vi.mocked(post).mock.calls[0][1]).toHaveProperty('profile.revision', 1)
    expect(vi.mocked(post).mock.calls[1][1]).toHaveProperty('profile.revision', 2)
  })
})
