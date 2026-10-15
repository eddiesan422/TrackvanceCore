import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { BarChart3, CheckCheck, Columns3, FileCheck2 } from 'lucide-react'
import { api } from '../../api/client'
import type { RecordData } from '../../api/client'
import { date, Empty, ErrorState, Field, Loading, Notice } from '../../components/ui'
import { category, checkName, controlCounts, controlLabels, controlTones, exactValue, expectedValue, finiteValue, logicalType, metricLabel, metricValue, nullRate, observedValue, percentage, profileMethod, quantity } from './sentinelPresentation'
import type { ControlCategory } from './sentinelPresentation'
import './sentinel-visual.css'

const categories: ControlCategory[] = ['CONFORMING', 'WARNING', 'FAILED', 'NOT_EVALUATED']

function ColumnBars({ columns, metric, title }: { columns: RecordData[]; metric: string; title: string }) {
  const measured = columns.map(column => ({ column, value: metric === 'null_rate' ? nullRate(column) : finiteValue(column[metric]) }))
  const maximum = metric === 'null_rate' ? 1 : Math.max(1, ...measured.map(item => item.value ?? 0))
  return <section className="sentinel-chart" aria-label={title}><h3>{title}</h3><p className="sentinel-axis-label">{metric === 'null_rate' ? 'Escala fija: 0 a 100 % de las filas de la versión' : `Escala: 0 a ${quantity(maximum)} ${metric === 'null_count' ? 'nulos' : 'valores distintos'}`}</p>
    {!columns.length ? <p className="muted">Selecciona columnas para comparar sus mediciones.</p> : <div className="sentinel-bars">{measured.map(({ column, value }) => {
      const description = metric.startsWith('null') ? `${quantity(column.null_count, 'nulos')} · ${percentage(nullRate(column))} de ${quantity(column.null_denominator, 'filas')}` : `${quantity(column.distinct_count, 'valores distintos')} de ${quantity(column.distinct_denominator, 'valores sin nulos')}`
      return <div className="sentinel-bar-row" key={column.name}><span title={column.name}>{column.name}</span><div role="img" aria-label={`${column.name}: ${description}`} title={description}>{value != null && <i style={{ width: `${Math.max(0, Math.min(100, 100 * value / maximum))}%` }}/>}</div><strong>{metric === 'null_rate' ? percentage(value) : quantity(value)}{value == null && <small>Sin medición</small>}</strong></div>
    })}</div>}
  </section>
}

function Evolution({ series }: { series: RecordData }) {
  const raw: RecordData[] = series.points || []
  const points = raw.filter(point => finiteValue(point.value) != null && Number.isFinite(Date.parse(point.observed_at)))
    .sort((a, b) => Date.parse(a.observed_at) - Date.parse(b.observed_at))
  const values = points.map(point => Number(point.value)), low = Math.min(...values), high = Math.max(...values)
  const first = Date.parse(points[0]?.observed_at || ''), last = Date.parse(points.at(-1)?.observed_at || '')
  const x = (point: RecordData) => 70 + (Date.parse(point.observed_at) - first) / (last - first || 1) * 490
  const y = (point: RecordData) => 145 - (point.value - low) / (high - low || 1) * 100
  const title = metricLabel(series.metric_key, series.dimensions)
  return <div className="sentinel-chart"><p className="muted">{title} · {profileMethod(series.method)} · definición v{series.metric_definition_version}</p>
    {points.length < 2 ? <p>Se necesitan dos observaciones comparables para mostrar evolución.</p> : <><svg viewBox="0 0 620 195" role="img" aria-label={`Evolución compatible de ${title}`}>
      <line x1="70" y1="145" x2="560" y2="145"/><line x1="70" y1="40" x2="70" y2="145"/>
      <polyline points={points.map(point => `${x(point)},${y(point)}`).join(' ')}/>
      {points.map((point, index) => <circle key={`${point.run_id}-${index}`} cx={x(point)} cy={y(point)} r="4"><title>{date(point.observed_at)}: {metricValue(series.metric_key, point.exact_value)} · versión {point.dataset_version_id}</title></circle>)}
      <text x="60" y="149" textAnchor="end">{quantity(low)}</text>{high !== low && <text x="60" y="48" textAnchor="end">{quantity(high)}</text>}
      <text x="70" y="170">{date(points[0].observed_at, false)}</text><text x="560" y="170" textAnchor="end">{date(points.at(-1)?.observed_at, false)}</text>
      <text x="315" y="190" textAnchor="middle">Fecha de observación · {series.metric_key.split(':')[0].includes('rate') || series.metric_key.startsWith('uniqueness_ratio') ? 'proporción de 0 a 1' : 'cantidad registrada'}</text>
    </svg><p className="muted">Los puntos se comparan por método, definición, columna y esquema. La línea une observaciones; no estima periodos sin datos.</p></>}
    <details><summary>Observaciones y valores exactos de la evolución</summary><div className="table-scroll"><table><thead><tr><th>Observación</th><th>Valor</th><th>Valor exacto</th><th>Versión registrada</th><th>Ejecución</th></tr></thead><tbody>{raw.map((point, index) => <tr key={`${point.run_id}-${index}`}><td>{date(point.observed_at)}</td><td>{metricValue(series.metric_key, point.exact_value)}</td><td className="mono">{exactValue(point.exact_value)}</td><td className="mono">{point.dataset_version_id}</td><td><Link to={`/runs/${point.run_id}`}>Ver ejecución</Link></td></tr>)}</tbody></table></div></details>
  </div>
}

function ControlResults({ checks, available }: { checks: RecordData[]; available: boolean }) {
  const [filter, setFilter] = useState<ControlCategory | 'CONTROLS' | ''>('CONTROLS')
  const counts = controlCounts(checks), total = checks.length - counts.OBSERVATION
  const visible = checks.filter(check => !filter || (filter === 'CONTROLS' ? category(check) !== 'OBSERVATION' : category(check) === filter))
  if (!available) return <Empty title="Controles no disponibles" description="Esta ejecución no conserva resultados de controles para construir una distribución."/>
  return <section className="sentinel-controls" aria-labelledby="sentinel-controls-title"><div className="sentinel-section-heading"><div><h3 id="sentinel-controls-title">Distribución de controles</h3><p>{quantity(total, 'controles')} · {quantity(counts.OBSERVATION)} {counts.OBSERVATION === 1 ? 'observación informativa' : 'observaciones informativas'} fuera del total</p></div><CheckCheck size={22} aria-hidden="true"/></div>
    {total > 0 ? <div className="sentinel-control-distribution" role="img" aria-label={categories.map(key => `${controlLabels[key]}: ${counts[key]}`).join('; ')}>{categories.filter(key => counts[key] > 0).map(key => <span className={`sentinel-category ${controlTones[key]}`} key={key} style={{ flexGrow: counts[key] }} title={`${controlLabels[key]}: ${quantity(counts[key])}`}/>)}</div> : <p className="muted">No hay controles registrados para distribuir.</p>}
    <div className="sentinel-control-filters" aria-label="Filtrar resultados de controles"><button className={`button secondary small ${filter === 'CONTROLS' ? 'selected' : ''}`} onClick={() => setFilter('CONTROLS')} aria-pressed={filter === 'CONTROLS'}>Controles ({quantity(total)})</button><button className={`button secondary small ${filter === '' ? 'selected' : ''}`} onClick={() => setFilter('')} aria-pressed={filter === ''}>Todos los resultados ({quantity(checks.length)})</button>{[...categories, 'OBSERVATION' as const].map(key => <button key={key} className={`button secondary small ${filter === key ? 'selected' : ''}`} aria-pressed={filter === key} onClick={() => setFilter(filter === key ? 'CONTROLS' : key)}><i className={`sentinel-dot ${controlTones[key]}`} aria-hidden="true"/>{controlLabels[key]} ({quantity(counts[key])})</button>)}</div>
    <p className="muted">Una advertencia es un control incumplido con severidad de advertencia. Los controles conformes no equivalen a filas válidas. Las reglas pueden superponerse; sus incumplimientos no se suman como filas distintas.</p>
    {!checks.length ? <Empty title="Sin controles completados" description="Las mediciones estarán disponibles al terminar la ejecución."/> : !visible.length ? <p className="muted">Sin resultados en esta categoría.</p> : <div className="sentinel-check-list">{visible.map((check, index) => <article className="sentinel-check" key={`${check.rule_id || check.code}-${index}`}><div className="sentinel-check-heading"><h4>{checkName(check)}</h4><span className={`badge ${controlTones[category(check)]}`}>{controlLabels[category(check)]}</span></div><dl><div><dt>Observado</dt><dd>{observedValue(check)}</dd></div><div><dt>{category(check) === 'OBSERVATION' ? 'Referencia' : 'Condición evaluada'}</dt><dd>{expectedValue(check)}</dd></div></dl><p>{check.message}</p><details><summary>Detalle técnico del control</summary><pre>{exactValue(check)}</pre></details></article>)}</div>}
  </section>
}

function VisualEvidence({ data }: { data: RecordData }) {
  const columns: RecordData[] = data.columns || [], checks: RecordData[] = data.checks || [], evolution: RecordData[] = data.evolution || []
  const [selectedColumns, setSelectedColumns] = useState<string[]>(columns.slice(0, 12).map(column => column.name))
  const [columnSearch, setColumnSearch] = useState(''), [nullMetric, setNullMetric] = useState('null_rate'), [seriesKey, setSeriesKey] = useState('')
  const visible = columns.filter(column => selectedColumns.includes(column.name))
  const matching = columns.filter(column => column.name.toLocaleLowerCase().includes(columnSearch.toLocaleLowerCase()))
  const indicators = data.indicators || {}, counts = controlCounts(checks), configuration = data.evaluated_configuration
  const controlsAvailable = data.checks_available ?? Array.isArray(data.checks)
  const seriesId = (series: RecordData) => JSON.stringify([series.metric_key, series.dimensions, series.method, series.metric_definition_version])
  const selected = evolution.find(series => seriesId(series) === seriesKey) || evolution[0]
  const partial = visible.length !== columns.length
  return <div className="sentinel-visual-summary">
    <div className="sentinel-evidence-heading"><FileCheck2 size={24} aria-hidden="true"/><div><h2>Resumen de esta ejecución</h2><p>Versión {data.dataset_version ?? 'no disponible'} · observada {date(data.observed_at)}{configuration && <> · {configuration.name}, revisión {configuration.version}</>}</p></div><span className="sentinel-evidence-tag">Evidencia persistida</span></div>
    <Notice>El resumen usa el perfil y los controles registrados para esta versión. Abrirlo no ejecuta nuevas lecturas del dataset.</Notice>
    <div className="run-metrics">{[['Filas de la versión', indicators.row_count, 'filas'], ['Columnas de la versión', indicators.column_count, 'columnas'], ['Filas duplicadas exactas', indicators.duplicate_row_count, 'filas'], ['Controles conformes', controlsAvailable ? counts.CONFORMING : null, 'controles']].map(([title, value, unit]) => <div key={String(title)}><span>{title}</span><strong>{quantity(value)}</strong><small>{value == null ? 'Sin medición registrada' : unit}</small></div>)}</div>
    <ControlResults checks={checks} available={controlsAvailable}/>
    <section className="sentinel-column-section" aria-labelledby="sentinel-columns-title"><div className="sentinel-section-heading"><div><h3 id="sentinel-columns-title">Nulos y diversidad por columna</h3><p>Compara medidas de la misma versión y consulta sus límites evaluados.</p></div><Columns3 size={22} aria-hidden="true"/></div>
      <details className="sentinel-column-picker"><summary>Seleccionar columnas ({quantity(visible.length)} de {quantity(columns.length)})</summary><div className="sentinel-column-actions"><Field label="Buscar columna"><input type="search" value={columnSearch} onChange={event => setColumnSearch(event.target.value)}/></Field><button className="button secondary small" onClick={() => setSelectedColumns(columns.map(column => column.name))}>Todas las columnas</button><button className="button secondary small" onClick={() => setSelectedColumns([])}>Quitar selección</button></div><div className="sentinel-column-options">{matching.map(column => <label key={column.name}><input type="checkbox" checked={selectedColumns.includes(column.name)} onChange={event => setSelectedColumns(event.target.checked ? [...selectedColumns, column.name] : selectedColumns.filter(name => name !== column.name))}/><span>{column.name}</span></label>)}</div>{!matching.length && <p>Sin columnas con ese nombre.</p>}</details>
      <p className={partial ? 'sentinel-partial-view' : 'muted'} role="status">{partial ? `Vista parcial: ${quantity(visible.length)} de ${quantity(columns.length)} columnas seleccionadas. Los indicadores generales siguen incluyendo toda la versión.` : `Vista de todas las columnas: ${quantity(columns.length)}.`}</p>
      <Field label="Medida del gráfico de nulos"><select value={nullMetric} onChange={event => setNullMetric(event.target.value)}><option value="null_rate">Porcentaje de filas</option><option value="null_count">Cantidad de nulos</option></select></Field>
      <div className="sentinel-chart-grid"><ColumnBars columns={visible} metric={nullMetric} title="Nulos por columna"/><ColumnBars columns={visible} metric="distinct_count" title="Diversidad por columna"/></div>
      <p className="muted">La diversidad cuenta valores distintos sin nulos. La repetición de categorías no implica unicidad incumplida. Los valores de aparición única se miden sobre los valores sin nulos; una restricción de unicidad existe solo si fue declarada. Las filas duplicadas son las adicionales a la primera de cada grupo idéntico, con nulos iguales.</p>
      <div className="sentinel-column-cards">{visible.map(column => <article className="sentinel-column-card" key={column.name}><div><h4>{column.name}</h4><span>{logicalType(column.logical_type)}</span></div><dl><div><dt>Nulos</dt><dd>{quantity(column.null_count)} · {percentage(nullRate(column))}<small>Denominador: {quantity(column.null_denominator, 'filas')}</small></dd></div><div><dt>Valores distintos</dt><dd>{quantity(column.distinct_count)}<small>Sobre {quantity(column.distinct_denominator, 'valores sin nulos')}</small></dd></div><div><dt>Diversidad proporcional</dt><dd>{percentage(column.distinct_rate)}</dd></div><div><dt>Valores de aparición única</dt><dd>{percentage(column.uniqueness_ratio)}</dd></div></dl><div className="sentinel-column-threshold"><strong>Umbral de nulos evaluado</strong>{column.null_threshold_checks?.length ? (column.null_threshold_checks as RecordData[]).map((check, index) => <p key={index}>{expectedValue({ ...check, metric_key: `null_rate:${column.name}` })}</p>) : <p>Sin umbral registrado en esta ejecución</p>}</div></article>)}</div>
    </section>
    <section aria-labelledby="sentinel-evolution-title"><div className="sentinel-section-heading"><div><h3 id="sentinel-evolution-title">Evolución compatible hasta esta ejecución</h3><p>No incluye versiones posteriores ni muestras con otra definición de la métrica.</p></div><BarChart3 size={22} aria-hidden="true"/></div>{!evolution.length ? <p className="muted">No hay métricas históricas comparables.</p> : <><Field label="Métrica de evolución"><select value={selected ? seriesId(selected) : ''} onChange={event => setSeriesKey(event.target.value)}>{evolution.map(item => <option value={seriesId(item)} key={seriesId(item)}>{metricLabel(item.metric_key, item.dimensions)} · {profileMethod(item.method)} v{item.metric_definition_version}</option>)}</select></Field>{selected && <Evolution series={selected}/>}</>}{data.evolution_truncated && <Notice>La ventana incluye hasta {data.evolution_limit} muestras de métricas. El historial anterior queda fuera de este gráfico.</Notice>}</section>
    <details className="sentinel-technical"><summary>Tabla de métricas exactas de la ejecución</summary><dl className="detail-list"><div><dt>Ejecución</dt><dd className="mono">{data.run_id}</dd></div><div><dt>Versión de origen</dt><dd className="mono">{data.dataset_version_id}</dd></div><div><dt>Configuración evaluada</dt><dd className="mono">{data.configuration_id}</dd></div><div><dt>Hash de configuración evaluada</dt><dd className="mono">{configuration?.config_hash || 'No disponible'}</dd></div><div><dt>Método del perfil</dt><dd>{profileMethod(data.profile_method)} · definición {data.metric_definition_version ?? 'No disponible'}</dd></div></dl><div className="table-scroll"><table><thead><tr><th>Métrica</th><th>Dimensiones</th><th>Valor exacto</th><th>Método</th><th>Definición</th></tr></thead><tbody>{(data.exact_metrics || []).map((item: RecordData, index: number) => <tr key={index}><td>{item.metric_key}</td><td>{exactValue(item.dimensions)}</td><td className="mono">{exactValue(item.exact_value)}</td><td>{item.method}</td><td>{item.metric_definition_version}</td></tr>)}</tbody></table></div><details><summary>Perfil exacto por columna</summary><pre>{exactValue(columns)}</pre></details></details>
  </div>
}

export function SentinelVisualSummary({ runId }: { runId: string }) {
  const summary = useQuery({ queryKey: ['sentinel-visual', runId], queryFn: () => api(`/runs/${runId}/visual-summary`) })
  if (summary.isPending) return <Loading text="Cargando métricas de esta ejecución…"/>
  if (summary.error) return <ErrorState error={summary.error} retry={() => summary.refetch()}/>
  return <VisualEvidence key={runId} data={summary.data}/>
}
