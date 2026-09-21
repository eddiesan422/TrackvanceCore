import { useState } from 'react'
import { ArrowDown, ArrowUp, Plus, Trash2 } from 'lucide-react'
import { Field, Notice } from '../../components/ui'
import { useQuery } from '@tanstack/react-query'
import { api } from '../../api/client'
import type { Collection, RecordData } from '../../api/client'
import { ColumnMultiSelect } from './ColumnMultiSelect'
import type { DatasetColumn } from './ColumnMultiSelect'

export type Parameters = Record<string, string | number | boolean | string[] | null | Normalization>
export interface Normalization { trim: boolean; case: 'NONE' | 'UPPER' | 'LOWER'; unicode_normalization: 'NONE' | 'NFC' | 'NFKC' }
export interface Condition { column: string; operator: string; value?: string; logical_type?: string }
export interface Rule { type: string; rule_id?: string; code?: string; column?: string; severity?: 'ERROR' | 'WARNING'; parameters: Parameters; when?: Condition }
export interface Transform { type: string; column: string; parameters: Parameters }
export interface Comparison { type: string; source_column: string; target_column: string; parameters: Parameters }
export const noNormalization: Normalization = { trim: false, case: 'NONE', unicode_normalization: 'NONE' }
export type SampleRow = Record<string, unknown>
type SampleState = 'loading' | 'error' | 'ready'
const names: Record<string, string> = { required: 'Obligatorio', not_null: 'No nulo', unique: 'Unicidad', compound_unique: 'Unicidad compuesta', numeric: 'Numérico', positive: 'Positivo', type: 'Tipo de dato', length: 'Longitud del texto', column_compare: 'Comparación entre columnas', reference: 'Integridad referencial', range: 'Rango numérico', allowed_values: 'Valores permitidos', regex: 'Patrón de texto', date_rule: 'Regla de fecha', distinct_count: 'Cantidad de valores distintos', distinct_rate: 'Proporción de valores distintos', uniqueness_ratio: 'Proporción de registros únicos', schema_type: 'Tipo esperado de columna', metric_threshold: 'Umbral de métrica', historical_band: 'Banda histórica (mediana + IQR)' }
const defaults: Record<string, Parameters> = { range: { gte: '0', null_policy: 'ALLOW' }, allowed_values: { values: [], null_policy: 'ALLOW' }, regex: { pattern: '', flags: '', null_policy: 'ALLOW' }, date_rule: { not_future: true, timezone: 'UTC', null_policy: 'ALLOW' }, distinct_count: { min: '1' }, distinct_rate: { min: '0.9' }, uniqueness_ratio: { min: '1' }, schema_type: { expected_type: 'STRING' }, historical_band: { metric: 'row_count', window: 10, min_history: 4, iqr_multiplier: '1.5' } }
Object.assign(defaults, { required: {}, not_null: {}, unique: { null_policy: 'ALLOW' }, compound_unique: { columns: [], null_policy: 'FAIL' }, numeric: { null_policy: 'FAIL' }, positive: { null_policy: 'FAIL' }, type: { logical_type: 'STRING', null_policy: 'FAIL' }, length: { min: 1, null_policy: 'ALLOW' }, column_compare: { other_column: '', operator: 'eq', logical_type: 'DECIMAL', null_policy: 'FAIL' }, reference: { columns: [], reference_columns: [], dataset_version_id: '', null_policy: 'FAIL' }, metric_threshold: { metric: 'row_count', operator: 'gte', threshold: '1' } })
const rowTypes = ['required', 'not_null', 'unique', 'compound_unique', 'numeric', 'positive', 'type', 'length', 'column_compare', 'reference', 'range', 'allowed_values', 'regex', 'date_rule']
const metricNames = { row_count: 'Cantidad de filas', null_rate: 'Proporción de nulos', distinct_count: 'Valores distintos', distinct_rate: 'Proporción de distintos', uniqueness_ratio: 'Proporción de únicos' }

export function ColumnPicker({ value, onChange, columns, ...accessibility }: { value: string; onChange: (value: string) => void; columns?: DatasetColumn[]; id?: string; 'aria-describedby'?: string }) {
  return columns ? <select {...accessibility} required value={value} onChange={e => onChange(e.target.value)}><option value="">Selecciona una columna</option>{value && !columns.some(c => c.name === value) && <option value={value} disabled>{value} · No disponible en el esquema</option>}{columns.map(c => <option value={c.name} key={c.name}>{c.name} · {c.logical_type}</option>)}</select> : <input {...accessibility} required value={value} onChange={e => onChange(e.target.value)}/>
}

function ReferenceFields({ parameters, onChange }: { parameters: Parameters; onChange: (parameters: Parameters) => void }) {
  const [selectedDataset, setSelectedDataset] = useState('')
  const datasets = useQuery({ queryKey: ['datasets'], queryFn: () => api<Collection>('/datasets') })
  const selectedVersion = String(parameters.dataset_version_id || '')
  const currentVersion = useQuery({ queryKey: ['reference-version', selectedVersion], queryFn: () => api(`/dataset-versions/${selectedVersion}/profile`), enabled: !!selectedVersion })
  const datasetId = selectedDataset || currentVersion.data?.dataset_id || ''
  const versions = useQuery({ queryKey: ['dataset', datasetId], queryFn: () => api(`/datasets/${datasetId}`), enabled: !!datasetId })
  const selected = versions.data?.versions?.find((v: RecordData) => v.id === selectedVersion) || currentVersion.data
  return <><Field label="Dataset de referencia"><select required value={datasetId} onChange={e => { setSelectedDataset(e.target.value); onChange({ ...parameters, dataset_version_id: '', reference_columns: [] }) }}><option value="">Selecciona un dataset</option>{datasets.data?.items.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}</select></Field><Field label="DatasetVersion de referencia" hint="Se fija esta versión exacta; una carga posterior no cambia la regla."><select required value={selectedVersion} onChange={e => onChange({ ...parameters, dataset_version_id: e.target.value, reference_columns: [] })}><option value="">Selecciona una versión</option>{selectedVersion && !versions.data?.versions?.some((v: RecordData) => v.id === selectedVersion) && <option value={selectedVersion}>{selectedVersion}</option>}{versions.data?.versions?.map((v: RecordData) => <option key={v.id} value={v.id}>Versión {v.version} · {v.filename}</option>)}</select></Field>{(datasets.error || versions.error || currentVersion.error) && <Notice>No se pudo cargar la referencia. <button type="button" onClick={() => { datasets.refetch(); versions.refetch(); if (selectedVersion) currentVersion.refetch() }}>Reintentar</button></Notice>}<ColumnMultiSelect label="Columnas de referencia" hint="Selecciona en el mismo orden que las columnas de origen." columns={selected?.schema || []} selected={Array.isArray(parameters.reference_columns) ? parameters.reference_columns : []} onChange={reference_columns => onChange({ ...parameters, reference_columns })} disabled={!selected}/></>
}

export function NormalizationFields({ value, onChange, prefix = 'Claves' }: { value: Normalization; onChange: (value: Normalization) => void; prefix?: string }) {
  const functionalLabel = (label: string) => prefix === 'Claves' ? label : `${prefix}: ${label.toLowerCase()}`
  return <div className="form-grid normalization-fields">
    <Field label={functionalLabel('Espacios al inicio y al final')}><select value={value.trim ? 'TRIM' : 'NONE'} onChange={e => onChange({ ...value, trim: e.target.value === 'TRIM' })}><option value="NONE">Conservar como están</option><option value="TRIM">Eliminar espacios externos</option></select></Field>
    <Field label={functionalLabel('Mayúsculas y minúsculas')}><select value={value.case} onChange={e => onChange({ ...value, case: e.target.value as Normalization['case'] })}><option value="NONE">Conservar como están</option><option value="UPPER">Convertir a MAYÚSCULAS</option><option value="LOWER">Convertir a minúsculas</option></select></Field>
    <Field label={functionalLabel('Normalización de caracteres')} hint="Unifica caracteres que pueden verse iguales, pero tienen una representación interna diferente."><select value={value.unicode_normalization} onChange={e => onChange({ ...value, unicode_normalization: e.target.value as Normalization['unicode_normalization'] })}><option value="NONE">No normalizar</option><option value="NFC">Normalización estándar (NFC)</option><option value="NFKC">Normalización de compatibilidad (NFKC)</option></select></Field>
  </div>
}

export function RuleBuilder({ value, onChange, sentinel = false, columns }: { value: Rule[]; onChange: (rules: Rule[]) => void; sentinel?: boolean; columns?: DatasetColumn[] }) {
  const update = (index: number, changes: Partial<Rule>) => onChange(value.map((rule, i) => i === index ? { ...rule, ...changes } : rule))
  return <section className="rule-builder" aria-label="Reglas adicionales">
    <div className="builder-heading"><div><h3>Reglas adicionales</h3><p>Condiciones explícitas; los datos recibidos se conservan sin normalización silenciosa.</p></div><button type="button" className="button secondary small" onClick={() => onChange([...value, { type: 'date_rule', column: '', severity: 'ERROR', parameters: { ...defaults.date_rule } }])}><Plus size={14}/> Agregar regla</button></div>
    {value.map((rule, index) => {
      const p = rule.parameters
      const param = (key: string, val: Parameters[string] | undefined) => { const next = { ...p }; if (val === undefined || val === '') delete next[key]; else next[key] = val; update(index, { parameters: next }) }
      return <fieldset className="builder-card" key={index}><legend>Regla {index + 1}</legend><div className="form-grid">
        <Field label="Tipo de regla"><select value={rule.type} onChange={e => update(index, { type: e.target.value, code: e.target.value.toUpperCase(), parameters: { ...defaults[e.target.value] }, column: ['historical_band', 'metric_threshold', 'compound_unique', 'reference'].includes(e.target.value) ? undefined : rule.column, when: rowTypes.includes(e.target.value) ? rule.when : undefined })}>{Object.entries(names).filter(([key]) => sentinel || rowTypes.includes(key)).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></Field>
        {!['historical_band', 'metric_threshold', 'compound_unique', 'reference'].includes(rule.type) && <Field label="Columna de la regla"><ColumnPicker columns={columns} value={rule.column || ''} onChange={column => update(index, { column })}/></Field>}
        {['compound_unique', 'reference'].includes(rule.type) && <ColumnMultiSelect label="Columnas de la regla" hint="Selecciona las columnas en el orden de la clave compuesta." columns={columns || []} selected={Array.isArray(p.columns) ? p.columns : []} onChange={selected => param('columns', selected)}/>}
        <Field label="Severidad de la regla"><select value={rule.severity || 'ERROR'} onChange={e => update(index, { severity: e.target.value as Rule['severity'] })}><option value="ERROR">Error de negocio</option><option value="WARNING">Advertencia</option></select></Field>
        {rowTypes.includes(rule.type) && !['required', 'not_null'].includes(rule.type) && <Field label="Política de valores nulos"><select value={String(p.null_policy || 'ALLOW')} onChange={e => param('null_policy', e.target.value)}><option value="ALLOW">Permitir</option><option value="FAIL">Reportar incumplimiento</option><option value="IGNORE">Excluir de la evaluación</option></select></Field>}
        {rule.type === 'type' && <Field label="Tipo de dato requerido"><select value={String(p.logical_type)} onChange={e => param('logical_type', e.target.value)}>{['STRING', 'DECIMAL', 'INT64', 'DATE', 'TIMESTAMP', 'BOOLEAN'].map(t => <option key={t}>{t}</option>)}</select></Field>}
        {rule.type === 'length' && <>{['min', 'max'].map(key => <Field key={key} label={key === 'min' ? 'Longitud mínima' : 'Longitud máxima'} hint="Caracteres Unicode; límites inclusivos."><input type="number" min="0" max="1000000" step="1" value={String(p[key] ?? '')} onChange={e => param(key, e.target.value === '' ? undefined : Number(e.target.value))}/></Field>)}</>}
        {rule.type === 'column_compare' && <><Field label="Columna a comparar"><ColumnPicker columns={columns} value={String(p.other_column || '')} onChange={other => param('other_column', other)}/></Field><Field label="Operador entre columnas"><select value={String(p.operator)} onChange={e => param('operator', e.target.value)}>{['eq', 'ne', 'gt', 'gte', 'lt', 'lte'].map(op => <option key={op}>{op}</option>)}</select></Field><Field label="Semántica de comparación"><select value={String(p.logical_type)} onChange={e => param('logical_type', e.target.value)}>{['STRING', 'DECIMAL', 'DATE', 'TIMESTAMP'].map(t => <option key={t}>{t}</option>)}</select></Field></>}
        {rule.type === 'reference' && <ReferenceFields parameters={p} onChange={parameters => update(index, { parameters })}/>}
        {rule.type === 'range' && <>{(['gte', 'lte'] as const).map(key => <Field key={key} label={key === 'gte' ? 'Mínimo inclusivo' : 'Máximo inclusivo'}><input type="number" step="any" value={String(p[key] ?? '')} onChange={e => param(key, e.target.value)}/></Field>)}</>}
        {rule.type === 'allowed_values' && <Field label="Valores permitidos" hint="Un valor de texto por línea. Se respetan los espacios y las mayúsculas."><textarea required rows={3} value={Array.isArray(p.values) ? p.values.join('\n') : ''} onChange={e => param('values', e.target.value.split('\n'))}/></Field>}
        {rule.type === 'regex' && <><Field label="Patrón regex" hint="Expresión portable, sin código ejecutable."><input required value={String(p.pattern || '')} onChange={e => param('pattern', e.target.value)}/></Field><Field label="Opciones del patrón"><select value={String(p.flags || '')} onChange={e => param('flags', e.target.value)}><option value="">Distinguir mayúsculas</option><option value="i">Ignorar mayúsculas (i)</option></select></Field></>}
        {rule.type === 'date_rule' && <><Field label="Fechas futuras"><select value={p.not_future ? 'REJECT' : 'ALLOW'} onChange={e => param('not_future', e.target.value === 'REJECT')}><option value="REJECT">No permitir (not_future)</option><option value="ALLOW">Permitir</option></select></Field><Field label="Fecha mínima"><input type="date" value={String(p.min || '')} onChange={e => param('min', e.target.value)}/></Field><Field label="Fecha máxima"><input type="date" value={String(p.max || '')} onChange={e => param('max', e.target.value)}/></Field></>}
        {['distinct_count', 'distinct_rate', 'uniqueness_ratio'].includes(rule.type) && <>{['min', 'max'].map(key => <Field key={key} label={`${key === 'min' ? 'Mínimo' : 'Máximo'} observado${rule.type === 'distinct_count' ? '' : ' (0 a 1)'}`}><input type="number" min="0" max={rule.type === 'distinct_count' ? undefined : '1'} step="any" value={String(p[key] ?? '')} onChange={e => param(key, e.target.value)}/></Field>)}</>}
        {rule.type === 'schema_type' && <Field label="Tipo lógico esperado" hint="Puedes fijar un tipo o detectar cambios frente a la versión anterior del dataset."><select value={String(p.expected_type || 'PREVIOUS')} onChange={e => param('expected_type', e.target.value === 'PREVIOUS' ? undefined : e.target.value)}><option value="PREVIOUS">Conservar el tipo de la versión anterior</option>{['STRING', 'DECIMAL', 'INT64', 'DATE', 'TIMESTAMP', 'BOOLEAN'].map(type => <option key={type}>{type}</option>)}</select></Field>}
        {['historical_band', 'metric_threshold'].includes(rule.type) && <><Field label="Métrica histórica"><select value={String(p.metric || 'row_count')} onChange={e => update(index, { parameters: { ...p, metric: e.target.value }, column: e.target.value === 'row_count' ? undefined : rule.column })}>{Object.entries(metricNames).map(([key, name]) => <option key={key} value={key}>{name}</option>)}</select></Field>{p.metric !== 'row_count' && <Field label="Columna de la métrica"><ColumnPicker columns={columns} value={rule.column || ''} onChange={column => update(index, { column })}/></Field>}</>}
        {rule.type === 'metric_threshold' && <><Field label="Operador del umbral"><select value={String(p.operator)} onChange={e => param('operator', e.target.value)}>{['eq', 'gt', 'gte', 'lt', 'lte'].map(op => <option key={op}>{op}</option>)}</select></Field><Field label="Umbral"><input required type="number" step="any" value={String(p.threshold)} onChange={e => param('threshold', e.target.value)}/></Field></>}
        {rule.type === 'historical_band' && <><Field label="Ventana de ejecuciones"><input required type="number" min="2" max="1000" value={Number(p.window)} onChange={e => param('window', Number(e.target.value))}/></Field><Field label="Mínimo de observaciones"><input required type="number" min="2" max={Number(p.window)} value={Number(p.min_history)} onChange={e => param('min_history', Number(e.target.value))}/></Field><Field label="Multiplicador IQR"><input required type="number" min="0" step="any" value={String(p.iqr_multiplier)} onChange={e => param('iqr_multiplier', e.target.value)}/></Field></>}
        {rowTypes.includes(rule.type) && <><Field label="Aplicación de la regla"><select value={rule.when ? 'CONDITIONAL' : 'ALL'} onChange={e => update(index, { when: e.target.value === 'CONDITIONAL' ? { column: '', operator: 'eq', value: '', logical_type: 'STRING' } : undefined })}><option value="ALL">Todos los registros</option><option value="CONDITIONAL">Solo cuando se cumple una condición</option></select></Field>{rule.when && !('column' in rule.when) && <Notice>Condición compuesta conservada: {JSON.stringify(rule.when)}. Para sustituirla, selecciona Todos los registros y vuelve a activar una condición.</Notice>}{rule.when && 'column' in rule.when && <><Field label="Columna de la condición"><ColumnPicker columns={columns} value={rule.when.column} onChange={column => update(index, { when: { ...rule.when!, column } })}/></Field><Field label="Operador de la condición"><select value={rule.when.operator} onChange={e => update(index, { when: { ...rule.when!, operator: e.target.value } })}>{['eq', 'ne', 'gt', 'gte', 'lt', 'lte', 'is_null', 'not_null'].map(op => <option key={op}>{op}</option>)}</select></Field>{!['is_null', 'not_null'].includes(rule.when.operator) && <Field label="Valor de la condición"><input value={rule.when.value || ''} onChange={e => update(index, { when: { ...rule.when!, value: e.target.value } })}/></Field>}<Field label="Tipo de la condición"><select value={rule.when.logical_type || 'STRING'} onChange={e => update(index, { when: { ...rule.when!, logical_type: e.target.value } })}>{['STRING', 'DECIMAL', 'DATE', 'TIMESTAMP'].map(t => <option key={t}>{t}</option>)}</select></Field></>}</>}
      </div>{rule.type === 'date_rule' && <p className="muted">Fechas ISO YYYY-MM-DD. La fecha de evaluación se fija en UTC y queda en la evidencia.</p>}{rule.type === 'historical_band' && <p className="muted">Solo se comparan ejecuciones completadas con el mismo método y versión de la métrica.</p>}<button type="button" className="text-button" onClick={() => onChange(value.filter((_, i) => i !== index))}><Trash2 size={14}/> Quitar regla {index + 1}</button></fieldset>
    })}
  </section>
}

export function ComparisonBuilder({ value, onChange, sourceColumns, targetColumns }: { value: Comparison[]; onChange: (rules: Comparison[]) => void; sourceColumns?: DatasetColumn[]; targetColumns?: DatasetColumn[] }) {
  const update = (index: number, changes: Partial<Comparison>) => onChange(value.map((rule, i) => i === index ? { ...rule, ...changes } : rule))
  return <section className="rule-builder" aria-label="Comparaciones"><div className="builder-heading"><h3>Campos a comparar</h3><button type="button" className="button secondary small" onClick={() => onChange([...value, { type: 'exact_compare', source_column: '', target_column: '', parameters: { equal_nulls: false, normalization: { ...noNormalization } } }])}><Plus size={14}/> Agregar comparación</button></div>{value.map((rule, index) => {
    const p = rule.parameters, param = (key: string, val: Parameters[string]) => update(index, { parameters: { ...p, [key]: val } })
    return <fieldset className="builder-card" key={index}><legend>Comparación {index + 1}</legend><div className="form-grid"><Field label="Tipo de comparación"><select value={rule.type} onChange={e => update(index, { type: e.target.value, parameters: e.target.value === 'numeric_tolerance' ? { abs: '0.01', percent: null, denominator: 'SOURCE', zero_denominator: 'EXACT_ONLY', equal_nulls: false } : e.target.value === 'date_tolerance' ? { days: '0', timezone: 'UTC', equal_nulls: false } : { normalization: { ...noNormalization }, equal_nulls: false } })}><option value="exact_compare">Igualdad exacta · EXACT_COMPARE</option><option value="numeric_tolerance">Tolerancia numérica · NUMERIC_TOLERANCE</option><option value="date_tolerance">Tolerancia de fecha · DATE_TOLERANCE</option></select></Field><Field label="Columna de origen"><ColumnPicker columns={sourceColumns} value={rule.source_column} onChange={source_column => update(index, { source_column })}/></Field><Field label="Columna de destino"><ColumnPicker columns={targetColumns} value={rule.target_column} onChange={target_column => update(index, { target_column })}/></Field><Field label="Dos valores nulos"><select value={p.equal_nulls ? 'EQUAL' : 'INVALID'} onChange={e => param('equal_nulls', e.target.value === 'EQUAL')}><option value="INVALID">{rule.type === 'exact_compare' ? 'Reportar diferencia' : 'Reportar como inválidos'}</option><option value="EQUAL">Considerar iguales</option></select></Field><Field label="Política explícita de nulos"><select value={String(p.null_policy || 'LEGACY')} onChange={e => { const parameters = { ...p }; if (e.target.value === 'LEGACY') delete parameters.null_policy; else parameters.null_policy = e.target.value; update(index, { parameters }) }}><option value="LEGACY">Conservar política de la comparación</option><option value="MATCH_NULLS">Dos nulos coinciden; uno genera diferencia</option><option value="MISMATCH">Cualquier nulo genera diferencia</option><option value="INVALID">Cualquier nulo genera INVALID</option></select></Field>
      {rule.type === 'numeric_tolerance' && <><Field label="Tolerancia absoluta"><input required type="number" min="0" step="any" value={String(p.abs ?? '0')} onChange={e => param('abs', e.target.value)}/></Field><Field label="Tolerancia porcentual (%)" hint="Opcional. La base cero solo permite coincidencia exacta."><input type="number" min="0" step="any" value={String(p.percent ?? '')} onChange={e => param('percent', e.target.value || null)}/></Field><Field label="Base del porcentaje"><select value={String(p.denominator || 'SOURCE')} onChange={e => param('denominator', e.target.value)}><option value="SOURCE">Importe de origen</option><option value="TARGET">Importe de destino</option><option value="MAX_ABS">Mayor importe absoluto</option></select></Field></>}
      {rule.type === 'date_tolerance' && <Field label="Tolerancia de fecha (días)"><input required type="number" min="0" step="any" value={String(p.days ?? '0')} onChange={e => param('days', e.target.value)}/></Field>}
    </div>{rule.type === 'exact_compare' && <NormalizationFields prefix={`Comparación ${index + 1}`} value={(p.normalization as Normalization) || noNormalization} onChange={normalization => param('normalization', normalization)}/>}<button type="button" className="text-button" disabled={value.length === 1} onClick={() => onChange(value.filter((_, i) => i !== index))}><Trash2 size={14}/> Quitar comparación {index + 1}</button></fieldset>
  })}<Notice>El texto vacío ("") se conserva como texto: no equivale a null. La igualdad exacta aplica únicamente la normalización declarada. Cada comparación queda registrada en la versión del control y en su manifiesto. Las claves duplicadas requieren revisión, salvo que declares una agregación.</Notice></section>
}

const transformLabels: Record<string, string> = {
  trim: 'Eliminar espacios externos',
  case: 'Convertir mayúsculas/minúsculas',
  unicode_normalization: 'Normalizar texto Unicode',
  empty_to_null: 'Convertir texto vacío en nulo',
  id_padding: 'Completar identificador',
  remove_characters: 'Eliminar caracteres',
  decimal_parse: 'Interpretar número decimal',
  date_parse: 'Interpretar fecha',
}

const transformDefaults: Record<string, Parameters> = {
  trim: {},
  case: { case: 'UPPER' },
  unicode_normalization: { form: 'NFC' },
  empty_to_null: {},
  id_padding: { width: 8, fill: '0', side: 'left' },
  remove_characters: { characters: '-' },
  decimal_parse: { decimal_separator: '.', thousands_separator: ',' },
  date_parse: { formats: ['%Y-%m-%d'] },
}

const transformDescriptions: Record<string, string> = {
  trim: 'Quita únicamente los espacios al inicio y al final del texto.',
  case: 'Convierte todo el texto a una misma combinación de mayúsculas o minúsculas.',
  unicode_normalization: 'Unifica caracteres equivalentes que pueden tener representaciones internas diferentes.',
  empty_to_null: 'Convierte el texto de longitud cero en un valor nulo; no elimina espacios.',
  id_padding: 'Completa el identificador hasta la longitud indicada sin convertirlo en número.',
  remove_characters: 'Elimina del valor cada uno de los caracteres declarados.',
  decimal_parse: 'Interpreta los separadores declarados y produce un decimal canónico.',
  date_parse: 'Prueba los formatos declarados en orden y produce una fecha ISO.',
}

function textValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (typeof value === 'boolean') return value ? 'True' : 'False'
  if (value instanceof Date) return value.toISOString()
  if (typeof value === 'object' && value !== null) return JSON.stringify(value)
  return String(value)
}

function displayValue(value: unknown): string {
  if (value === null) return 'nulo'
  if (value === undefined) return '—'
  const text = textValue(value)
  return text === '' ? 'texto vacío ("")' : JSON.stringify(text)
}

function canonicalDecimal(value: string): string | null {
  if (!/^[+-]?[0-9]+(?:\.[0-9]+)?$/.test(value)) return null
  const sign = value.startsWith('-') ? '-' : ''
  const unsigned = value.replace(/^[+-]/, '')
  const [integer, fraction] = unsigned.split('.')
  const normalizedInteger = integer.replace(/^0+(?=\d)/, '')
  return `${sign}${normalizedInteger}${fraction === undefined ? '' : `.${fraction}`}`
}

type DatePart = 'year' | 'shortYear' | 'month' | 'day' | 'hour' | 'hour12' | 'minute' | 'second' | 'fraction' | 'period' | 'timezone'
type DatePreview = { kind: 'parsed'; value: string } | { kind: 'invalid' } | { kind: 'unsupported' }
const dateDirectives: Record<string, { pattern: string; part: DatePart }> = {
  Y: { pattern: '(\\d{4})', part: 'year' }, y: { pattern: '(\\d{2})', part: 'shortYear' },
  m: { pattern: '(\\d{1,2})', part: 'month' }, d: { pattern: '(\\d{1,2})', part: 'day' },
  H: { pattern: '(\\d{1,2})', part: 'hour' }, I: { pattern: '(\\d{1,2})', part: 'hour12' },
  M: { pattern: '(\\d{1,2})', part: 'minute' }, S: { pattern: '(\\d{1,2})', part: 'second' },
  f: { pattern: '(\\d{1,6})', part: 'fraction' }, p: { pattern: '(AM|PM|am|pm)', part: 'period' },
  z: { pattern: '(Z|[+-]\\d{2}:?\\d{2})', part: 'timezone' },
}

const escapePattern = (value: string) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')

function parseDate(value: string, format: string): DatePreview {
  let pattern = '^'
  const parts: DatePart[] = []
  for (let index = 0; index < format.length; index += 1) {
    if (format[index] !== '%') {
      pattern += /\s/.test(format[index]) ? '\\s+' : escapePattern(format[index])
      continue
    }
    const directive = format[index + 1]
    index += 1
    if (directive === '%') { pattern += '%'; continue }
    const definition = dateDirectives[directive]
    if (!definition) return { kind: 'unsupported' }
    pattern += definition.pattern
    parts.push(definition.part)
  }
  const match = new RegExp(`${pattern}$`).exec(value)
  if (!match) return { kind: 'invalid' }
  const parsed: Partial<Record<DatePart, string>> = {}
  parts.forEach((part, index) => { parsed[part] = match[index + 1] })
  const shortYear = Number(parsed.shortYear)
  const year = parsed.year ? Number(parsed.year) : parsed.shortYear ? (shortYear <= 68 ? 2000 + shortYear : 1900 + shortYear) : 1900
  const month = Number(parsed.month || 1), day = Number(parsed.day || 1)
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0)
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
  const hour = Number(parsed.hour || 0), hour12 = Number(parsed.hour12 || 1)
  const minute = Number(parsed.minute || 0), second = Number(parsed.second || 0)
  const offset = parsed.timezone
  const invalidOffset = offset && offset !== 'Z' && (() => {
    const compact = offset.replace(':', '').slice(1)
    return Number(compact.slice(0, 2)) > 23 || Number(compact.slice(2, 4)) > 59
  })()
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1] || hour > 23 || hour12 < 1 || hour12 > 12 || minute > 59 || second > 59 || invalidOffset) return { kind: 'invalid' }
  return { kind: 'parsed', value: `${String(year).padStart(4, '0')}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}` }
}

function applyTransform(value: unknown, transform: Transform): { value: unknown; error?: boolean; unsupported?: boolean } {
  if (value === null || value === undefined) return { value: null }
  const text = textValue(value), parameters = transform.parameters
  if (transform.type === 'trim') return { value: text.trim() }
  if (transform.type === 'empty_to_null') return { value: text === '' ? null : text }
  if (transform.type === 'case') return { value: parameters.case === 'LOWER' ? text.toLowerCase() : text.toUpperCase() }
  if (transform.type === 'unicode_normalization') return { value: text.normalize(String(parameters.form || 'NFC') as 'NFC' | 'NFKC') }
  if (transform.type === 'id_padding') {
    const width = Number(parameters.width), fill = String(parameters.fill || '0')
    const padding = fill.repeat(Math.max(0, width - Array.from(text).length))
    return { value: parameters.side === 'right' ? `${text}${padding}` : `${padding}${text}` }
  }
  if (transform.type === 'remove_characters') {
    const characters = new Set(Array.from(String(parameters.characters || '')))
    return { value: Array.from(text).filter(character => !characters.has(character)).join('') }
  }
  if (transform.type === 'decimal_parse') {
    const decimal = String(parameters.decimal_separator ?? '.'), thousands = String(parameters.thousands_separator ?? ',')
    const withoutThousands = thousands ? text.split(thousands).join('') : text
    const parsed = canonicalDecimal(withoutThousands.split(decimal).join('.'))
    return parsed === null ? { value: text, error: true } : { value: parsed }
  }
  if (transform.type === 'date_parse') {
    const formats = Array.isArray(parameters.formats) ? parameters.formats : []
    let unsupported = false
    for (const format of formats) {
      const parsed = parseDate(text, format)
      if (parsed.kind === 'parsed') return { value: parsed.value }
      if (parsed.kind === 'unsupported') unsupported = true
    }
    return unsupported ? { value: text, unsupported: true } : { value: text, error: true }
  }
  return { value: text }
}

function exampleInput(transform: Transform): string {
  const parameters = transform.parameters
  if (transform.type === 'trim') return ' Cliente 01 '
  if (transform.type === 'case') return 'Cliente 01'
  if (transform.type === 'unicode_normalization') return parameters.form === 'NFKC' ? 'Ｃｌｉｅｎｔｅ 01' : 'Cafe\u0301'
  if (transform.type === 'empty_to_null') return ''
  if (transform.type === 'id_padding') return '123'
  if (transform.type === 'remove_characters') return `AB${String(parameters.characters || '-')[0] || '-'}123`
  if (transform.type === 'decimal_parse') return `1${String(parameters.thousands_separator ?? ',')}234${String(parameters.decimal_separator ?? '.')}50`
  if (transform.type === 'date_parse') {
    const format = Array.isArray(parameters.formats) && parameters.formats[0] ? parameters.formats[0] : '%Y-%m-%d'
    return format.replace(/%Y/g, '2026').replace(/%y/g, '26').replace(/%m/g, '12').replace(/%d/g, '31').replace(/%H/g, '14').replace(/%I/g, '02').replace(/%M/g, '30').replace(/%S/g, '45').replace(/%f/g, '123456').replace(/%p/g, 'PM').replace(/%z/g, '+0000').replace(/%Z/g, 'UTC').replace(/%%/g, '%')
  }
  return 'Cliente 01'
}

function TransformExplanation({ transform }: { transform: Transform }) {
  const before = exampleInput(transform), result = applyTransform(before, transform)
  return <div className="transform-explanation"><p>{transformDescriptions[transform.type]}</p><div><span>Ejemplo</span><code>{displayValue(before)}</code><b>→</b><code>{result.unsupported ? 'Vista previa no disponible para este formato' : result.error ? 'No se pudo interpretar' : displayValue(result.value)}</code></div></div>
}

function TransformParameters({ transform, onChange }: { transform: Transform; onChange: (parameters: Parameters) => void }) {
  const parameters = transform.parameters
  const set = (key: string, value: Parameters[string]) => onChange({ ...parameters, [key]: value })
  if (transform.type === 'case') return <Field label="Resultado del texto"><select value={String(parameters.case || 'UPPER')} onChange={event => set('case', event.target.value)}><option value="UPPER">MAYÚSCULAS</option><option value="LOWER">minúsculas</option></select></Field>
  if (transform.type === 'unicode_normalization') return <Field label="Forma de normalización"><select value={String(parameters.form || 'NFC')} onChange={event => set('form', event.target.value)}><option value="NFC">Estándar (NFC)</option><option value="NFKC">Compatibilidad (NFKC)</option></select></Field>
  if (transform.type === 'id_padding') return <><Field label="Longitud total"><input required type="number" min="1" max="1000" value={Number(parameters.width)} onChange={event => set('width', Number(event.target.value))}/></Field><Field label="Carácter de relleno"><input required maxLength={1} value={String(parameters.fill || '')} onChange={event => set('fill', event.target.value)}/></Field><Field label="Posición del relleno"><select value={String(parameters.side || 'left')} onChange={event => set('side', event.target.value)}><option value="left">Antes del identificador</option><option value="right">Después del identificador</option></select></Field></>
  if (transform.type === 'remove_characters') return <Field label="Caracteres a eliminar"><input value={String(parameters.characters || '')} onChange={event => set('characters', event.target.value)}/></Field>
  if (transform.type === 'decimal_parse') return <><Field label="Separador decimal"><input required maxLength={1} value={String(parameters.decimal_separator ?? '.')} onChange={event => set('decimal_separator', event.target.value)}/></Field><Field label="Separador de miles"><input maxLength={1} value={String(parameters.thousands_separator ?? ',')} onChange={event => set('thousands_separator', event.target.value)}/></Field></>
  if (transform.type === 'date_parse') return <Field label="Formatos de fecha" hint="Un formato explícito por línea; se prueban de arriba hacia abajo. Ejemplo: %d/%m/%Y."><textarea required rows={3} value={Array.isArray(parameters.formats) ? parameters.formats.join('\n') : ''} onChange={event => set('formats', event.target.value.split('\n'))}/></Field>
  return null
}

function TransformPreview({ transforms, rows, sampleState, label }: { transforms: Transform[]; rows: SampleRow[]; sampleState: SampleState; label: string }) {
  const configured = transforms.filter(transform => transform.column)
  const selectedColumns = [...new Set(configured.map(transform => transform.column))]
  if (!configured.length) return null
  if (sampleState === 'loading') return <div className="transform-preview empty-preview"><h4>Vista previa Antes / Después</h4><p>Cargando una muestra acotada de la versión…</p></div>
  if (sampleState === 'error') return <div className="transform-preview empty-preview"><h4>Vista previa Antes / Después</h4><p>No se pudo cargar la muestra. La configuración continúa disponible, pero no se presenta una vista previa.</p></div>
  if (!rows.length) return <div className="transform-preview empty-preview"><h4>Vista previa Antes / Después</h4><p>La versión no contiene registros de muestra para las columnas seleccionadas.</p></div>
  const previews = rows.slice(0, 8).map((row, rowIndex) => {
    const after = { ...row }, errors = new Set<string>(), unsupported = new Set<string>()
    for (const transform of configured) {
      const result = applyTransform(after[transform.column], transform)
      after[transform.column] = result.value
      if (result.error) errors.add(transform.column)
      if (result.unsupported) unsupported.add(transform.column)
    }
    return { row, after, errors, unsupported, rowIndex }
  })
  return <section className="transform-preview" aria-label={`Vista previa Antes / Después · ${label}`}><div className="preview-heading"><div><h4>Vista previa Antes / Después</h4><p>{previews.length} registros reales · las transformaciones se aplican en el orden mostrado</p></div></div><div className="table-scroll"><table><thead><tr><th>Registro</th><th>Columna</th><th>Antes</th><th>Después</th><th>Interpretación</th></tr></thead><tbody>{previews.flatMap(preview => selectedColumns.map(column => <tr key={`${preview.rowIndex}-${column}`}><td>{preview.rowIndex + 1}</td><td className="mono">{column}</td><td><code>{displayValue(preview.row[column])}</code></td><td><code>{displayValue(preview.after[column])}</code></td><td>{preview.errors.has(column) ? <span className="preview-error">No se pudo interpretar; la transformación conservó el valor que recibió.</span> : preview.unsupported.has(column) ? <span className="preview-warning">Vista previa no disponible para este formato; la ejecución usará el formato declarado.</span> : <span className="preview-success">Aplicada</span>}</td></tr>))}</tbody></table></div><p className="preview-disclaimer">Vista informativa: no modifica el dataset ni su versión.</p></section>
}

function normalizePreviewValue(value: unknown, normalization: Normalization): unknown {
  if (value === null || value === undefined) return null
  let result = textValue(value)
  if (normalization.unicode_normalization !== 'NONE') result = result.normalize(normalization.unicode_normalization)
  if (normalization.trim) result = result.trim()
  if (normalization.case === 'UPPER') result = result.toUpperCase()
  if (normalization.case === 'LOWER') result = result.toLowerCase()
  return result
}

function keyValue(row: SampleRow, keys: string[], normalization?: Normalization): string {
  return `{ ${keys.map(key => `${key}: ${displayValue(normalization ? normalizePreviewValue(row[key], normalization) : row[key])}`).join(' · ')} }`
}

export function KeyNormalizationPreview({ value, keys, sourceRows = [], targetRows = [], sampleState = 'ready' }: { value: Normalization; keys: string[]; sourceRows?: SampleRow[]; targetRows?: SampleRow[]; sampleState?: SampleState }) {
  const sourceBefore = ' cliente01 ', targetBefore = 'CLIENTE01'
  const sourceAfter = normalizePreviewValue(sourceBefore, value), targetAfter = normalizePreviewValue(targetBefore, value)
  const actions = [value.trim && 'eliminar espacios externos', value.case === 'UPPER' && 'convertir a mayúsculas', value.case === 'LOWER' && 'convertir a minúsculas', value.unicode_normalization !== 'NONE' && `normalizar caracteres (${value.unicode_normalization})`].filter(Boolean)
  const sourceLimit = targetRows.length ? 5 : 10, targetLimit = sourceRows.length ? 5 : 10
  const samples = [
    ...sourceRows.slice(0, sourceLimit).map((row, index) => ({ source: 'Origen', row, index })),
    ...targetRows.slice(0, targetLimit).map((row, index) => ({ source: 'Destino', row, index })),
  ]
  return <section className="normalization-preview" aria-label="Ejemplo y vista previa de normalización de claves"><div className="normalization-example"><h4>Ejemplo con la configuración actual</h4><p>{actions.length ? `Después de ${actions.join(', ')}:` : 'Sin normalización declarada:'}</p><div><span>Origen</span><code>{displayValue(sourceBefore)}</code><b>→</b><code>{displayValue(sourceAfter)}</code></div><div><span>Destino</span><code>{displayValue(targetBefore)}</code><b>→</b><code>{displayValue(targetAfter)}</code></div><strong className={sourceAfter === targetAfter ? 'preview-success' : 'preview-error'}>Resultado → {sourceAfter === targetAfter ? 'Coincidencia' : 'Sin coincidencia'}</strong></div>
    {!!keys.length && <div className="key-preview"><div className="preview-heading"><div><h4>Vista previa Antes / Después</h4><p>Claves reales usadas internamente para el cruce · hasta 10 registros entre ambas fuentes</p></div></div>{sampleState === 'loading' ? <p>Cargando muestras acotadas de ambas versiones…</p> : sampleState === 'error' ? <p>No se pudieron cargar una o ambas muestras. La normalización puede configurarse, pero no se presenta una vista previa real.</p> : samples.length ? <div className="table-scroll"><table><thead><tr><th>Fuente</th><th>Registro</th><th>Clave antes</th><th>Clave después</th></tr></thead><tbody>{samples.map(sample => <tr key={`${sample.source}-${sample.index}`}><td>{sample.source}</td><td>{sample.index + 1}</td><td><code>{keyValue(sample.row, keys)}</code></td><td><code>{keyValue(sample.row, keys, value)}</code></td></tr>)}</tbody></table></div> : <p>No hay registros de muestra disponibles para estas versiones.</p>}<p className="preview-disclaimer">Vista informativa: la normalización no modifica los datasets originales.</p></div>}
  </section>
}

export function TransformBuilder({ value, onChange, columns, rows = [], sampleState = 'ready', label = 'Transformaciones previas' }: { value: Transform[]; onChange: (value: Transform[]) => void; columns?: DatasetColumn[]; rows?: SampleRow[]; sampleState?: SampleState; label?: string }) {
  const update = (index: number, changes: Partial<Transform>) => onChange(value.map((item, itemIndex) => itemIndex === index ? { ...item, ...changes } : item))
  const move = (index: number, direction: -1 | 1) => {
    const reordered = [...value], destination = index + direction
    ;[reordered[index], reordered[destination]] = [reordered[destination], reordered[index]]
    onChange(reordered)
  }
  return <section className="rule-builder" aria-label={label}><div className="builder-heading"><div><h3>{label}</h3><p>Se aplican de arriba hacia abajo; la vista previa es informativa y el snapshot recibido conserva sus valores.</p></div><button type="button" className="button secondary small" onClick={() => onChange([...value, { type: 'trim', column: '', parameters: {} }])}>Agregar transformación · {label}</button></div>{value.map((item, index) => <fieldset className="builder-card" key={index}><legend>Transformación {index + 1} · {label}</legend><div className="form-grid"><Field label="Columna de transformación"><ColumnPicker columns={columns} value={item.column} onChange={column => update(index, { column })}/></Field><Field label="Transformación"><select value={item.type} onChange={event => update(index, { type: event.target.value, parameters: { ...transformDefaults[event.target.value] } })}>{Object.entries(transformLabels).map(([kind, functionalLabel]) => <option key={kind} value={kind}>{functionalLabel}</option>)}</select></Field><TransformParameters transform={item} onChange={parameters => update(index, { parameters })}/></div><TransformExplanation transform={item}/><div className="builder-card-actions"><button type="button" className="text-button" disabled={index === 0} aria-label={`Mover transformación ${index + 1} hacia arriba`} onClick={() => move(index, -1)}><ArrowUp size={14}/> Subir</button><button type="button" className="text-button" disabled={index === value.length - 1} aria-label={`Mover transformación ${index + 1} hacia abajo`} onClick={() => move(index, 1)}><ArrowDown size={14}/> Bajar</button><button type="button" className="text-button" onClick={() => onChange(value.filter((_, itemIndex) => itemIndex !== index))}><Trash2 size={14}/> Quitar transformación {index + 1}</button></div></fieldset>)}<TransformPreview transforms={value} rows={rows} sampleState={sampleState} label={label}/></section>
}
