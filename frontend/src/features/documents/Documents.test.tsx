import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import Documents from './Documents'
import type { RecordData } from '../../api/client'

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

  it('uses validator exceptions without requesting confirmation of safely validated candidates', async () => {
    const user = userEvent.setup()
    const baseApi = vi.mocked(api).getMockImplementation()!
    vi.mocked(api).mockImplementation(async path => path === '/documents/originals/original' ? {
      ...original, issues: [], phase: 'COMPLETE', quality: { status: 'AUTO_VERIFIED', validator_version: 'idp098-v1' },
    } : baseApi(path))
    renderApp(<Documents/>, { path: '/documents/families/family?document=original', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:review'] })
    expect(await screen.findByLabelText('Valor headers.0.invoice')).toHaveValue('000001')
    expect(screen.getByText('Aprobación automática verificada')).toBeVisible()
    expect(screen.getByText(/revisión: No requerida por el validador/)).toBeVisible()
    expect(screen.queryByRole('checkbox', { name: 'Confirmar valor' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Siguiente incidencia (0)' })).toBeDisabled()
    await user.click(screen.getByLabelText('Mostrar sólo excepciones del validador'))
    expect(screen.queryByLabelText('Valor headers.0.invoice')).not.toBeInTheDocument()
    await user.click(screen.getByLabelText('Mostrar sólo excepciones del validador'))
    await user.clear(screen.getByLabelText('Valor headers.0.invoice'))
    await user.type(screen.getByLabelText('Valor headers.0.invoice'), '000002')
    await user.click(screen.getByRole('checkbox', { name: 'Confirmar valor' }))
    await user.type(screen.getByLabelText('Valor headers.0.invoice'), '3')
    expect(screen.getByRole('checkbox', { name: 'Confirmar valor' })).not.toBeChecked()
    expect(post).not.toHaveBeenCalled()
  })

  it('summarizes separate exceptions and synchronizes source page when navigating a field', async () => {
    const user = userEvent.setup()
    const baseApi = vi.mocked(api).getMockImplementation()!
    const good = original.extraction.payload.fields.invoice
    const fields = { invoice: { ...good, reviewed: true }, total: { ...good, validity: 'AMBIGUOUS', origin: { ...good.origin, page: 2 } }, missing: { ...good, value: null, validity: 'MISSING', origin: null }, malformed: { ...good, validity: 'INVALID' }, pending: good }
    vi.mocked(api).mockImplementation(async path => path === '/documents/originals/original' ? {
      ...original, issues: ['VALUE_INVALID:headers.0.total', 'VALUE_INVALID:headers.0.missing', 'VALUE_INVALID:headers.0.malformed', 'REVIEW_REQUIRED:headers.0.pending'],
      extraction: { ...original.extraction, payload: { ...original.extraction.payload, fields, pages: [1, 2] } },
    } : baseApi(path))
    renderApp(<Documents/>, { path: '/documents/families/family?document=original', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:review', 'documents:download'] })
    const summary = await screen.findByLabelText('Resumen de resultados')
    for (const label of ['Validados', 'Ambiguos', 'Faltantes', 'Inválidos', 'Confirmación pendiente']) expect(within(summary).getByText(label).parentElement).toHaveTextContent('1')
    await user.click(screen.getByRole('button', { name: 'Siguiente incidencia (4)' }))
    expect(screen.getByLabelText('Página del original')).toHaveValue('2')
    expect(screen.getByRole('img', { name: 'Página 2 del original PDF' })).toHaveAttribute('src', '/api/v1/documents/originals/original/pages/2')
    await user.click(screen.getByLabelText('Mostrar sólo excepciones del validador'))
    expect(screen.queryByLabelText('Valor headers.0.invoice')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Valor headers.0.total')).toBeVisible()
  })

  it('preserves partial candidates/source and requires confirming even optional absence before a value-only save', async () => {
    const user = userEvent.setup(), baseApi = vi.mocked(api).getMockImplementation()!
    const binding = { source_sha256: 'source-sha', expected_extraction_id: 'partial-1', expected_payload_hash: 'payload-sha', expected_marker_hash: 'marker-sha', pages: [1, 2] }
    const partial = { ...original, sha256: 'source-sha', quality: { status: 'AUTO_VERIFIED' }, error_code: 'DOCUMENT_TIMEOUT',
      incomplete_inference: { status: 'PARTIAL', failed_stage: 'REFINE', error_code: 'DOCUMENT_TIMEOUT', automatic_approval_blocked: true, requires_full_source_review: true, can_confirm: false, confirmation_binding: binding },
      issues: ['QUALITY_INCOMPLETE_SOURCE:REFINE'], template: { definition: { ...definition, fields: [...definition.fields, { name: 'optional', required: false }] } },
      extraction: { ...original.extraction, id: 'partial-1', sha256: 'payload-sha', payload: { ...original.extraction.payload, pages: [1, 2], fields: {
        ...original.extraction.payload.fields, optional: { value: null, observed: null, validity: 'VALID', recognition_status: 'NOT_APPLICABLE', reviewed: false, origin: null },
      } } } }
    vi.mocked(api).mockImplementation(async path => path === '/documents/originals/original' ? partial : baseApi(path))
    renderApp(<Documents/>, { path: '/documents/families/family?document=original', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:review', 'documents:download'] })
    const dialog = await screen.findByRole('dialog', { name: 'known.pdf' })
    expect(await within(dialog).findByLabelText('Valor headers.0.invoice')).toHaveValue('000001')
    expect(within(dialog).getByRole('img', { name: 'Página 1 del original PDF' })).toBeVisible()
    expect(within(dialog).getByText(/tiempo máximo en refinamiento de regiones/)).toBeVisible()
    expect(within(dialog).queryByText('Aprobación automática verificada')).not.toBeInTheDocument()
    expect(within(dialog).getByText(/Campo opcional sin valor: confirma su ausencia/)).toBeVisible()
    await user.type(within(dialog).getByLabelText('Motivo de la revisión'), 'Verifiqué los valores y la ausencia opcional contra el original.')
    const save = within(dialog).getByRole('button', { name: 'Guardar valores de la revisión parcial' })
    expect(save).toBeDisabled()
    for (const checkbox of within(dialog).getAllByRole('checkbox', { name: 'Confirmar valor' })) await user.click(checkbox)
    await user.click(save)
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/originals/original/review', { expected_extraction_id: 'partial-1', changes: {}, confirmed: ['headers.0.invoice', 'headers.0.optional'], reason: 'Verifiqué los valores y la ausencia opcional contra el original.', table_rows: {} }))
    expect(within(dialog).getByLabelText('Extracción parcial conservada')).toBeVisible()
    expect(within(dialog).getByRole('button', { name: 'Confirmar fuente completa mediante revisión manual' })).toBeDisabled()
  })

  it('sends only a separately acknowledged opaque full-source binding and resolves as manual, preserving the partial marker', async () => {
    const user = userEvent.setup(), baseApi = vi.mocked(api).getMockImplementation()!
    const binding = { source_sha256: 'source-sha', expected_extraction_id: 'partial-2', expected_payload_hash: 'reviewed-payload', expected_marker_hash: 'immutable-partial-marker', pages: [1, 2] }
    let current: RecordData = { ...original, sha256: binding.source_sha256, quality: { status: 'REVIEW_REQUIRED' },
      incomplete_inference: { status: 'PARTIAL', failed_stage: 'INTERPRET', error_code: 'DOCUMENT_TIMEOUT', automatic_approval_blocked: true, requires_full_source_review: true, can_confirm: true, confirmation_binding: binding },
      publications: [{ batch_id: 'old-batch', extraction_id: 'prior' }], issues: ['QUALITY_INCOMPLETE_SOURCE:INTERPRET'],
      extraction: { ...original.extraction, id: binding.expected_extraction_id, sha256: binding.expected_payload_hash, engine: { inference_completion: { marker: 'original-retained' } }, payload: { ...original.extraction.payload, pages: [1, 2], fields: { invoice: { ...original.extraction.payload.fields.invoice, reviewed: true } } } } }
    vi.mocked(api).mockImplementation(async path => path === '/documents/originals/original' ? current : baseApi(path))
    vi.mocked(post).mockImplementation(async (_path, body) => {
      expect((body as RecordData).changes).toEqual({})
      current = { ...current, quality: { status: 'HUMAN_VERIFIED' }, incomplete_inference: { ...current.incomplete_inference, status: 'HUMAN_RESOLVED', can_confirm: false, requires_full_source_review: false, confirmation_binding: undefined },
        extraction: { ...current.extraction, id: 'manual-resolution-3', revision: 3, sha256: 'new-manual-payload' } }
      return current
    })
    renderApp(<Documents/>, { path: '/documents/families/family?document=original', route: '/documents/families/:familyId', permissions: ['documents:read', 'documents:review', 'documents:download', 'documents:publish'] })
    const dialog = await screen.findByRole('dialog', { name: 'known.pdf' })
    await within(dialog).findByLabelText('Valor headers.0.invoice')
    expect(within(dialog).getByRole('button', { name: 'Publicar revisión como corrección' })).toBeDisabled()
    for (const page of [1, 2]) {
      await user.click(within(dialog).getByRole('button', { name: `Ver página ${page} del original completo` }))
      fireEvent.load(within(dialog).getByRole('img', { name: `Original completo, página ${page}` }))
    }
    await user.click(within(dialog).getByRole('checkbox', { name: 'He revisado completas todas las páginas del original.' }))
    await user.click(within(dialog).getByRole('checkbox', { name: /He comprobado la totalidad de filas/ }))
    await user.type(within(dialog).getByLabelText('Motivo de la confirmación de fuente completa'), 'Comparé ambas páginas y todas las filas con el original completo.')
    await user.click(within(dialog).getByRole('button', { name: 'Confirmar fuente completa mediante revisión manual' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/originals/original/review', { expected_extraction_id: 'partial-2', changes: {}, confirmed: [], table_rows: {}, header_confirmations: [], reason: 'Comparé ambas páginas y todas las filas con el original completo.', incomplete_source_confirmation: { ...binding, all_pages_confirmed: true, all_rows_confirmed: true } }))
    expect(await within(dialog).findByText('Revisión humana verificada')).toBeVisible()
    expect(within(dialog).queryByText('Aprobación automática verificada')).not.toBeInTheDocument()
    expect(current.extraction.engine.inference_completion).toEqual({ marker: 'original-retained' })
    expect(post).toHaveBeenCalledTimes(1)
  })
})
