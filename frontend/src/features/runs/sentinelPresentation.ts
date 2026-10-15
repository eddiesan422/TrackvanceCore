import type { RecordData } from '../../api/client'

export type ControlCategory = 'CONFORMING' | 'WARNING' | 'FAILED' | 'NOT_EVALUATED' | 'OBSERVATION'
export const controlLabels: Record<ControlCategory, string> = { CONFORMING: 'Conforme', WARNING: 'Advertencia', FAILED: 'Incumplido', NOT_EVALUATED: 'No evaluado', OBSERVATION: 'Informativo' }
export const controlTones: Record<ControlCategory, string> = { CONFORMING: 'green', WARNING: 'amber', FAILED: 'red', NOT_EVALUATED: 'neutral', OBSERVATION: 'neutral' }
export const exactValue = (value: unknown) => value == null ? 'No disponible' : typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)
export function finiteValue(value: unknown): number | null {
  if (value == null || value === '' || typeof value === 'boolean' || typeof value === 'object') return null
  const result = Number(value)
  return Number.isFinite(result) ? result : null
}
export function quantity(value: unknown, unit = ''): string {
  const numeric = finiteValue(value)
  if (numeric == null) return 'No disponible'
  const amount = typeof value === 'string' && /^-?\d+$/.test(value) ? BigInt(value) : numeric
  const singular: Record<string, string> = { filas: 'fila', columnas: 'columna', horas: 'hora', nulos: 'nulo', controles: 'control', valores: 'valor', 'valores distintos': 'valor distinto', 'valores sin nulos': 'valor sin nulos' }
  const suffix = numeric === 1 ? singular[unit] || unit : unit
  return `${new Intl.NumberFormat('es-CO', { maximumFractionDigits: 2 }).format(amount)}${suffix ? ` ${suffix}` : ''}`
}
export function percentage(value: unknown): string {
  const numeric = finiteValue(value)
  return numeric == null ? 'No disponible' : `${quantity(numeric * 100)} %`
}
export function category(check: RecordData): ControlCategory {
  if (check.kind === 'OBSERVATION') return 'OBSERVATION'
  if (check.status === 'PASS') return 'CONFORMING'
  if (['WARNING', 'WARN'].includes(check.status) || check.status === 'FAIL' && check.severity === 'WARNING') return 'WARNING'
  if (check.status === 'FAIL') return 'FAILED'
  return 'NOT_EVALUATED'
}
export function controlCounts(checks: RecordData[]) {
  const result = { CONFORMING: 0, WARNING: 0, FAILED: 0, NOT_EVALUATED: 0, OBSERVATION: 0 }
  checks.forEach(check => { result[category(check)] += 1 })
  return result
}
const metricLabels: Record<string, string> = { row_count: 'Filas', null_count: 'Nulos', null_rate: 'Proporción de nulos', distinct_count: 'Valores distintos', distinct_rate: 'Diversidad sobre valores sin nulos', uniqueness_ratio: 'Valores de aparición única', duplicate_row_count: 'Filas duplicadas exactas', distinct_row_count: 'Filas distintas' }
const typeLabels: Record<string, string> = { STRING: 'Texto', INT64: 'Entero', INTEGER: 'Entero', DECIMAL: 'Decimal', FLOAT: 'Decimal', DATE: 'Fecha', TIMESTAMP: 'Fecha y hora', BOOLEAN: 'Booleano' }
export const logicalType = (value: unknown) => typeLabels[String(value)] || 'Tipo registrado'
export function metricLabel(key: string, dimensions: RecordData = {}): string {
  const [metric, ...rest] = key.split(':')
  const column = dimensions.column || rest.join(':')
  return `${metricLabels[metric] || 'Medición registrada'}${column ? ` · ${column}` : ''}`
}
export function metricValue(key: string, value: unknown): string {
  const metric = key.split(':')[0]
  return ['null_rate', 'distinct_rate', 'uniqueness_ratio'].includes(metric) ? percentage(value) : quantity(value, metric.includes('row') ? 'filas' : 'valores')
}
export function nullRate(column: RecordData): number | null {
  const denominator = finiteValue(column.null_denominator), count = finiteValue(column.null_count)
  return denominator != null && denominator > 0 && count != null && count >= 0 && count <= denominator ? count / denominator : null
}
function checkMetric(check: RecordData): string {
  return check.metric_key?.split(':')[0] || (check.code?.startsWith('NULL_RATE_') ? 'null_rate' : '')
}
function checkValue(check: RecordData, value: unknown): string {
  const metric = checkMetric(check)
  if (['null_rate', 'distinct_rate', 'uniqueness_ratio'].includes(metric)) return percentage(value)
  if (check.code === 'FRESHNESS') return quantity(value, 'horas')
  if (check.code === 'VOLUME_CHANGE') return `${quantity(value)} %`
  if (['VOLUME_MIN', 'BASE_DUPLICATE_ROWS'].includes(check.code) || metric === 'row_count') return quantity(value, 'filas')
  if (check.failed_count != null) return `${quantity(value, 'filas')} con incumplimiento`
  if (metric === 'distinct_count' || check.code === 'BASE_DIVERSITY') return quantity(value, 'valores distintos')
  if (check.code === 'BASE_NULLS') return quantity(value, 'nulos')
  return quantity(value)
}
export function observedValue(check: RecordData): string {
  const value = check.actual
  if (value == null) return 'No disponible'
  if (check.code === 'SCHEMA_REQUIRED' && Array.isArray(value)) return value.length ? `Columnas ausentes: ${value.join(', ')}` : 'Todas las columnas requeridas están presentes'
  if (['BASE_SCHEMA_CHANGE', 'BASE_SCHEMA_BASELINE'].includes(check.code) && Array.isArray(value)) return `${quantity(value.length)} columnas registradas`
  if (typeof value === 'number' || typeof value === 'string' && /^-?\d+(\.\d+)?$/.test(value)) return checkValue(check, value)
  if (typeof value === 'boolean') return value ? 'Sí' : 'No'
  if (typeof value === 'string') return typeLabels[value] || value
  return 'Consultar el detalle de la evidencia'
}
const operators: Record<string, string> = { eq: 'Igual a', gt: 'Mayor que', gte: 'Mínimo', lt: 'Menor que', lte: 'Máximo', ne: 'Distinto de' }
export function expectedValue(check: RecordData): string {
  const value = check.expected
  if (value == null) return 'No disponible'
  if (check.code === 'SCHEMA_REQUIRED') return 'Todas las columnas requeridas'
  if (check.code === 'BASE_SCHEMA_CHANGE') return 'Conservar nombres, orden y tipos anteriores'
  if (typeof value === 'string') return typeLabels[value] || value
  if (typeof value !== 'object') return observedValue({ ...check, actual: value })
  if ('max_hours' in value) return `Máximo: ${quantity(value.max_hours, 'horas')}`
  if ('max_pct' in value) return `Máximo: ${quantity(value.max_pct)} %${value.baseline_rows == null ? '' : ` · Base: ${quantity(value.baseline_rows, 'filas')}`}`
  if ('max_failed' in value) return `Máximo: ${quantity(value.max_failed, 'filas')} con incumplimiento`
  if ('operator' in value && 'threshold' in value) return `${operators[value.operator] || 'Umbral'}: ${checkValue(check, value.threshold)}`
  const bounds = ['min', 'max'].filter(bound => bound in value).map(bound => `${bound === 'min' ? 'Mínimo' : 'Máximo'}: ${checkValue(check, value[bound])}`)
  if (bounds.length) return bounds.join(' · ')
  if (value.expected_type) return `Tipo: ${logicalType(value.expected_type)}`
  return 'Consultar la condición registrada en el detalle técnico'
}
export function checkName(check: RecordData): string {
  const names: Record<string, string> = { required: 'Dato obligatorio', not_null: 'Valor no nulo', unique: 'Unicidad declarada', compound_unique: 'Unicidad de clave compuesta', numeric: 'Valor numérico', positive: 'Valor positivo', type: 'Tipo de dato', range: 'Rango permitido', allowed_values: 'Valores permitidos', regex: 'Patrón de texto', date_rule: 'Regla de fecha', length: 'Longitud', column_compare: 'Comparación de columnas', reference: 'Referencia', metric_threshold: 'Umbral de métrica', schema_type: 'Tipo de columna', historical_band: 'Banda histórica compatible', distinct_count: 'Diversidad de valores', distinct_rate: 'Diversidad proporcional', uniqueness_ratio: 'Valores de aparición única' }
  const [prefix, ...nameColumn] = String(check.name || '').split(' · ')
  const type = check.rule_type || prefix.toLowerCase()
  const column = check.column || nameColumn.join(' · ')
  return names[type] ? `${names[type]}${column ? ` · ${column}` : ''}` : check.name || 'Control registrado'
}
export const profileMethod = (method: unknown) => method === 'EXACT_OBSERVED' ? 'Medición exacta' : method === 'LEGACY_NORMALIZED' ? 'Medición histórica normalizada' : 'Método registrado'
