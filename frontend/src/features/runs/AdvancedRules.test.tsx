import { useState } from 'react'
import { screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../api/client'
import { renderApp } from '../../test/render'
import { ComparisonBuilder, RuleBuilder, TransformBuilder } from './RuleBuilder'
import type { Comparison, Rule, Transform } from './RuleBuilder'

vi.mock('../../api/client', async original => ({ ...await original<typeof import('../../api/client')>(), api: vi.fn() }))
const columns = [{ name: 'country', logical_type: 'STRING', numeric: false }, { name: 'department', logical_type: 'STRING', numeric: false }, { name: 'amount', logical_type: 'DECIMAL', numeric: true }, { name: 'limit', logical_type: 'DECIMAL', numeric: true }]
function Harness({ kind = 'rules' }: { kind?: string }) {
  const [rules, setRules] = useState<Rule[]>([]), [transforms, setTransforms] = useState<Transform[]>([]), [comparisons, setComparisons] = useState<Comparison[]>([])
  return <>{kind === 'rules' ? <RuleBuilder value={rules} onChange={setRules} columns={columns}/> : kind === 'transforms' ? <TransformBuilder value={transforms} onChange={setTransforms} columns={columns}/> : <ComparisonBuilder value={comparisons} onChange={setComparisons} sourceColumns={columns} targetColumns={columns}/>}<output data-testid="payload">{JSON.stringify(kind === 'rules' ? rules : kind === 'transforms' ? transforms : comparisons)}</output></>
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
  it('exposes declared ordered transforms and preserves explicit separators', async () => {
    const user = userEvent.setup()
    renderApp(<Harness kind="transforms"/>)
    await user.click(screen.getByRole('button', { name: /Agregar transformación/ }))
    await user.selectOptions(screen.getByLabelText('Columna de transformación'), 'amount')
    await user.selectOptions(screen.getByLabelText('Transformación'), 'decimal_parse')
    await user.clear(screen.getByLabelText('Parámetro decimal_separator'))
    await user.type(screen.getByLabelText('Parámetro decimal_separator'), ',')
    await user.clear(screen.getByLabelText('Parámetro thousands_separator'))
    await user.type(screen.getByLabelText('Parámetro thousands_separator'), '.')
    expect(payload()).toEqual([{ column: 'amount', type: 'decimal_parse', parameters: { decimal_separator: ',', thousands_separator: '.' } }])
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
