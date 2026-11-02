import { useState } from 'react'
import { screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import type { RecordData } from '../../api/client'
import { renderApp } from '../../test/render'
import SemanticProfileEditor from './SemanticProfileEditor'
import type { SemanticProfileDraft } from './SemanticProfileEditor'

function setup(field: RecordData) {
  const initial: SemanticProfileDraft = { id: 'custom-independent', name: 'Contrato propio', revision: 2, document_type: 'CUSTOM', description: 'Información solicitada.', fields: [field], tables: [] }
  const changed = vi.fn()
  function Editor() {
    const [value, setValue] = useState(initial)
    return <SemanticProfileEditor value={value} onChange={next => { changed(next); setValue(next) }} disabled={false}/>
  }
  renderApp(<Editor/>)
  return { changed, initial, latest: () => (changed.mock.lastCall![0] as SemanticProfileDraft).fields[0] }
}

describe('Declared normalization policies in custom semantic profiles', () => {
  it('lets the user replace numeric consensus with one explicit grammar without retaining a hidden competing policy', async () => {
    const user = userEvent.setup()
    const { initial, latest } = setup({ name: 'amount', label: 'Importe', logical_type: 'DECIMAL', required: true, normalization: { decimal_formats: 'UNAMBIGUOUS_LOCALE_V1' } })
    expect(screen.queryByLabelText('Separador decimal documento 1')).not.toBeInTheDocument()
    expect(screen.getByText(/dos valores distintos/)).toBeVisible()
    await user.click(screen.getByRole('checkbox', { name: 'Reconocer formatos numéricos comunes · documento 1' }))
    await user.selectOptions(screen.getByLabelText('Separador decimal documento 1'), ',')
    await user.selectOptions(screen.getByLabelText('Separador de miles documento 1'), '.')
    expect(latest()).toMatchObject({ decimal_separator: ',', thousands_separator: '.' })
    expect(latest().normalization?.decimal_formats).toBeUndefined()
    expect(initial.fields[0].normalization.decimal_formats).toBe('UNAMBIGUOUS_LOCALE_V1')
    await user.click(screen.getByRole('checkbox', { name: 'Reconocer formatos numéricos comunes · documento 1' }))
    expect(latest().normalization.decimal_formats).toBe('UNAMBIGUOUS_LOCALE_V1')
    expect(screen.queryByLabelText('Separador decimal documento 1')).not.toBeInTheDocument()
  })

  it('shows declared date alternatives and removes them before an explicit single-format choice', async () => {
    const user = userEvent.setup()
    const { initial, latest } = setup({ name: 'date', label: 'Fecha', logical_type: 'DATE', required: false, normalization: { date_formats: ['%Y-%m-%d', '%d/%m/%Y'] } })
    expect(screen.getByText(/AAAA-MM-DD, DD\/MM\/AAAA/)).toBeVisible()
    expect(screen.queryByLabelText('Formato de fecha documento 1')).not.toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Admitir varios formatos de fecha · documento 1' }))
    await user.clear(screen.getByLabelText('Formato de fecha documento 1'))
    await user.type(screen.getByLabelText('Formato de fecha documento 1'), '%d/%m/%Y')
    expect(latest().date_format).toBe('%d/%m/%Y')
    expect(latest().normalization?.date_formats).toBeUndefined()
    expect(initial.fields[0].normalization.date_formats).toEqual(['%Y-%m-%d', '%d/%m/%Y'])
  })

  it('removes incompatible normalization when changing the destination type', async () => {
    const user = userEvent.setup()
    const { initial, latest } = setup({ name: 'field', label: 'Dato', logical_type: 'DECIMAL', normalization: { decimal_formats: 'UNAMBIGUOUS_LOCALE_V1', decimal_scale: 2 } })
    await user.selectOptions(screen.getByLabelText('Tipo de dato documento 1'), 'DATE')
    expect(latest().normalization).toBeNull()
    expect(screen.getByLabelText('Formato de fecha documento 1')).toBeVisible()
    expect(initial.fields[0].normalization.decimal_scale).toBe(2)
  })

  it('protects identifiers from multiline equivalence and allows explicitly removing currency equivalences', async () => {
    const user = userEvent.setup()
    const { initial, latest } = setup({ name: 'text', label: 'Texto', logical_type: 'STRING', normalization: { currency_equivalences: { EUR: ['euro'] } } })
    const multiline = screen.getByRole('checkbox', { name: 'Unificar saltos de línea · documento 1' })
    expect(multiline).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Usar texto sin equivalencias de moneda · documento 1' }))
    expect(multiline).toBeEnabled()
    await user.click(multiline)
    expect(latest().normalization.text_linebreaks).toBe('DECLARED_MULTILINE_V1')
    expect(latest().normalization.currency_equivalences).toBeUndefined()
    await user.click(screen.getByRole('checkbox', { name: 'Clave de negocio · documento 1' }))
    expect(multiline).toBeDisabled()
    expect(multiline).not.toBeChecked()
    expect(latest().business_key).toBe(true)
    expect(latest().normalization?.text_linebreaks).toBeUndefined()
    expect(initial.fields[0].normalization.currency_equivalences).toEqual({ EUR: ['euro'] })
  })
})
