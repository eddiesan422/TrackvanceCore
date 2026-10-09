export type SchemaColumn = { name: string; type?: string; logical_type?: string; semantic_tag?: string }
export type SourceCandidate = { input_dataset_id: string; name: string; contract_id: string; contract_name: string; contract_revision_ids: string[]; versions: { id: string; version: number }[]; contract_revision_total?: number; contract_revision_offset?: number; contract_revision_limit?: number; version_total?: number; version_offset?: number; version_limit?: number; output_version_id?: string; output_dataset_id?: string; input_version_id?: string; approval_run_id?: string; approval_finished_at?: string; schema: SchemaColumn[]; eligibility: { eligible: boolean; strict_approval?: boolean; reasons: (string | { message?: string; code?: string })[] }; warnings?: string[]; finished_at?: string }
export type ReportSource = { alias: string; input_dataset_id: string; contract_id: string; contract_revision_ids: string[]; policy: 'LATEST_APPROVED' | 'SPECIFIC'; input_version_id?: string }
export type Join = { left_alias: string; right_alias: string; type: 'INNER' | 'LEFT' | 'RIGHT' | 'FULL'; keys: { left_column: string; right_column: string }[]; expected_cardinality: '1:1' | '1:N' | 'N:1' | 'N:M'; allow_many_to_many: boolean }
export type Projection = { source_alias: string; column: string; alias: string }
export type Filter = { source_alias?: string; column: string; operator: 'EQ' | 'NE' | 'GT' | 'GE' | 'LT' | 'LE' | 'IS_NULL' | 'IS_NOT_NULL' | 'IN' | 'CONTAINS' | 'STARTS_WITH'; value?: string | string[] | boolean }
export type FilterGroup = { operator: 'AND' | 'OR'; conditions: (Filter | FilterGroup)[] }
export type Parameter = { name: string; type: 'TEXT' | 'INTEGER' | 'DECIMAL' | 'DATE' | 'TIMESTAMP' | 'BOOLEAN'; value: string | boolean }
export type Order = { source_alias?: string; column: string; direction: 'ASC' | 'DESC' }
export type ReportDraft = { mode: 'GUIDED' | 'SQL'; sources: ReportSource[]; joins: Join[]; columns: Projection[]; source_filters: Record<string, FilterGroup>; post_filter?: FilterGroup; order_by: Order[]; sql?: string; parameters: Parameter[]; expected_schemas: Record<string, SchemaColumn[]>; output_preferences?: { format: 'CSV' | 'XLSX' } }
export type ResolvedContext = { context_id: string; expires_at: string; sources: Record<string, unknown>[]; warnings: string[]; query_hash: string }
export type Preview = { execution_id: string; rows: Record<string, unknown>[]; columns: SchemaColumn[]; cardinality: Record<string, unknown>[]; warnings: string[]; status: string; error_code?: string; error_message?: string; allow_generate?: boolean }
export const emptyDraft = (): ReportDraft => ({ mode: 'GUIDED', sources: [], joins: [], columns: [], source_filters: {}, order_by: [], parameters: [], expected_schemas: {} })
export const emptyGroup = (): FilterGroup => ({ operator: 'AND', conditions: [] })
function nonemptyGroup(group?: FilterGroup): FilterGroup | undefined {
  if (!group) return undefined
  const conditions = group.conditions.flatMap<Filter | FilterGroup>(condition => {
    if (!('conditions' in condition)) return [condition]
    const nested = nonemptyGroup(condition)
    return nested ? [nested] : []
  })
  return conditions.length ? { ...group, conditions } : undefined
}
/** Empty controls carry no predicate; they must not compile into an invalid or altered query. */
export function executableDraft(draft: ReportDraft): ReportDraft {
  return { ...draft, source_filters: Object.fromEntries(Object.entries(draft.source_filters).flatMap(([alias, group]) => { const filtered = nonemptyGroup(group); return filtered ? [[alias, filtered]] : [] })), post_filter: nonemptyGroup(draft.post_filter) }
}
export function sameDefinition(left: ReportDraft, right: ReportDraft) {
  const structural = (draft: ReportDraft) => ({ ...executableDraft(draft), parameters: draft.parameters.map(parameter => ({ name: parameter.name, type: parameter.type })) })
  return JSON.stringify(structural(left)) === JSON.stringify(structural(right))
}
export function mapFilterSources(group: FilterGroup | undefined, rename: (alias: string) => string | null): FilterGroup | undefined {
  if (!group) return undefined
  const conditions = group.conditions.flatMap<Filter | FilterGroup>(condition => {
    if ('conditions' in condition) {
      const nested = mapFilterSources(condition, rename)
      return nested ? [nested] : []
    }
    if (!condition.source_alias) return [condition]
    const alias = rename(condition.source_alias)
    return alias === null ? [] : [{ ...condition, source_alias: alias }]
  })
  return conditions.length ? { ...group, conditions } : undefined
}
export function draftError(draft: ReportDraft): string {
  if (!draft.sources.length) return 'Selecciona al menos una fuente.'
  const aliases = draft.sources.map(source => source.alias)
  if (aliases.some(alias => !/^[a-zA-Z][a-zA-Z0-9_]{0,62}$/.test(alias) || alias.startsWith('tv_')) || new Set(aliases.map(alias => alias.toLocaleLowerCase('en'))).size !== aliases.length) return 'Cada fuente necesita un alias único que empiece por una letra.'
  if (draft.sources.some(source => !source.contract_revision_ids.length || (source.policy === 'SPECIFIC' && !source.input_version_id))) return 'Indica revisiones admitidas y la versión específica de cada fuente cuando corresponda.'
  if (draft.mode === 'SQL') return draft.sql?.trim() ? '' : 'Escribe una consulta SQL.'
  if (!draft.columns.length) return 'Selecciona al menos una columna de resultado.'
  if (draft.columns.length > 100) return `Seleccionaste ${draft.columns.length} columnas; el límite es 100. Reduce la selección explícitamente.`
  if (draft.columns.some(column => !column.alias.trim()) || new Set(draft.columns.map(column => column.alias.toLocaleLowerCase('en'))).size !== draft.columns.length) return 'Los nombres de las columnas de resultado deben ser únicos y no vacíos.'
  if (draft.sources.length > 1 && draft.joins.length !== draft.sources.length - 1) return 'Relaciona todas las fuentes con cruces completos.'
  const joined = new Set([aliases[0]])
  for (const join of draft.joins) {
    if (!joined.has(join.left_alias) || joined.has(join.right_alias) || !aliases.includes(join.right_alias) || !join.keys.length || join.keys.some(key => !key.left_column || !key.right_column)) return 'Cada cruce necesita una fuente ya relacionada, una fuente nueva y llaves completas.'
    if (join.expected_cardinality === 'N:M' && !join.allow_many_to_many) return 'El cruce N:M exige autorización explícita y queda sujeto a límites de expansión.'
    joined.add(join.right_alias)
  }
  if (!draft.order_by.length) return 'Define un orden reproducible para el resultado.'
  return ''
}

/** R085-05: preserve edited aliases/order; add only missing explicit columns. */
export function selectColumns(current: Projection[], sources: { alias: string; columns: SchemaColumn[] }[], selected: boolean): Projection[] {
  const key = (alias: string, name: string) => JSON.stringify([alias, name])
  const target = new Set(sources.flatMap(source => source.columns.map(column => key(source.alias, column.name))))
  if (!selected) return current.filter(column => !target.has(key(column.source_alias, column.column)))
  const result = [...current], existing = new Set(current.map(column => key(column.source_alias, column.column)))
  const aliases = new Set(current.map(column => column.alias.toLocaleLowerCase('en')))
  for (const source of sources) for (const column of source.columns) {
    const identity = key(source.alias, column.name)
    if (existing.has(identity)) continue
    const base = `${source.alias}_${column.name}`.slice(0, 230)
    let alias = base, ordinal = 2
    while (aliases.has(alias.toLocaleLowerCase('en'))) alias = `${base}_${ordinal++}`
    aliases.add(alias.toLocaleLowerCase('en')); existing.add(identity)
    result.push({ source_alias: source.alias, column: column.name, alias })
  }
  return result
}
