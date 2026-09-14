import { useState } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { ComparisonBuilder, NormalizationFields, noNormalization, RuleBuilder } from './RuleBuilder'
import type { Comparison, Normalization, Rule } from './RuleBuilder'

function RulesHarness({ sentinel = false }: { sentinel?: boolean }) {
  const [rules, setRules] = useState<Rule[]>([])
  return <><RuleBuilder value={rules} onChange={setRules} sentinel={sentinel}/><output data-testid="payload">{JSON.stringify(rules)}</output></>
}
function ComparisonsHarness() {
  const [rules, setRules] = useState<Comparison[]>([])
  return <><ComparisonBuilder value={rules} onChange={setRules}/><output data-testid="payload">{JSON.stringify(rules)}</output></>
}
function NormalizationHarness() {
  const [normalization, setNormalization] = useState<Normalization>({ ...noNormalization })
  return <><NormalizationFields value={normalization} onChange={setNormalization}/><output data-testid="payload">{JSON.stringify(normalization)}</output></>
}
const payload = () => JSON.parse(screen.getByTestId('payload').textContent || 'null')

describe('Declarative rule builders', () => {
  it('declares not_future and ISO date bounds without implicit transforms', async () => {
    const user = userEvent.setup()
    render(<RulesHarness/>)
    await user.click(screen.getByRole('button', { name: 'Agregar regla' }))
    await user.type(screen.getByLabelText('Columna de la regla'), 'transaction_date')
    await user.type(screen.getByLabelText('Fecha mínima'), '2020-01-01')
    await user.selectOptions(screen.getByLabelText('Política de valores nulos'), 'FAIL')
    expect(payload()).toEqual([{ type: 'date_rule', column: 'transaction_date', severity: 'ERROR', parameters: { not_future: true, timezone: 'UTC', null_policy: 'FAIL', min: '2020-01-01' } }])
  })

  it('preserves whitespace, case and Unicode in allowed values', async () => {
    const user = userEvent.setup()
    render(<RulesHarness/>)
    await user.click(screen.getByRole('button', { name: 'Agregar regla' }))
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), 'allowed_values')
    await user.type(screen.getByLabelText('Valores permitidos'), 'Cliente 12\n  Cliente 12  \nCLIENTE 12\nBogotá')
    expect(payload()[0].parameters.values).toEqual(['Cliente 12', '  Cliente 12  ', 'CLIENTE 12', 'Bogotá'])
    expect(screen.queryByRole('option', { name: 'Banda histórica (mediana + IQR)' })).not.toBeInTheDocument()
  })

  it('edits numeric range and portable regex settings', async () => {
    const user = userEvent.setup()
    render(<RulesHarness/>)
    await user.click(screen.getByRole('button', { name: 'Agregar regla' }))
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), 'range')
    await user.type(screen.getByLabelText('Máximo inclusivo'), '100.5')
    expect(payload()[0].parameters).toMatchObject({ gte: '0', lte: '100.5' })
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), 'regex')
    await user.click(screen.getByLabelText('Patrón regex'))
    await user.paste('^CO-[0-9]+$')
    await user.selectOptions(screen.getByLabelText('Opciones del patrón'), 'i')
    expect(payload()[0].parameters).toEqual({ pattern: '^CO-[0-9]+$', flags: 'i', null_policy: 'ALLOW' })
    await user.click(screen.getByRole('button', { name: 'Quitar regla 1' }))
    expect(payload()).toEqual([])
  })

  it.each(['distinct_count', 'distinct_rate', 'uniqueness_ratio', 'schema_type', 'historical_band'])('provides Sentinel %s controls', async type => {
    const user = userEvent.setup()
    render(<RulesHarness sentinel/>)
    await user.click(screen.getByRole('button', { name: 'Agregar regla' }))
    await user.selectOptions(screen.getByLabelText('Tipo de regla'), type)
    expect(payload()[0].type).toBe(type)
    if (type === 'historical_band') {
      expect(payload()[0].parameters).toEqual({ metric: 'row_count', window: 10, min_history: 4, iqr_multiplier: '1.5' })
      expect(screen.queryByLabelText('Columna de la regla')).not.toBeInTheDocument()
    } else if (type === 'schema_type') {
      await user.selectOptions(screen.getByLabelText('Tipo lógico esperado'), 'DATE')
      expect(payload()[0].parameters.expected_type).toBe('DATE')
    } else {
      expect(screen.getByLabelText('Columna de la regla')).toBeRequired()
    }
  })

  it('makes all key normalization choices explicit', async () => {
    const user = userEvent.setup()
    render(<NormalizationHarness/>)
    expect(payload()).toEqual({ trim: false, case: 'NONE', unicode_normalization: 'NONE' })
    await user.selectOptions(screen.getByLabelText('Claves: espacios externos'), 'TRIM')
    await user.selectOptions(screen.getByLabelText('Claves: mayúsculas y minúsculas'), 'UPPER')
    await user.selectOptions(screen.getByLabelText('Claves: Unicode'), 'NFC')
    expect(payload()).toEqual({ trim: true, case: 'UPPER', unicode_normalization: 'NFC' })
  })

  it('documents empty text and explicit null equality without changing normalization', async () => {
    const user = userEvent.setup()
    render(<ComparisonsHarness/>)
    await user.click(screen.getByRole('button', { name: 'Agregar comparación' }))
    expect(screen.getByText(/El texto vacío/)).toHaveTextContent('no equivale a null')
    expect(screen.getByRole('option', { name: 'Reportar diferencia' })).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText('Dos valores nulos'), 'EQUAL')
    expect(payload()[0].parameters).toEqual({ equal_nulls: true, normalization: noNormalization })
    await user.selectOptions(screen.getByLabelText('Comparación 1: espacios externos'), 'TRIM')
    expect(payload()[0].parameters.normalization).toEqual({ ...noNormalization, trim: true })
  })

  it('supports multiple exact, percentage and date comparisons with zero denominator policy', async () => {
    const user = userEvent.setup()
    render(<ComparisonsHarness/>)
    await user.click(screen.getByRole('button', { name: 'Agregar comparación' }))
    expect(payload()[0].parameters.normalization).toEqual(noNormalization)
    await user.click(screen.getByRole('button', { name: 'Agregar comparación' }))
    await user.selectOptions(screen.getAllByLabelText('Tipo de comparación')[1], 'numeric_tolerance')
    await user.type(screen.getByLabelText('Tolerancia porcentual (%)'), '2.5')
    await user.selectOptions(screen.getByLabelText('Base del porcentaje'), 'TARGET')
    expect(payload()[1].parameters).toEqual({ abs: '0.01', percent: '2.5', denominator: 'TARGET', zero_denominator: 'EXACT_ONLY', equal_nulls: false })
    await user.click(screen.getByRole('button', { name: 'Agregar comparación' }))
    await user.selectOptions(screen.getAllByLabelText('Tipo de comparación')[2], 'date_tolerance')
    await user.clear(screen.getByLabelText('Tolerancia de fecha (días)'))
    await user.type(screen.getByLabelText('Tolerancia de fecha (días)'), '2')
    expect(payload()[2].parameters).toEqual({ days: '2', timezone: 'UTC', equal_nulls: false })
  })
})
