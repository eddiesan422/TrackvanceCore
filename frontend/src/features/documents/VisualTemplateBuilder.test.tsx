import { fireEvent, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api, post } from '../../api/client'
import { renderApp } from '../../test/render'
import VisualTemplateBuilder from './VisualTemplateBuilder'

vi.mock('../../api/client', () => ({ api: vi.fn(), post: vi.fn() }))
const field = { name: 'invoice', logical_type: 'STRING', required: true, critical: false, business_key: false, pattern: '(.+)', visual: { kind: 'LABEL_VALUE', page: 1, region: [.3, .1, .4, .12], label: 'Invoice', direction: 'RIGHT', sample_value: '000001', context: 'Invoice: 000001', tolerance: .12 } }
const table = { name: 'detalle', required: true, visual: { page: 1, region: [.1, .3, .7, .6], header_labels: ['Code', 'Qty'], column_bounds: [.1, .4, .7], stop_label: 'Total', tolerance: .12 }, fields: [{ name: 'code', logical_type: 'STRING', required: true }, { name: 'qty', logical_type: 'INT64', required: true }] }
const original = { id: 'example', filename: 'invoice.png', is_example: true, status: 'REVIEW_REQUIRED', extraction: { id: 'extraction', payload: { fields: { invoice: { value: '000001', validity: 'VALID' } }, tables: { detalle: [{ code: { value: 'A100', validity: 'VALID' }, qty: { value: '2', validity: 'VALID' } }] } } } }
const geometry = { document: original, pages: [1], lines: [{ page: 1, text: 'Invoice: 000001', region: [.1, .1, .4, .12], words: [{ text: 'Invoice:', region: [.1, .1, .25, .12] }, { text: '000001', region: [.3, .1, .4, .12] }] }] }

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api).mockImplementation(async (path, options) => {
    if (path.includes('/builder/examples') && options?.method === 'POST') return original
    if (path.startsWith('/documents/originals?')) return { items: [original], total: 1 }
    if (path.endsWith('/builder')) return geometry
    if (path.startsWith('/documents/originals/')) return original
    throw new Error(`Unexpected path: ${path}`)
  })
  vi.mocked(post).mockImplementation(async (path, body) => path.endsWith('/propose') ? ((body as { kind: string }).kind === 'TABLE' ? { table, notice: 'Revisa las columnas' } : { field, notice: 'Confirma el tipo' }) : { ...original, id: 'tested-example' })
})

describe('Visual OCR template builder', () => {
  it('loads a local example before requiring extraction rules', async () => {
    const user = userEvent.setup()
    renderApp(<VisualTemplateBuilder identity="family" refresh={vi.fn()}/>)
    expect(screen.getByText(/No necesitas configurar expresiones regulares/)).toBeVisible()
    const file = new File(['synthetic'], 'invoice.png', { type: 'image/png' })
    await user.upload(screen.getByLabelText('Cargar ejemplo para constructor visual'), file)
    await waitFor(() => expect(api).toHaveBeenCalledWith('/documents/families/family/builder/examples', expect.objectContaining({ method: 'POST', body: expect.any(FormData) })))
    expect(await screen.findByRole('button', { name: 'Seleccionar 000001' })).toBeVisible()
  })

  it('proposes a field from a real word region, edits flags and tests an immutable revision without regex', async () => {
    const user = userEvent.setup()
    const review = vi.fn()
    renderApp(<VisualTemplateBuilder identity="family" refresh={vi.fn()} onReviewDocument={review}/>)
    await screen.findByRole('option', { name: /invoice.png/ })
    await user.selectOptions(screen.getByRole('combobox', { name: 'O continuar con un ejemplo recibido' }), 'example')
    await user.click(await screen.findByRole('button', { name: 'Seleccionar 000001' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/originals/example/builder/propose', { kind: 'FIELD', selection: { page: 1, region: [.3, .1, .4, .12] } }))
    expect(await screen.findByRole('textbox', { name: 'Nombre del campo' })).toHaveValue('invoice')
    expect(screen.queryByRole('textbox', { name: /Patrón/ })).not.toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Crítico' }))
    await user.click(screen.getByRole('checkbox', { name: 'Clave de negocio' }))
    await user.click(screen.getByRole('button', { name: 'Volver a marcar este campo' }))
    await user.click(screen.getByRole('button', { name: 'Seleccionar 000001' }))
    await waitFor(() => expect(screen.getAllByRole('textbox', { name: 'Nombre del campo' })).toHaveLength(1))
    expect(screen.getByRole('checkbox', { name: 'Crítico' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'Clave de negocio' })).toBeChecked()
    await user.click(screen.getByRole('button', { name: 'Crear revisión y probar extracción' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/families/family/builder/test', expect.objectContaining({ document_id: 'example', definition: expect.objectContaining({ fields: [expect.objectContaining({ critical: true, business_key: true, visual: expect.objectContaining({ label: 'Invoice' }) })] }) })))
    await user.click(await screen.findByRole('button', { name: 'Revisar resultados y activar plantilla' }))
    expect(review).toHaveBeenCalledWith('tested-example')
    expect(screen.getByText(/La prueba no activa la plantilla ni publica datasets/)).toBeVisible()
  })

  it('offers keyboard region selection, table proposals and editable column boundaries', async () => {
    const user = userEvent.setup()
    renderApp(<VisualTemplateBuilder identity="family" refresh={vi.fn()}/>)
    await screen.findByRole('option', { name: /invoice.png/ })
    await user.selectOptions(screen.getByRole('combobox', { name: 'O continuar con un ejemplo recibido' }), 'example')
    await user.click(await screen.findByRole('button', { name: 'Tabla' }))
    await user.click(screen.getByText('Seleccionar una región con teclado'))
    for (const [name, value] of [['Izquierda (%)', '10'], ['Arriba (%)', '30'], ['Derecha (%)', '70'], ['Abajo (%)', '60']]) fireEvent.change(screen.getByRole('spinbutton', { name }), { target: { value } })
    await user.click(screen.getByRole('button', { name: 'Proponer desde esta región' }))
    await waitFor(() => expect(post).toHaveBeenCalledWith('/documents/originals/example/builder/propose', { kind: 'TABLE', selection: { page: 1, region: [.1, .3, .7, .6] } }))
    const boundary = await screen.findByRole('spinbutton', { name: 'Límite derecho columna 1 (%)' })
    fireEvent.change(boundary, { target: { value: '45' } })
    expect(screen.getByRole('spinbutton', { name: 'Límite izquierdo columna 2 (%)' })).toHaveValue(45)
    expect(screen.queryByRole('textbox', { name: /Patrón de fila/ })).not.toBeInTheDocument()
  })

  it('retains existing advanced rules while showing editable visual additions', async () => {
    renderApp(<VisualTemplateBuilder identity="family" existing={{ fields: [{ name: 'legacy', label: 'Invoice', pattern: '(.*)', logical_type: 'STRING' }], tables: [] }} refresh={vi.fn()}/>)
    expect(screen.getByRole('button', { name: 'Crear revisión y probar extracción' })).toBeDisabled()
    expect(screen.getByRole('combobox', { name: 'Período de negocio' })).toHaveTextContent('legacy')
  })

  it('resumes a reloaded first example without presenting the internal scaffold as a business field', async () => {
    const user = userEvent.setup()
    renderApp(<VisualTemplateBuilder identity="family" existing={{ fields: [{ name: 'por_configurar', label: '__constructor_por_configurar__', logical_type: 'STRING', required: true }], tables: [] }} refresh={vi.fn()}/>)
    await screen.findByRole('option', { name: /invoice.png/ })
    await user.selectOptions(screen.getByRole('combobox', { name: 'O continuar con un ejemplo recibido' }), 'example')
    await screen.findByRole('button', { name: 'Seleccionar 000001' })
    expect(screen.queryByRole('textbox', { name: 'Nombre del campo' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Seleccionar 000001' }))
    expect(await screen.findByRole('textbox', { name: 'Nombre del campo' })).toHaveValue('invoice')
    expect(screen.getAllByRole('textbox', { name: 'Nombre del campo' })).toHaveLength(1)
    expect(screen.getByRole('combobox', { name: 'Período de negocio' })).not.toHaveTextContent('por_configurar')
  })

  it('preserves a legitimate field with the same name but a different label', () => {
    renderApp(<VisualTemplateBuilder identity="family" existing={{ fields: [{ name: 'por_configurar', label: 'Invoice', logical_type: 'STRING' }], tables: [] }} refresh={vi.fn()}/>)
    expect(screen.getByRole('combobox', { name: 'Período de negocio' })).toHaveTextContent('por_configurar')
  })
})
