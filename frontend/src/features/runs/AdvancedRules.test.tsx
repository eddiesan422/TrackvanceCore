import { useState } from 'react'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { ComparisonBuilder, KeyNormalizationPreview, noNormalization, NormalizationFields, RuleBuilder, TransformBuilder } from './RuleBuilder'
import type { Comparison, Normalization, Rule, Transform } from './RuleBuilder'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn() }))
const columns = [{ name: 'country', logical_type: 'STRING', numeric: false }, { name: 'department', logical_type: 'STRING', numeric: false }, { name: 'amount', logical_type: 'DECIMAL', numeric: true }, { name: 'limit', logical_type: 'DECIMAL', numeric: true }, { name: 'transaction_date', logical_type: 'DATE', numeric: false }]
const transformRows = [{ amount: ' 1.234,50 ', transaction_date: '31/12/2026' }, { amount: 'sin importe', transaction_date: '31/02/2026' }]
function Harness({ kind = 'rules', sampleState = 'ready' }: { kind?: string; sampleState?: 'loading' | 'error' | 'ready' }) {
  const [rules, setRules] = useState<Rule[]>([]), [transforms, setTransforms] = useState<Transform[]>([]), [comparisons, setComparisons] = useState<Comparison[]>([])
  return <>{kind === 'rules' ? <RuleBuilder value={rules} onChange={setRules} columns={columns}/> : kind === 'transforms' ? <TransformBuilder value={transforms} onChange={setTransforms} columns={columns} rows={transformRows} sampleState={sampleState}/> : <ComparisonBuilder value={comparisons} onChange={setComparisons} sourceColumns={columns} targetColumns={columns}/>}<output data-testid="payload">{JSON.stringify(kind === 'rules' ? rules : kind === 'transforms' ? transforms : comparisons)}</output></>
}
function KeyHarness() {
  const [normalization, setNormalization] = useState<Normalization>(noNormalization)
  return <><NormalizationFields value={normalization} onChange={setNormalization}/><KeyNormalizationPreview value={normalization} keys={['country', 'department']} sourceRows={[{ country: ' co ', department: ' Antioquia ' }]} targetRows={[{ country: 'CO', department: 'ANTIOQUIA' }]}/><output data-testid="normalization">{JSON.stringify(normalization)}</output></>
}
const payload = () => JSON.parse(screen.getByTestId('payload').textContent || '[]')
async function chooseColumns(user: ReturnType<typeof userEvent.setup>, label: string, names: string[]) {
  await user.click(screen.getByRole('button', { name: `${label}: abrir selector` }))
  const group = screen.getByRole('group', { name: `Opciones de ${label}` })
  for (const name of names) await user.click(within(group).getByRole('checkbox', { name: new RegExp(`^${name} `) }))
  await user.click(screen.getByRole('button', { name: `${label}: cerrar selector` }))
}

describe('Advanced schema-aware rules', () => {
  beforeEach(() => {
    vi.mocked(api).mockImplementation(async path => path === '/datasets' ? { items: [{ id: 'reference', name: 'Countries' }], total: 1 } : path === '/datasets/reference' ? { id: 'reference', name: 'Countries', versions: [{ id: 'v1', version: 1, filename: 'countries.csv', schema: [columns[0]] }] } : { id: 'v1', dataset_id: 'reference', schema: [columns[0]] })
  })
  it('builds a conditional required rule with real columns and literal values', async () => {
    const user = userEvent.setup()
    renderApp(<Harness/>)
    await user.click(screen.getByRole('button', { name: 'Agregar regla' }))
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), 'required')
    await user.selectOptions(screen.getByLabelText('Columna de la regla'), 'department')
    expect(within(screen.getByLabelText('Columna de la regla')).queryByRole('option', { name: /unknown/ })).not.toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Aplicación de la regla'), 'CONDITIONAL')
    await user.selectOptions(screen.getByLabelText('Columna de la condición'), 'country')
    await user.type(screen.getByLabelText('Valor de la condición'), 'CO')
    expect(payload()[0]).toMatchObject({ type: 'required', column: 'department', when: { column: 'country', operator: 'eq', value: 'CO', logical_type: 'STRING' } })
    await user.selectOptions(screen.getByLabelText('Aplicación de la regla'), 'ALL')
    expect(payload()[0]).not.toHaveProperty('when')
  })
  it('selects a compound key and an immutable reference with matching columns', async () => {
    const user = userEvent.setup()
    renderApp(<Harness/>)
    await user.click(screen.getByRole('button', { name: 'Agregar regla' }))
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), 'compound_unique')
    await chooseColumns(user, 'Columnas de la regla', ['country', 'department'])
    expect(payload()[0].parameters.columns).toEqual(['country', 'department'])
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), 'reference')
    await chooseColumns(user, 'Columnas de la regla', ['country'])
    await user.selectOptions(await screen.findByLabelText('Dataset de referencia'), 'reference')
    await user.selectOptions(screen.getByLabelText('DatasetVersion de referencia'), 'v1')
    await chooseColumns(user, 'Columnas de referencia', ['country'])
    expect(payload()[0].parameters).toEqual({ columns: ['country'], reference_columns: ['country'], dataset_version_id: 'v1', null_policy: 'FAIL' })
    expect(api).not.toHaveBeenCalledWith(expect.stringContaining('preview'))
  })
  it('offers type, length and typed column comparisons', async () => {
    const user = userEvent.setup()
    renderApp(<Harness/>)
    await user.click(screen.getByRole('button', { name: 'Agregar regla' }))
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), 'type')
    await user.selectOptions(screen.getByLabelText('Tipo de dato requerido'), 'INT64')
    expect(payload()[0].parameters.logical_type).toBe('INT64')
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), 'length')
    await user.type(screen.getByLabelText('Longitud máxima'), '12')
    expect(payload()[0].parameters.max).toBe(12)
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), 'column_compare')
    await user.selectOptions(screen.getByLabelText('Columna de la regla'), 'amount')
    await user.selectOptions(screen.getByLabelText('Columna a comparar'), 'limit')
    await user.selectOptions(screen.getByLabelText('Operador entre columnas'), 'lte')
    expect(payload()[0]).toMatchObject({ column: 'amount', parameters: { other_column: 'limit', logical_type: 'DECIMAL', operator: 'lte' } })
  })
  it('uses functional transform labels, applies ordered previews and keeps parse failures visible', async () => {
    const user = userEvent.setup()
    renderApp(<Harness kind="transforms"/>)
    await user.click(screen.getByRole('button', { name: /Agregar transformación/ }))
    await user.selectOptions(screen.getByLabelText('Columna de transformación'), 'amount')
    expect(screen.getByText('Quita únicamente los espacios al inicio y al final del texto.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /Agregar transformación/ }))
    const cards = screen.getAllByRole('group', { name: /Transformación \d/ })
    const decimalCard = cards[1]
    await user.selectOptions(within(decimalCard).getByLabelText('Columna de transformación'), 'amount')
    const transformType = within(decimalCard).getByLabelText('Transformación')
    expect(within(transformType).getAllByRole('option').map(option => option.textContent)).toEqual([
      'Eliminar espacios externos', 'Convertir mayúsculas/minúsculas', 'Normalizar texto Unicode', 'Convertir texto vacío en nulo',
      'Completar identificador', 'Eliminar caracteres', 'Interpretar número decimal', 'Interpretar fecha',
    ])
    await user.selectOptions(transformType, 'decimal_parse')
    await user.clear(within(decimalCard).getByLabelText('Separador decimal'))
    await user.type(within(decimalCard).getByLabelText('Separador decimal'), ',')
    await user.clear(within(decimalCard).getByLabelText('Separador de miles'))
    await user.type(within(decimalCard).getByLabelText('Separador de miles'), '.')
    expect(payload()).toEqual([
      { column: 'amount', type: 'trim', parameters: {} },
      { column: 'amount', type: 'decimal_parse', parameters: { decimal_separator: ',', thousands_separator: '.' } },
    ])
    const preview = screen.getByRole('region', { name: 'Vista previa Antes / Después · Transformaciones previas' })
    const rows = within(preview).getAllByRole('row')
    expect(rows[1]).toHaveTextContent('" 1.234,50 "')
    expect(rows[1]).toHaveTextContent('"1234.50"')
    expect(rows[2]).toHaveTextContent('No se pudo interpretar; la transformación conservó el valor que recibió.')
    await user.click(screen.getByRole('button', { name: 'Mover transformación 2 hacia arriba' }))
    expect(payload().map((transform: Transform) => transform.type)).toEqual(['decimal_parse', 'trim'])
    expect(within(preview).getAllByRole('row')[1]).toHaveTextContent('"1.234,50"')
    expect(within(preview).getAllByRole('row')[1]).toHaveTextContent('No se pudo interpretar')
  })
  it('keeps invalid date values unchanged and identifies the parse error', async () => {
    const user = userEvent.setup()
    renderApp(<Harness kind="transforms"/>)
    await user.click(screen.getByRole('button', { name: /Agregar transformación/ }))
    await user.selectOptions(screen.getByLabelText('Columna de transformación'), 'transaction_date')
    await user.selectOptions(screen.getByLabelText('Transformación'), 'date_parse')
    await user.clear(screen.getByLabelText('Formatos de fecha'))
    await user.type(screen.getByLabelText('Formatos de fecha'), '%d/%m/%Y')
    const preview = screen.getByRole('region', { name: 'Vista previa Antes / Después · Transformaciones previas' })
    const rows = within(preview).getAllByRole('row')
    expect(rows[1]).toHaveTextContent('"2026-12-31"')
    expect(rows[2]).toHaveTextContent('"31/02/2026"')
    expect(rows[2]).toHaveTextContent('No se pudo interpretar; la transformación conservó el valor que recibió.')
    expect(payload()[0]).toEqual({ column: 'transaction_date', type: 'date_parse', parameters: { formats: ['%d/%m/%Y'] } })
  })
  it('does not misclassify a Python-only date format as a parse failure', async () => {
    const user = userEvent.setup()
    renderApp(<Harness kind="transforms"/>)
    await user.click(screen.getByRole('button', { name: /Agregar transformación/ }))
    await user.selectOptions(screen.getByLabelText('Columna de transformación'), 'transaction_date')
    await user.selectOptions(screen.getByLabelText('Transformación'), 'date_parse')
    await user.clear(screen.getByLabelText('Formatos de fecha'))
    await user.type(screen.getByLabelText('Formatos de fecha'), '%d-%b-%Y')

    const preview = screen.getByRole('region', { name: 'Vista previa Antes / Después · Transformaciones previas' })
    expect(within(preview).getAllByText(/Vista previa no disponible para este formato/)).toHaveLength(2)
    expect(within(preview).queryByText(/No se pudo interpretar/)).not.toBeInTheDocument()
    expect(payload()[0]).toEqual({ column: 'transaction_date', type: 'date_parse', parameters: { formats: ['%d-%b-%Y'] } })
  })
  it('distinguishes a sample loading failure from an empty dataset', async () => {
    const user = userEvent.setup()
    renderApp(<Harness kind="transforms" sampleState="error"/>)
    await user.click(screen.getByRole('button', { name: /Agregar transformación/ }))
    await user.selectOptions(screen.getByLabelText('Columna de transformación'), 'amount')
    expect(screen.getByText(/No se pudo cargar la muestra/)).toBeInTheDocument()
    expect(screen.queryByText(/no contiene registros de muestra/)).not.toBeInTheDocument()
  })
  it('explains key normalization and previews a composite key without changing internal values', async () => {
    const user = userEvent.setup()
    renderApp(<KeyHarness/>)
    expect(screen.getByLabelText('Espacios al inicio y al final')).toHaveDisplayValue('Conservar como están')
    expect(screen.getByRole('option', { name: 'Eliminar espacios externos' })).toHaveValue('TRIM')
    expect(screen.getByRole('option', { name: 'Normalización de compatibilidad (NFKC)' })).toHaveValue('NFKC')
    expect(screen.getByText(/Unifica caracteres que pueden verse iguales/)).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Espacios al inicio y al final'), 'TRIM')
    await user.selectOptions(screen.getByLabelText('Mayúsculas y minúsculas'), 'UPPER')
    expect(screen.getByText('Resultado → Coincidencia')).toBeInTheDocument()
    const preview = screen.getByRole('region', { name: 'Ejemplo y vista previa de normalización de claves' })
    expect(within(preview).getByText('{ country: " co " · department: " Antioquia " }')).toBeInTheDocument()
    expect(within(preview).getAllByText('{ country: "CO" · department: "ANTIOQUIA" }')).toHaveLength(3)
    expect(JSON.parse(screen.getByTestId('normalization').textContent || '{}')).toEqual({ trim: true, case: 'UPPER', unicode_normalization: 'NONE' })
  })
  it('declares null policies independently for each comparison', async () => {
    const user = userEvent.setup()
    renderApp(<Harness kind="comparisons"/>)
    await user.click(screen.getByRole('button', { name: 'Agregar comparación' }))
    await user.selectOptions(screen.getByLabelText('Columna de origen'), 'country')
    await user.selectOptions(screen.getByLabelText('Columna de destino'), 'country')
    await user.selectOptions(screen.getByLabelText('Política explícita de nulos'), 'INVALID')
    expect(payload()[0].parameters.null_policy).toBe('INVALID')
  })
})
