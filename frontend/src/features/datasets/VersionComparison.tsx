import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { api, post } from '../../api/client'
import { Badge, date, Empty, ErrorState, Field, Loading, Notice, number, PageHeading } from '../../components/ui'
import './comparison.css'

type Version = { id: string; version: number; created_at: string; row_count: number; schema: { name: string; logical_type: string }[]; schema_json?: { name: string; logical_type: string }[] }
type Dataset = { id: string; name: string; versions: Version[] }
type Comparison = {
  id: string; before_version_id: string; after_version_id: string; status: string; created_at: string
  progress_percent: number; progress_stage: string; error_code?: string; error_message?: string
  summary: { mode?: string; before_rows?: number; after_rows?: number; counts?: Record<string, number>; warnings?: string[]; schema_changes?: { column: string; before: string | null; after: string | null; kind: string }[]; elapsed_seconds?: number; before_scope?: object; after_scope?: object }
}
type Detail = { id: number; kind: string; multiplicity: number; before: Record<string, unknown> | null; after: Record<string, unknown> | null; changed_columns: string[] }
const kinds = { ADDED: 'Incorporados', ABSENT: 'Ausentes', MODIFIED: 'Modificados', UNCHANGED: 'Sin cambios' }
const terminal = (status: string) => ['SUCCESS', 'FAILED', 'CANCELLED'].includes(status)

export function VersionComparison() {
  const params = useParams()
  const datasetId = params.datasetId || params.id || ''
  const [searchParams, setSearchParams] = useSearchParams()
  const queryClient = useQueryClient()
  const [before, setBefore] = useState(searchParams.get('from') || '')
  const [after, setAfter] = useState('')
  const [keys, setKeys] = useState<string[]>([])
  const [kind, setKind] = useState('')
  const [search, setSearch] = useState('')
  const [offset, setOffset] = useState(0)
  const comparisonId = searchParams.get('report') || ''
  const exceptionId = searchParams.get('exception_id') || ''
  const dataset = useQuery({ queryKey: ['datasets', datasetId], queryFn: () => api<Dataset>(`/datasets/${datasetId}`), enabled: !!datasetId })
  const history = useQuery({ queryKey: ['comparisons', datasetId], queryFn: () => api<{ items: Comparison[] }>(`/datasets/${datasetId}/comparisons`), enabled: !!datasetId })
  const report = useQuery({ queryKey: ['comparison', comparisonId], queryFn: () => api<Comparison>(`/version-comparisons/${comparisonId}`), enabled: !!comparisonId,
    refetchInterval: query => query.state.data && !terminal(query.state.data.status) ? 1500 : false })
  const rows = useQuery({ queryKey: ['comparison-rows', comparisonId, kind, search, offset],
    queryFn: () => api<{ items: Detail[]; total: number; records: number }>(`/version-comparisons/${comparisonId}/rows?${new URLSearchParams({ ...(kind ? { kind } : {}), search, offset: String(offset), limit: '50' })}`),
    enabled: !!comparisonId && report.data?.status === 'SUCCESS' })
  const versions = dataset.data?.versions || []
  const selectedBefore = before || versions[1]?.id || versions[0]?.id || ''
  const selectedAfter = after || versions[0]?.id || ''
  const beforeVersion = versions.find(version => version.id === selectedBefore)
  const afterVersion = versions.find(version => version.id === selectedAfter)
  const columns = (beforeVersion?.schema || beforeVersion?.schema_json || []).filter(column => (afterVersion?.schema || afterVersion?.schema_json || []).some(candidate => candidate.name === column.name && candidate.logical_type === column.logical_type))
  const create = useMutation({ mutationFn: () => post<Comparison>(`/datasets/${datasetId}/comparisons`, { before_version_id: selectedBefore, after_version_id: selectedAfter, keys }),
    onSuccess: result => { setSearchParams({ report: result.id, ...(exceptionId ? { exception_id: exceptionId } : {}) }); setOffset(0); void queryClient.invalidateQueries({ queryKey: ['comparisons', datasetId] }) } })
  const cancel = useMutation({ mutationFn: () => post(`/version-comparisons/${comparisonId}/cancel`, {}),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: ['comparison', comparisonId] }) } })

  return <div className="version-comparison">
    <PageHeading title="Comparar versiones" description={`Poblaciones completas de ${dataset.data?.name || 'este dataset'}. Conserva un informe de evidencia con sus identidades y alcance.`} />
    <p><Link to={`/datasets/${datasetId}`}>← Volver a versiones del dataset</Link></p>
    {exceptionId && <p><Link to={`/exceptions?id=${encodeURIComponent(exceptionId)}`}>Volver a la incidencia y validar la corrección</Link></p>}
    {dataset.isPending ? <Loading /> : dataset.error ? <ErrorState error={dataset.error} /> : versions.length < 2 ? <Empty title="Necesitas dos versiones" description="Publica otra versión en este mismo dataset para comparar sus poblaciones." /> : <section className="panel" aria-label="Seleccionar versiones">
      <div className="form-grid">
        <Field label="Versión anterior"><select value={selectedBefore} onChange={event => { setBefore(event.target.value); setKeys([]) }}>{versions.map(version => <option value={version.id} key={version.id}>v{version.version} · {number(version.row_count)} registros · {date(version.created_at)}</option>)}</select></Field>
        <Field label="Versión posterior"><select value={selectedAfter} onChange={event => { setAfter(event.target.value); setKeys([]) }}>{versions.map(version => <option value={version.id} key={version.id}>v{version.version} · {number(version.row_count)} registros · {date(version.created_at)}</option>)}</select></Field>
      </div>
      <fieldset><legend>Declarar clave de correspondencia (opcional, simple o compuesta)</legend>
        <div className="button-row">{columns.map(column => <label key={column.name}><input type="checkbox" checked={keys.includes(column.name)} onChange={event => setKeys(current => event.target.checked ? [...current, column.name] : current.filter(key => key !== column.name))} /> {column.name} ({column.logical_type})</label>)}</div>
        <p className="muted">Se valida unicidad en ambas poblaciones. Con claves ambiguas, nulas o ausentes se compara contenido y multiplicidad sin inferir correspondencias.</p>
      </fieldset>
      <Notice>Una ausencia entre lotes independientes no demuestra eliminación. El informe registra los alcances y sus advertencias.</Notice>
      <button className="button primary" disabled={create.isPending || !selectedBefore || !selectedAfter || selectedBefore === selectedAfter} onClick={() => create.mutate()}>{create.isPending ? 'Encolando…' : 'Comparar poblaciones completas'}</button>
      {create.error && <ErrorState error={create.error} />}
    </section>}
    {comparisonId && <section className="panel" aria-label="Informe de comparación">
      {report.isPending ? <Loading /> : report.error ? <ErrorState error={report.error} /> : report.data && <>
        <div className="section-heading"><h2>Informe de evidencia</h2><Badge value={report.data.status} /></div>
        <p className="muted">{report.data.id} · {date(report.data.created_at)}</p>
        {!terminal(report.data.status) && <><progress max={100} value={report.data.progress_percent} aria-label="Progreso de comparación" /><p role="status">{report.data.progress_stage}</p><button className="button" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Cancelar comparación</button></>}
        {report.data.error_message && <Notice>{report.data.error_code}: {report.data.error_message}</Notice>}
        {cancel.error && <ErrorState error={cancel.error} />}
        {report.data.status === 'SUCCESS' && <>
          <p>Anterior: {number(report.data.summary.before_rows)} · Posterior: {number(report.data.summary.after_rows)} · {report.data.summary.mode === 'KEYED' ? 'Claves únicas verificadas' : 'Contenido y multiplicidad'} · {report.data.summary.elapsed_seconds} s</p>
          <div className="button-row">{Object.entries(kinds).map(([value, title]) => <button key={value} className="button" aria-pressed={kind === value} onClick={() => { setKind(kind === value ? '' : value); setOffset(0) }}>{title}: {number(report.data?.summary.counts?.[value])}</button>)}</div>
          {report.data.summary.warnings?.map(warning => <Notice key={warning}>{warning}</Notice>)}
          <details><summary>Alcance y períodos de las versiones</summary><p>Identidades: {report.data.before_version_id} → {report.data.after_version_id}</p><div className="form-grid"><pre>{JSON.stringify(report.data.summary.before_scope, null, 2)}</pre><pre>{JSON.stringify(report.data.summary.after_scope, null, 2)}</pre></div></details>
          <h3>Cambios de esquema</h3>
          {report.data.summary.schema_changes?.length ? <div className="table-wrap"><table><thead><tr><th>Columna</th><th>Tipo anterior</th><th>Tipo posterior</th></tr></thead><tbody>{report.data.summary.schema_changes.map(change => <tr key={change.column}><td>{change.column}</td><td>{change.before ?? 'Columna ausente'}</td><td>{change.after ?? 'Columna ausente'}</td></tr>)}</tbody></table></div> : <p>Sin cambios de columnas o tipos lógicos.</p>}
          <Field label="Buscar valores en el informe"><input value={search} maxLength={200} onChange={event => { setSearch(event.target.value); setOffset(0) }} /></Field>
          {rows.isPending ? <Loading /> : rows.error ? <ErrorState error={rows.error} /> : <>
            <p>{number(rows.data?.records)} registros en {number(rows.data?.total)} grupos de contenido.</p>
            <div className="table-wrap"><table><thead><tr><th>Resultado</th><th>Multiplicidad</th><th>Antes</th><th>Después</th><th>Columnas diferentes</th></tr></thead><tbody>{rows.data?.items.map(row => <tr key={row.id}><td>{kinds[row.kind as keyof typeof kinds]}</td><td>{number(row.multiplicity)}</td><td><pre>{row.before === null ? 'Registro ausente' : JSON.stringify(row.before, null, 2)}</pre></td><td><pre>{row.after === null ? 'Registro ausente' : JSON.stringify(row.after, null, 2)}</pre></td><td>{row.changed_columns.join(', ') || '—'}</td></tr>)}</tbody></table></div>
            <div className="button-row"><button className="button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 50))}>Anterior</button><span>{offset + 1}–{Math.min(offset + 50, rows.data?.total || 0)} de {number(rows.data?.total)}</span><button className="button" disabled={offset + 50 >= (rows.data?.total || 0)} onClick={() => setOffset(offset + 50)}>Siguiente</button></div>
          </>}
          <a className="button" href={`/api/v1/version-comparisons/${comparisonId}/download`}>Descargar informe completo (SQLite)</a>
        </>}
      </>}
    </section>}
    <section className="panel"><h2>Informes recientes</h2>{history.error ? <ErrorState error={history.error} /> : history.isPending ? <Loading /> : history.data?.items.length ? <ul>{history.data.items.map(item => <li key={item.id}><Link to={`?${new URLSearchParams({ report: item.id, ...(exceptionId ? { exception_id: exceptionId } : {}) })}`}>{date(item.created_at)} · {item.status} · {item.id}</Link></li>)}</ul> : <p>Todavía no hay comparaciones registradas.</p>}</section>
  </div>
}
