import { describe, expect, it } from 'vitest'
import { category, checkName, controlCounts, expectedValue, metricLabel, nullRate, observedValue, percentage, quantity } from './sentinelPresentation'

describe('Sentinel evidence presentation', () => {
  it('classifies actual outcomes and excludes observations even when their stored legacy status is PASS', () => {
    expect(controlCounts([{ status: 'PASS' }, { status: 'FAIL', severity: 'WARNING' }, { status: 'FAIL', severity: 'ERROR' },
      { status: 'UNKNOWN' }, { status: 'PASS', kind: 'OBSERVATION' }])).toEqual({ CONFORMING: 1, WARNING: 1, FAILED: 1, NOT_EVALUATED: 1, OBSERVATION: 1 })
    expect(category({ status: 'FAIL', actual: null })).toBe('FAILED')
    expect(category({ status: 'WARNING' })).toBe('WARNING')
  })
  it('uses null count and the version population and never manufactures an empty denominator ratio', () => {
    expect(nullRate({ null_count: 1, null_denominator: 4 })).toBe(0.25)
    expect(nullRate({ null_count: 0, null_denominator: 0 })).toBeNull()
    expect(nullRate({ null_count: 5, null_denominator: 4 })).toBeNull()
    expect(nullRate({ null_count: null, null_denominator: 4 })).toBeNull()
    expect(percentage(null)).toBe('No disponible')
    expect(percentage(0)).toBe('0 %')
    expect(quantity(null)).toBe('No disponible')
    expect(quantity('9007199254740993')).toBe('9.007.199.254.740.993')
  })
  it('formats units, declared bounds, and row rule counts without treating failures as disjoint rows', () => {
    expect(observedValue({ code: 'FRESHNESS', actual: 62.13 })).toBe('62,13 horas')
    expect(expectedValue({ code: 'FRESHNESS', expected: { max_hours: 48 } })).toBe('Máximo: 48 horas')
    expect(observedValue({ code: 'NULL_RATE_EMAIL', actual: 0.125 })).toBe('12,5 %')
    expect(expectedValue({ metric_key: 'null_rate:email', expected: { operator: 'lte', threshold: '0.05' } })).toBe('Máximo: 5 %')
    expect(expectedValue({ code: 'VOLUME_CHANGE', expected: { max_pct: 15, baseline_rows: 100000 } })).toBe('Máximo: 15 % · Base: 100.000 filas')
    expect(observedValue({ failed_count: 3, actual: 3 })).toBe('3 filas con incumplimiento')
    expect(expectedValue({ expected: { max_failed: 0, null_policy: 'FAIL' } })).toBe('Máximo: 0 filas con incumplimiento')
  })
  it('keeps unrecognized structured evidence out of the functional labels and distinguishes declared uniqueness', () => {
    expect(observedValue({ actual: { private_internal_key: 'value' } })).toBe('Consultar el detalle de la evidencia')
    expect(expectedValue({ expected: { logical_type: 'STRING' } })).toBe('Consultar la condición registrada en el detalle técnico')
    expect(checkName({ rule_type: 'unique', code: 'R1', column: 'id' })).toBe('Unicidad declarada · id')
    expect(metricLabel('uniqueness_ratio:category')).toBe('Valores de aparición única · category')
  })
})
