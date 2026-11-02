import { screen, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import type { RecordData } from '../../api/client'
import { renderApp } from '../../test/render'
import Documents from './Documents'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))
vi.mock('./DocumentPreview', () => ({ default: ({ source, alt, children }: { source: string; alt: string; children?: ReactNode }) => <div><img src={source} alt={alt}/>{children}</div> }))
const profile = { id: 'invoice-v1', name: 'Factura general', document_type: 'INVOICE', revision: 1, sha256: 'semantic-sha', fields: [{ name: 'number', label: 'Número', logical_type: 'STRING', required: true }], tables: [] }
let family: RecordData, enabled: boolean
beforeEach(() => {
  vi.clearAllMocks(); enabled = true
  family = { id: 'general-family', name: 'Documentos nuevos', description: 'Extracción por contenido', active_template_id: null, document_count: 0, pending_count: 0, output_datasets: {}, templates: [] }
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path === '/documents/families/general-family') return family
    if (path === '/documents/semantic-profiles') return { enabled, default_enabled: false, mode: 'SEMANTIC_GENERAL_V1', profiles: [profile] }
    if (path.startsWith('/documents/originals?') || path === '/documents/families/general-family/batches' || path === '/documents/families/general-family/builder/drafts') return { items: [], total: 0 }
    if (path === '/documents/families/general-family/originals' && options?.method === 'POST') return { items: [{ id: 'uploaded', status: 'QUEUED' }] }
    throw new Error(`Unexpected API path ${path}`)
  })
  vi.mocked(post).mockImplementation(async path => {
    if (path !== '/documents/families/general-family/semantic-profile') throw new Error(`Unexpected mutation ${path}`)
    family = { ...family, active_template_id: 'semantic-contract-1', semantic_general: { mode: 'SEMANTIC_GENERAL_V1', profile_id: profile.id, profile_name: profile.name, profile_revision: 1, profile_sha256: profile.sha256, route: 'QWEN_VISUAL' } }
    return { family, profile: { id: profile.id, revision: 1 } }
  })
})

describe('General extraction within the existing document module', () => {
  it.each([false, true])('keeps legacy visual configuration and the native pipeline visible when semantic capability enabled=%s', async capability => {
    enabled = capability
    family = { ...family, active_template_id: 'legacy-template', output_datasets: { headers: 'legacy-output' }, templates: [{ id: 'legacy-template', revision: 1, sha256: 'legacy-hash', definition: { fields: profile.fields, tables: [] } }] }
    const user = userEvent.setup()
    renderApp(<Documents/>, { path: '/documents/families/general-family', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:write', 'documents:configure', 'documents:download'] })
    await user.click(await screen.findByRole('tab', { name: 'Configuración' }))
    await screen.findByLabelText('Perfil semántico')
    expect(screen.getByRole('button', { name: 'Crear borrador' })).toBeVisible()
    expect(screen.getByLabelText('Finalizar el flujo en')).toBeVisible()
    expect(screen.queryByText('Configuración avanzada con plantilla visual', { exact: true })).not.toBeInTheDocument()
    expect(post).not.toHaveBeenCalled()
  })

  it.each([false, true])('collapses optional visual configuration only for an active semantic family with capability enabled=%s', async capability => {
    enabled = capability
    const metadata = { mode: 'SEMANTIC_GENERAL_V1', profile_id: profile.id, profile_name: profile.name, profile_revision: 1, profile_sha256: profile.sha256, route: 'QWEN_VISUAL' }
    family = { ...family, active_template_id: 'semantic-template', semantic_general: metadata, output_datasets: { headers: 'semantic-output' }, templates: [{ id: 'semantic-template', revision: 1, sha256: 'semantic-hash', definition: { fields: profile.fields, tables: [], semantic_general: metadata } }] }
    const user = userEvent.setup()
    renderApp(<Documents/>, { path: '/documents/families/general-family', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:write', 'documents:configure', 'documents:download'] })
    await user.click(await screen.findByRole('tab', { name: 'Configuración' }))
    await screen.findByLabelText('Perfil semántico')
    expect(screen.queryByRole('button', { name: 'Crear borrador' })).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Finalizar el flujo en')).not.toBeInTheDocument()
    await user.click(screen.getByText('Configuración avanzada con plantilla visual', { exact: true }))
    expect(screen.getByRole('button', { name: 'Crear borrador' })).toBeVisible()
    expect(screen.getByLabelText('Finalizar el flujo en')).toBeVisible()
    expect(post).not.toHaveBeenCalled()
  })

  it('enables an existing family upload through an explicit semantic profile without a visual builder', async () => {
    const user = userEvent.setup()
    renderApp(<Documents/>, { path: '/documents/families/general-family', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:write', 'documents:configure'] })
    expect(await screen.findByRole('button', { name: 'Cargar documentos' })).toBeDisabled()
    expect(vi.mocked(api).mock.calls.some(([path]) => path === '/documents/semantic-profiles')).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Configurar extracción general' }))
    await user.selectOptions(await screen.findByLabelText('Perfil semántico'), JSON.stringify([profile.id, profile.revision]))
    await user.type(screen.getByLabelText('Motivo del cambio de perfil'), 'Información independiente del diseño')
    await user.click(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' }))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Cargar documentos' })).toBeEnabled())
    expect(screen.getByText('Perfil vigente:', { exact: false })).toHaveTextContent('Factura general')
    expect(vi.mocked(api).mock.calls.some(([path]) => path.startsWith('/documents/builder'))).toBe(false)
    await user.click(screen.getByRole('button', { name: 'Cargar documentos' }))
    const source = new File(['synthetic PDF source'], 'different-design.pdf', { type: 'application/pdf' })
    await user.upload(screen.getByLabelText('Seleccionar documentos'), source)
    await waitFor(() => expect(vi.mocked(api).mock.calls.some(([path, options]) => path === '/documents/families/general-family/originals' && options?.method === 'POST')).toBe(true))
    const [, options] = vi.mocked(api).mock.calls.find(([path, options]) => path.endsWith('/originals') && options?.method === 'POST')!
    expect(options?.body).toBeInstanceOf(FormData)
    const upload = options?.body as FormData
    expect(upload.get('files')).toEqual(source)
    expect(upload.has('template_id')).toBe(false)
    expect(upload.has('is_example')).toBe(false)
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('keeps upload blocked when the optional mode is disabled instead of fabricating a processing state', async () => {
    enabled = false
    const user = userEvent.setup()
    renderApp(<Documents/>, { path: '/documents/families/general-family', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:write', 'documents:configure'] })
    await user.click(await screen.findByRole('button', { name: 'Configurar extracción general' }))
    expect(await screen.findByText(/está deshabilitada en este entorno/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Cargar documentos' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Usar perfil para documentos nuevos' })).toBeDisabled()
    expect(post).not.toHaveBeenCalled()
    expect(screen.queryByText('En curso')).not.toBeInTheDocument()
  })

  it('submits an authorized value and explicit empty-collection review through the native review endpoint', async () => {
    const user = userEvent.setup()
    const definition = { fields: profile.fields, tables: [{ name: 'items', semantic_collection: true, fields: [{ name: 'description', logical_type: 'STRING' }] }], semantic_general: { mode: 'SEMANTIC_GENERAL_V1', profile_id: profile.id, profile_name: profile.name, profile_revision: 1, profile_sha256: profile.sha256 } }
    const original = { id: 'general-original', family_id: family.id, filename: 'controlled.pdf', status: 'REVIEW_REQUIRED', template_id: 'semantic-contract', template: { definition },
      issues: ['REVIEW_REQUIRED:headers.0.number', 'SEMANTIC_TABLE_COVERAGE_REVIEW_REQUIRED:items'], publications: [], revisions: [],
      semantic_review: { mode: 'SEMANTIC_GENERAL_V1', automatic_approval: false, values: [{ path: 'headers.0.number', state: 'NORMALIZED_PENDING_VERIFICATION', observed: 'R-123', normalized: 'R-123', verified: null }], tables: [{ name: 'items', review_required: true, latest_confirmation: null }] },
      extraction: { id: 'semantic-extraction-1', revision: 1, payload: { fields: { number: { observed: 'R-123', value: 'R-123', validity: 'VALID', reviewed: false, origin: { page: 1, method: 'QWEN_VISUAL' }, geometry_kind: 'PAGE_LOCATOR_ONLY' } }, tables: { items: [] }, pages: [1] } },
    }
    const baseApi = vi.mocked(api).getMockImplementation()!
    vi.mocked(api).mockImplementation(async (path, options) => path === '/documents/originals/general-original' ? original : baseApi(path, options))
    vi.mocked(post).mockResolvedValue({ ...original, status: 'REVIEWED', extraction: { ...original.extraction, id: 'reviewed-semantic-2', revision: 2 } })
    renderApp(<Documents/>, { path: '/documents/families/general-family?document=general-original', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:review', 'documents:download', 'documents:publish'] })
    expect(await screen.findByLabelText('Valor headers.0.number')).toHaveValue('R-123')
    expect(screen.getByText('Normalizado; pendiente de verificación')).toBeVisible()
    expect(screen.getByRole('button', { name: 'Publicar esta revisión en datasets' })).toBeDisabled()
    expect(screen.getByText('Filas conservadas: 0')).toBeVisible()
    await user.click(screen.getByRole('checkbox', { name: 'Confirmar valor' }))
    await user.click(screen.getByRole('checkbox', { name: 'Confirmé en el original la cobertura de items' }))
    await user.type(screen.getByLabelText('Motivo de la revisión'), 'Comprobé el valor y la ausencia de detalle contra todas las páginas del original.')
    await user.click(screen.getByRole('button', { name: 'Guardar revisión y comprobar publicación' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/originals/general-original/review', { expected_extraction_id: 'semantic-extraction-1', changes: {}, confirmed: ['headers.0.number'], table_rows: {}, semantic_table_confirmations: ['items'], reason: 'Comprobé el valor y la ausencia de detalle contra todas las páginas del original.' }))
    expect(screen.queryByText('Aprobación automática verificada')).not.toBeInTheDocument()
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('allows explicit source review of an optional absent field when the server requires it', async () => {
    const user = userEvent.setup()
    const optional = { name: 'reference', label: 'Referencia opcional', logical_type: 'STRING', required: false }
    const original = { id: 'optional-original', family_id: family.id, filename: 'optional.pdf', status: 'REVIEW_REQUIRED', template_id: 'semantic-contract',
      template: { definition: { fields: [optional], tables: [], semantic_general: { mode: 'SEMANTIC_GENERAL_V1' } } },
      issues: ['REVIEW_REQUIRED:headers.0.reference'], publications: [], revisions: [],
      extraction: { id: 'optional-extraction', revision: 1, payload: { fields: { reference: { observed: null, value: null, validity: 'VALID', reviewed: false, recognition_status: 'NOT_APPLICABLE', origin: null } }, tables: {}, pages: [1] } },
    }
    const baseApi = vi.mocked(api).getMockImplementation()!
    vi.mocked(api).mockImplementation(async (path, options) => path === '/documents/originals/optional-original' ? original : baseApi(path, options))
    vi.mocked(post).mockResolvedValue(original)
    renderApp(<Documents/>, { path: '/documents/families/general-family?document=optional-original', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:review'] })
    await screen.findByLabelText('Valor headers.0.reference')
    expect(screen.getByText('Confirmación pendiente').parentElement).toHaveTextContent('1')
    await user.click(screen.getByRole('checkbox', { name: 'Confirmar valor' }))
    await user.type(screen.getByLabelText('Motivo de la revisión'), 'Comprobada la ausencia documental de la referencia opcional.')
    await user.click(screen.getByRole('button', { name: 'Guardar revisión y comprobar publicación' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/originals/optional-original/review', { expected_extraction_id: 'optional-extraction', changes: {}, confirmed: ['headers.0.reference'], table_rows: {}, semantic_table_confirmations: [], reason: 'Comprobada la ausencia documental de la referencia opcional.' }))
  })
})
