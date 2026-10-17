import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import Documents from './Documents'

vi.mock('../../api/client', () => ({ api: vi.fn(), post: vi.fn() }))
const definition = { fields: [{ name: 'invoice', logical_type: 'STRING', required: true, critical: true, business_key: true, label: 'Invoice', pattern: '(.*)' }], tables: [], publication: { mode: 'SCHEDULED', timezone: 'America/Bogota' } }
const family = { id: 'family', name: 'Invoices', active_template_id: 'template', description: 'Known invoices', document_count: 1, pending_count: 1, output_datasets: { headers: 'stable-header' }, publication: definition.publication, templates: [{ id: 'template', revision: 1, sha256: 'hash', definition }] }
const original = { id: 'original', family_id: 'family', filename: 'known.pdf', template_id: 'template', status: 'REVIEW_REQUIRED', template: { definition }, issues: ['REVIEW_REQUIRED:headers.0.invoice'], publications: [], revisions: [], extraction: { id: 'extraction-1', revision: 1, payload: { fields: { invoice: { value: '000001', observed: '000001', validity: 'VALID', confidence: 99, reviewed: false, origin: { page: 1, region: [0.1, 0.2, 0.3, 0.4], method: 'OCR' } } }, tables: {} } } }

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockImplementation(async path => {
    if (path.startsWith('/documents/families?')) return { items: [family], total: 1 }
    if (path === '/documents/families/family') return family
    if (path.startsWith('/documents/originals?')) return { items: [original], total: 1 }
    if (path === '/documents/originals/original') return original
    if (path === '/documents/batches/batch-1') return { id: 'batch-1', family_id: 'family', status: 'PUBLISHED', settings: {}, members: [{ document_id: 'original', status: 'PUBLISHED' }], published_versions: { headers: 'version-2' } }
    if (path === '/documents/families/family/batches') return { items: [{ id: 'batch-1', status: 'PUBLISHED', settings: {}, members: [{ document_id: 'original', status: 'PUBLISHED' }], published_versions: { headers: 'version-2' } }], total: 1 }
    throw new Error('Unexpected API path in document UI test')
  })
  vi.mocked(post).mockResolvedValue(original)
})

describe('Documentos guided operations', () => {
  it('shows family scope and exposes creation only to the document configurator', async () => {
    renderApp(<Documents/>, { permissions: ['documents:read'] })
    expect(await screen.findByRole('link', { name: /Invoices/ })).toHaveAttribute('href', '/documents/families/family')
    expect(screen.queryByRole('button', { name: 'Nueva familia' })).not.toBeInTheDocument()
  })

  it('creates a family through the labeled keyboard-accessible dialog', async () => {
    const user = userEvent.setup()
    renderApp(<Documents/>, { permissions: ['documents:read', 'documents:configure'] })
    await user.click(screen.getByRole('button', { name: 'Nueva familia' }))
    await user.type(screen.getByRole('textbox', { name: 'Nombre' }), 'Invoices reviewed')
    await user.type(screen.getByRole('textbox', { name: 'Descripción' }), 'Common invoice schema')
    await user.click(screen.getByRole('button', { name: 'Crear familia' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/families', { name: 'Invoices reviewed', description: 'Common invoice schema' }))
  })

  it('opens an operation batch link in results and navigates exact stable output/version', async () => {
    renderApp(<Documents/>, { path: '/documents/families/family?batch=batch-1', route: '/documents/families/:familyId', permissions: ['documents:read'] })
    expect(await screen.findByRole('tab', { name: 'Lotes y resultados' })).toHaveAttribute('aria-selected', 'true')
    expect(await screen.findByRole('link', { name: 'headers: abrir versión publicada' })).toHaveAttribute('href', '/datasets/stable-header?version=version-2')
    expect(screen.getByRole('link', { name: 'Ver linaje' })).toHaveAttribute('href', '/catalog/datasets/stable-header?section=lineage&version_id=version-2')
    expect(screen.getByText(/el original requiere permisos documentales independientes/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Corregir este lote' })).not.toBeInTheDocument()
  })

  it('requires explicit impact acknowledgement and preserves the exact published document set for correction', async () => {
    const user = userEvent.setup()
    const baseApi = vi.mocked(api).getMockImplementation()!
    vi.mocked(api).mockImplementation(async path => path === '/documents/batches/batch-1' ? { id: 'batch-1', template_id: 'frozen-template', status: 'PUBLISHED', settings: {}, published_versions: { headers: 'version-2' }, members: [
        { document_id: 'original', extraction_id: 'old-extraction-1', status: 'PUBLISHED' },
        { document_id: 'second-original', extraction_id: 'old-extraction-2', status: 'PUBLISHED' },
        { document_id: 'deferred-original', status: 'DEFERRED' },
      ], family_id: 'family',
    } : baseApi(path))
    vi.mocked(post).mockResolvedValue({ id: 'correction-batch', status: 'BLOCKED' })
    renderApp(<Documents/>, { path: '/documents/families/family?batch=batch-1', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:publish'] })
    await user.click(await screen.findByRole('button', { name: 'Corregir este lote' }))
    expect(screen.getByRole('dialog', { name: 'Corregir lote publicado' })).toBeVisible()
    expect(screen.getByText(/mismos 2 documentos publicados/)).toBeVisible()
    expect(screen.getByText(/no ejecuta Intake ni Delivery automáticamente/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Publicar corrección del lote' })).toBeDisabled()
    expect(screen.getAllByRole('button', { name: /Revisar documento / })).toHaveLength(2)
    await user.click(screen.getByRole('checkbox', { name: /Confirmo la revisión de todos los documentos/ }))
    await user.click(screen.getByRole('button', { name: 'Publicar corrección del lote' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/families/family/batches', {
      template_id: 'frozen-template', document_ids: ['original', 'second-original'], correction: true, idempotency_key: expect.any(String),
    }))
  })

  it('keeps confidence distinct from human confirmation and requires original download permission', async () => {
    const user = userEvent.setup()
    renderApp(<Documents/>, { path: '/documents/families/family?document=original', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:review'] })
    expect(await screen.findByLabelText('Valor headers.0.invoice')).toHaveValue('000001')
    expect(screen.getByText(/confianza: 99.0% · revisión: Pendiente/)).toBeVisible()
    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getByText(/Se requiere permiso de descarga documental/)).toBeVisible()
    expect(screen.getByRole('button', { name: 'Guardar revisión y comprobar publicación' })).toBeDisabled()
    await user.click(screen.getByRole('checkbox', { name: 'Confirmar valor' }))
    await user.type(screen.getByLabelText('Motivo de la revisión'), 'Checked exact known invoice')
    await user.click(screen.getByRole('button', { name: 'Guardar revisión y comprobar publicación' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/originals/original/review', { expected_extraction_id: 'extraction-1', changes: {}, confirmed: ['headers.0.invoice'], reason: 'Checked exact known invoice', table_rows: {} }))
  })
})
