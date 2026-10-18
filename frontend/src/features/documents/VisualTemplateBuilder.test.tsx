import { screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, post } from '../../api/client'
import { renderApp } from '../../test/render'
import VisualTemplateBuilder from './VisualTemplateBuilder'

vi.mock('../../api/client', async importOriginal => ({ ...await importOriginal<typeof import('../../api/client')>(), api: vi.fn(), post: vi.fn() }))
const definition = { fields: [{ name: 'invoice', section: 'DOCUMENT', logical_type: 'STRING', label: 'Invoice', visual: { label: 'Invoice', page: 1, region: [.3, .1, .4, .12], direction: 'RIGHT', kind: 'LABEL_VALUE' } }], tables: [] }
const initial = { id: 'draft', name: 'Guardado', version: 3, definition, reference_ids: ['reference'], discovered_from: [] }
const payload = { fields: { invoice: { value: '000001', observed: '000001', validity: 'VALID', confidence: 96, reviewed: false, origin: { page: 1, region: [.3, .1, .4, .12] } } }, tables: {}, table_unparsed: {}, pages: [1], lines: [{ page: 1, words: [{ text: '000001', region: [.3, .1, .4, .12] }] }] }
const reference = { id: 'reference', filename: 'invoice.png', status: 'READY', role: 'CONFIGURATION', payload }
const tested = { id: 'test', draft_id: 'draft', draft_version: 3, snapshot_hash: '1234567890abcdef', automatic_results: { reference: payload }, reviewed_results: {}, review_version: 1, issues: { reference: ['REVIEW_REQUIRED:headers.0.invoice'] }, automatic_metrics: { reference: { recognized: 1, fields_and_cells: 1, rows: 0, ambiguous: 0, unparsed_regions: 0 } }, reviewed_metrics: { reference: { human_confirmed: 0, fields_and_cells: 1, corrected: 0 } }, comparisons: [] }

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.endsWith('/builder/drafts')) return { items: [initial], total: 1 }
    if (path === '/documents/builder/drafts/draft') return options?.method === 'PUT' ? { ...initial, ...JSON.parse(String(options.body)), version: 4 } : initial
    if (path.includes('/builder/references?')) return { items: [reference], total: 1 }
    if (path.endsWith('/builder/references') && options?.method === 'POST') return { items: [{ ...reference, id: 'new' }, { ...reference, id: 'new2' }], total: 2 }
    if (path.startsWith('/documents/builder/references/')) return reference
    throw new Error(`Unexpected API: ${path}`)
  })
  vi.mocked(post).mockImplementation(async path => {
    if (path.endsWith('/builder/drafts')) return initial
    if (path.endsWith('/discover')) return { draft: { ...initial, version: 4 }, proposal: { notice: 'Propuesta multiejemplo', expected_not_located: ['moneda'] } }
    if (path.endsWith('/test')) return tested
    if (path.endsWith('/propose')) return { field: definition.fields[0], notice: 'Ancla comprobada' }
    if (path.endsWith('/review')) return { ...tested, review_version: 2, reviewed_results: { reference: payload } }
    return { id: 'formal', revision: 1 }
  })
})

describe('DOC097 independent draft and references', () => {
  it('requires a chosen draft or explicit revision copy and never uses the latest template automatically', async () => {
    const user = userEvent.setup()
    renderApp(<VisualTemplateBuilder identity="family" templates={[{ id: 'historical', revision: 9 }]} existing={definition} refresh={vi.fn()}/>)
    expect(screen.queryByLabelText('Nombre del campo')).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Base de un nuevo borrador'), 'historical')
    await user.click(screen.getByRole('button', { name: 'Crear borrador' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/families/family/builder/drafts', { source_template_id: 'historical' }))
    expect(await screen.findByLabelText('Nombre del campo')).toHaveValue('invoice')
    expect(localStorage.getItem('document-draft:family')).toBe('draft')
  })

  it('recovers the chosen server draft and uploads several references without formal revision creation', async () => {
    localStorage.setItem('document-draft:family', 'draft')
    const user = userEvent.setup()
    renderApp(<VisualTemplateBuilder identity="family" refresh={vi.fn()}/>)
    expect(await screen.findByLabelText('Nombre del campo')).toHaveValue('invoice')
    await user.upload(screen.getByLabelText('Cargar referencias'), [new File(['a'], 'a.pdf', { type: 'application/pdf' }), new File(['b'], 'b.png', { type: 'image/png' })])
    await waitFor(() => expect(api).toHaveBeenCalledWith('/documents/families/family/builder/references', expect.objectContaining({ method: 'POST', body: expect.any(FormData) })))
    const call = vi.mocked(api).mock.calls.find(([path, options]) => path.endsWith('/builder/references') && options?.method === 'POST')!
    expect((call[1]!.body as FormData).getAll('files')).toHaveLength(2)
    await user.click(screen.getByRole('button', { name: 'Guardar borrador' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/documents/builder/drafts/draft', expect.objectContaining({ method: 'PUT', body: expect.stringContaining('"expected_version":3') })))
    expect(post).not.toHaveBeenCalledWith(expect.stringContaining('/templates'), expect.anything())
  })

  it('discovers then tests the same server snapshot and keeps value corrections separate from rule changes', async () => {
    localStorage.setItem('document-draft:family', 'draft')
    const user = userEvent.setup()
    renderApp(<VisualTemplateBuilder identity="family" refresh={vi.fn()}/>)
    await user.click(await screen.findByRole('button', { name: 'Analizar documentos y proponer plantilla' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/builder/drafts/draft/discover', { expected_version: 3 }))
    await user.click(screen.getByRole('button', { name: 'Probar todas las referencias' }))
    expect(await screen.findByLabelText('Corregir valor de este documento: invoice')).toHaveValue('000001')
    await user.clear(screen.getByLabelText('Corregir valor de este documento: invoice'))
    await user.type(screen.getByLabelText('Corregir valor de este documento: invoice'), '000099')
    await user.click(screen.getByLabelText('Confirmar contra original'))
    await user.type(screen.getByLabelText('Motivo de la revisión de valores'), 'Comprobado contra original')
    await user.click(screen.getByRole('button', { name: 'Guardar revisión de este documento' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/builder/tests/test/review', expect.objectContaining({ expected_version: 1, reference_id: 'reference', changes: { 'headers.0.invoice': '000099' }, confirmed: ['headers.0.invoice'] })))
    expect(screen.getByText(/La cobertura reconocida no demuestra exactitud automática/)).toBeVisible()
    await user.click(screen.getByRole('button', { name: 'Corregir regla para futuros documentos' }))
    await user.type(screen.getByLabelText('Nombre del campo'), '_nuevo')
    expect(screen.getByText(/Cambios sin guardar; pruebas invalidadas/)).toBeVisible()
    expect(screen.queryByRole('button', { name: 'Aprobar y activar esta instantánea' })).not.toBeInTheDocument()
  })

  it('shows a concurrency conflict while preserving the local edited field', async () => {
    localStorage.setItem('document-draft:family', 'draft')
    const base = vi.mocked(api).getMockImplementation()!
    vi.mocked(api).mockImplementation(async (path, options) => { if (options?.method === 'PUT') throw new ApiError('Otra persona guardó este borrador. Recarga su versión.', 409, undefined, 'DOCUMENT_DRAFT_CONFLICT'); return base(path, options) })
    const user = userEvent.setup()
    renderApp(<VisualTemplateBuilder identity="family" refresh={vi.fn()}/>)
    await user.type(await screen.findByLabelText('Nombre del campo'), '_local')
    await user.click(screen.getByRole('button', { name: 'Guardar borrador' }))
    expect(await screen.findByText(/Otra persona guardó este borrador/)).toBeVisible()
    expect(screen.getByLabelText('Nombre del campo')).toHaveValue('invoice_local')
  })

  it('saves manual advanced rules into the chosen draft and requires the same test before activation', async () => {
    localStorage.setItem('document-draft:family', 'draft')
    const user = userEvent.setup()
    renderApp(<VisualTemplateBuilder identity="family" refresh={vi.fn()}/>)
    await user.click(await screen.findByText('Configuración técnica avanzada del borrador'))
    const editor = screen.getByLabelText('Definición avanzada del borrador')
    await user.clear(editor)
    await user.paste(JSON.stringify({ ...definition, fields: [{ ...definition.fields[0], name: 'manual_rule', pattern: '([0-9]+)' }] }))
    await user.click(screen.getByRole('button', { name: 'Aplicar cambios avanzados al borrador' }))
    await user.click(screen.getByRole('button', { name: 'Guardar borrador' }))
    await waitFor(() => expect(api).toHaveBeenCalledWith('/documents/builder/drafts/draft', expect.objectContaining({ method: 'PUT', body: expect.stringContaining('manual_rule') })))
    expect(vi.mocked(post).mock.calls.some(([path]) => path.includes('/templates'))).toBe(false)
    expect(screen.queryByRole('button', { name: 'Aprobar y activar esta instantánea' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Probar todas las referencias' })).toBeEnabled()
  })
})
