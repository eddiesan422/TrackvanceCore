import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Plus, RefreshCw } from 'lucide-react'
import { api, post } from '../../api/client'
import type { Collection, RecordData } from '../../api/client'
import { ErrorState, Field, Loading, Modal, Notice } from '../../components/ui'
import { ColumnMultiSelect } from './ColumnMultiSelect'
import type { DatasetColumn } from './ColumnMultiSelect'
import { ComparisonBuilder, NormalizationFields, noNormalization, RuleBuilder } from './RuleBuilder'
import type { Comparison, Normalization, Rule } from './RuleBuilder'

type Module = 'intake' | 'recon' | 'sentinel'
interface DatasetSchema {
  dataset_id: string
  dataset_name: string
  version_id: string | null
  version: number | null
  schema_hash: string | null
  scan_mode: 'NO_VERSION' | 'PERSISTED_PROFILE' | 'CANONICAL_PARQUET_METADATA'
  scanned_rows: number
  columns: DatasetColumn[]
}
const definitions = { intake: { singular: 'contrato', endpoint: '/intake/contracts' }, recon: { singular: 'control', endpoint: '/recon/controls' }, sentinel: { singular: 'monitor', endpoint: '/monitors' } }
const split = (text: string) => [...new Set(text.split(',').map(v => v.trim()).filter(Boolean))]

export function ConfigDialog({ module, open, close, initial }: { module: Module; open: boolean; close: () => void; initial?: RecordData }) {
  const definition = definitions[module], cache = useQueryClient(), old = initial?.config || {}
  const [name, setName] = useState(initial?.name || ''), [datasetId, setDatasetId] = useState(initial?.dataset_id || ''), [targetId, setTargetId] = useState(initial?.target_dataset_id || '')
  const [owner, setOwner] = useState(initial?.owner || 'Equipo de datos'), [description, setDescription] = useState(initial?.description || '')
  const [required, setRequired] = useState<string[]>(old.required_columns || []), [unique, setUnique] = useState<string[]>(old.unique_columns || []), [numeric, setNumeric] = useState<string[]>(old.numeric_columns || []), [positive, setPositive] = useState<string[]>(old.positive_columns || [])
  const [sentinelRequired, setSentinelRequired] = useState((old.required_columns || []).join(', ')), [keys, setKeys] = useState((old.key_columns || []).join(', ')), [nullColumns, setNullColumns] = useState((old.null_columns || []).join(', '))
  const [threshold, setThreshold] = useState(String((old.max_error_rate ?? old.max_null_rate ?? 0.05) * 100)), [volume, setVolume] = useState(String(old.max_volume_change_pct ?? 15)), [age, setAge] = useState(String(old.max_age_hours ?? 48))
  const [rules, setRules] = useState<Rule[]>(old.rules || [])
  const [normalization, setNormalization] = useState<Normalization>(old.key_normalization || { ...noNormalization, trim: !!initial && (old.schema_version ?? 1) === 1 })
  const [comparisons, setComparisons] = useState<Comparison[]>(old.comparison_rules || [{ type: 'numeric_tolerance', source_column: old.amount_column || '', target_column: old.amount_column || '', parameters: { abs: old.tolerance || '0.01', percent: null, denominator: 'SOURCE', zero_denominator: 'EXACT_ONLY', equal_nulls: false } }])
  const [aggregate, setAggregate] = useState(!!old.aggregation), [aggregation, setAggregation] = useState(old.aggregation || { side: 'TARGET', operation: 'sum', column: '', output_column: '' })
  const datasets = useQuery({ queryKey: ['datasets'], queryFn: () => api<Collection>('/datasets'), enabled: open })
  const dataset = useQuery({ queryKey: ['dataset', datasetId], queryFn: () => api(`/datasets/${datasetId}`), enabled: open && !!datasetId && module !== 'intake' })
  const schema = useQuery({ queryKey: ['dataset-schema', datasetId], queryFn: () => api<DatasetSchema>(`/datasets/${datasetId}/schema`), enabled: open && !!datasetId && module === 'intake' })
  const refreshSchema = useMutation({ mutationFn: () => api<DatasetSchema>(`/datasets/${datasetId}/schema?refresh=true`), onSuccess: value => cache.setQueryData(['dataset-schema', datasetId], value) })
  const create = useMutation({ mutationFn: () => {
    const base = { ...old, schema_version: 2 }
    if (module === 'recon') delete base.rules
    else base.rules = rules
    const config = module === 'intake' ? { ...base, required_columns: required, unique_columns: unique, numeric_columns: numeric, positive_columns: positive, max_error_rate: Number(threshold) / 100 } : module === 'recon' ? { ...base, key_columns: split(keys), key_normalization: normalization, comparison_rules: comparisons, aggregation: aggregate ? aggregation : null } : { ...base, required_columns: split(sentinelRequired), null_columns: split(nullColumns), max_null_rate: Number(threshold) / 100, max_volume_change_pct: Number(volume), max_age_hours: Number(age) }
    return initial ? post(`${definition.endpoint}/${initial.id}/versions`, { config, description }) : post(definition.endpoint, { name: name.trim(), dataset_id: datasetId, ...(module === 'recon' ? { target_dataset_id: targetId } : {}), owner, description, config })
  }, onSuccess: () => { cache.invalidateQueries({ queryKey: ['configurations', module] }); close() } })
  const selectDataset = (target = false) => <select required disabled={!!initial} value={target ? targetId : datasetId} onChange={e => {
    if (target) return setTargetId(e.target.value)
    setDatasetId(e.target.value)
    if (module === 'intake' && !initial) { setRequired([]); setUnique([]); setNumeric([]); setPositive([]) }
  }}><option value="">Selecciona un dataset</option>{datasets.data?.items.map(d => <option key={d.id} value={d.id}>{d.name}</option>)}</select>
  return <Modal open={open} onOpenChange={value => { if (!value && !create.isPending) close() }} title={initial ? `Nueva versión de ${initial.name}` : `Nuevo ${definition.singular}`} description={initial ? `Se publicará una versión nueva. Las ejecuciones y la versión ${initial.version} conservarán su configuración.` : 'Define reglas explícitas y reutilízalas sobre cada nueva versión de tus datos.'} wide><form onSubmit={e => { e.preventDefault(); create.mutate() }}>
    {datasets.isPending ? <Loading/> : datasets.error ? <ErrorState error={datasets.error} retry={() => datasets.refetch()}/> : <div className="form-stack">
      <div className="form-grid"><Field label={`Nombre del ${definition.singular}`}><input required disabled={!!initial} maxLength={120} value={name} onChange={e => setName(e.target.value)}/></Field><Field label="Responsable"><input required disabled={!!initial} value={owner} onChange={e => setOwner(e.target.value)}/></Field></div>
      <div className={module === 'recon' ? 'form-grid' : ''}><Field label={module === 'recon' ? 'Dataset de origen' : 'Dataset'}>{selectDataset()}</Field>{module === 'recon' && <Field label="Dataset de destino">{selectDataset(true)}</Field>}</div>
      {!datasets.data?.items.length && <Notice>Primero <Link to="/datasets?upload=1">carga un dataset</Link> para asociarlo al control.</Notice>}
      {datasetId && module === 'intake' && (schema.isPending ? <Loading text="Escaneando el esquema del dataset…"/> : schema.error ? <ErrorState error={schema.error} retry={() => schema.refetch()}/> : <div className="schema-hint"><div className="schema-hint-heading"><div><strong>Esquema disponible</strong><span>{schema.data?.version_id ? `Versión ${schema.data.version} · ${schema.data.columns.length} columnas` : 'El dataset todavía no tiene versiones'}</span></div><button type="button" className="button secondary small" disabled={refreshSchema.isPending || !schema.data?.version_id} onClick={() => refreshSchema.mutate()}><RefreshCw size={14}/>{refreshSchema.isPending ? 'Actualizando…' : 'Actualizar esquema'}</button></div>{schema.data?.version_id && <div className="schema-columns">{schema.data.columns.map(column => <code key={column.name}><strong>{column.name}</strong><small>{column.logical_type}{column.semantic_tag ? ` · ${column.semantic_tag}` : ''}</small></code>)}</div>}{schema.data?.scan_mode === 'CANONICAL_PARQUET_METADATA' && <span>Metadata de la última versión verificada sin leer filas del dataset.</span>}{refreshSchema.error && <ErrorState error={refreshSchema.error}/>}</div>)}
      {datasetId && module !== 'intake' && (dataset.isPending ? <Loading text="Leyendo columnas disponibles…"/> : dataset.error ? <ErrorState error={dataset.error}/> : <div className="schema-hint"><strong>Columnas disponibles en origen</strong><div>{(dataset.data?.versions?.[0]?.schema || []).map((c: RecordData) => <code key={c.name}>{c.name}</code>)}</div></div>)}
      <div className="form-section-title">Reglas del control</div>
      {module === 'intake' && <><div className="form-grid"><ColumnMultiSelect label="Columnas obligatorias" hint="Selecciona únicamente los campos que deben contener un valor." columns={schema.data?.columns || []} selected={required} onChange={setRequired} disabled={!schema.data?.version_id || schema.isPending}/><ColumnMultiSelect label="Columnas sin duplicados" hint="Selecciona las columnas que deben ser únicas." columns={schema.data?.columns || []} selected={unique} onChange={setUnique} disabled={!schema.data?.version_id || schema.isPending}/><ColumnMultiSelect label="Columnas numéricas" hint="Los tipos numéricos detectados aparecen primero; también puedes validar otro campo." columns={schema.data?.columns || []} selected={numeric} onChange={setNumeric} disabled={!schema.data?.version_id || schema.isPending} numericFirst/><ColumnMultiSelect label="Columnas con valores positivos" hint="Solo se muestran columnas detectadas como numéricas." columns={schema.data?.columns || []} selected={positive} onChange={setPositive} disabled={!schema.data?.version_id || schema.isPending} numericOnly/></div><Field label="Porcentaje máximo de filas con error (%)"><input type="number" required min="0" max="100" step="0.1" value={threshold} onChange={e => setThreshold(e.target.value)}/></Field></>}
      {module === 'recon' && <><Field label="Columnas clave" hint="Una o más columnas, separadas por coma. Deben existir en ambas fuentes."><input required value={keys} onChange={e => setKeys(e.target.value)}/></Field><h3>Normalización declarada de claves</h3><NormalizationFields value={normalization} onChange={setNormalization}/>{initial && !old.key_normalization && <Notice>Esta configuración histórica eliminaba espacios externos. La nueva versión conserva esa opción de forma explícita; puedes cambiarla antes de publicar.</Notice>}<ComparisonBuilder value={comparisons} onChange={setComparisons}/><Field label="Conciliación 1:N"><select value={aggregate ? 'AGGREGATE' : 'NONE'} onChange={e => setAggregate(e.target.value === 'AGGREGATE')}><option value="NONE">Sin agregación</option><option value="AGGREGATE">Agrupar antes de comparar</option></select></Field>{aggregate && <div className="form-grid"><Field label="Fuente que se agrupa"><select value={aggregation.side} onChange={e => setAggregation({ ...aggregation, side: e.target.value })}><option value="SOURCE">Origen</option><option value="TARGET">Destino</option></select></Field><Field label="Operación de agregación"><select value={aggregation.operation} onChange={e => setAggregation({ ...aggregation, operation: e.target.value })}><option value="sum">Suma</option><option value="count">Cantidad de registros</option></select></Field>{aggregation.operation === 'sum' && <Field label="Columna a sumar"><input required value={aggregation.column} onChange={e => setAggregation({ ...aggregation, column: e.target.value })}/></Field>}<Field label="Columna del resultado agregado" hint="Usa este nombre en el campo de comparación del lado agrupado."><input required value={aggregation.output_column} onChange={e => setAggregation({ ...aggregation, output_column: e.target.value })}/></Field></div>}</>}
      {module === 'sentinel' && <><div className="form-grid"><Field label="Columnas requeridas"><input value={sentinelRequired} onChange={e => setSentinelRequired(e.target.value)}/></Field><Field label="Columnas a revisar por nulos"><input value={nullColumns} onChange={e => setNullColumns(e.target.value)}/></Field><Field label="Porcentaje máximo de nulos (%)"><input required type="number" min="0" max="100" step="0.1" value={threshold} onChange={e => setThreshold(e.target.value)}/></Field><Field label="Cambio máximo de volumen (%)"><input required type="number" min="0" step="0.1" value={volume} onChange={e => setVolume(e.target.value)}/></Field><Field label="Antigüedad máxima (horas)"><input required type="number" min="0.1" step="any" value={age} onChange={e => setAge(e.target.value)}/></Field></div><Notice>El volumen se compara con la versión anterior. Puedes agregar una banda histórica y controles sobre el tipo de las columnas.</Notice></>}
      {module !== 'recon' && <RuleBuilder value={rules} onChange={setRules} sentinel={module === 'sentinel'}/>}
      <Field label="Descripción (opcional)"><textarea rows={2} value={description} onChange={e => setDescription(e.target.value)}/></Field>
    </div>}{create.error && <ErrorState error={create.error}/>}<div className="modal-footer"><button type="button" className="button secondary" onClick={close} disabled={create.isPending}>Cancelar</button><button className="button primary" disabled={create.isPending || !datasetId || (module === 'recon' && !targetId)}><Plus size={16}/>{create.isPending ? 'Guardando…' : initial ? 'Publicar nueva versión' : `Crear ${definition.singular}`}</button></div>
  </form></Modal>
}
