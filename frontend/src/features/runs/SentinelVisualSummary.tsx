import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../../api/client'
import type { RecordData } from '../../api/client'
import { Badge, date, Empty, ErrorState, Field, Loading, Notice, number } from '../../components/ui'
import './sentinel-visual.css'

const exact = (value: unknown) => value == null ? 'No disponible' : String(value)
const scalar = (value: unknown) => value == null ? 'No disponible' : typeof value === 'object' ? JSON.stringify(value) : String(value)

function ColumnBars({ columns, metric, title }: { columns: RecordData[]; metric: string; title: string }) {
  const measured = columns.filter(column => typeof column[metric] === 'number' && Number.isFinite(column[metric]))
  const maximum = Math.max(1, ...measured.map(column => column[metric]))
  return <div className="sentinel-chart"><h3>{title}</h3>{!measured.length ? <p className="muted">No hay mediciones disponibles.</p> : measured.map(column => <div className="sentinel-bar-row" key={column.name}>
    <span>{column.name}</span><div role="img" aria-label={`${column.name}: ${exact(column[metric])}`}><i style={{ width: `${100 * column[metric] / maximum}%` }}/></div><strong>{exact(column[metric])}</strong>
  </div>)}</div>
}

function Evolution({ series }: { series: RecordData }) {
  const points = (series.points as RecordData[]).filter(point => typeof point.value === 'number' && Number.isFinite(point.value) && Number.isFinite(Date.parse(point.observed_at)))
  const values = points.map(point => point.value), low = Math.min(...values), high = Math.max(...values)
  const first = Date.parse(points[0]?.observed_at || ''), last = Date.parse(points.at(-1)?.observed_at || '')
  const x = (point: RecordData) => 35 + (Date.parse(point.observed_at) - first) / (last - first || 1) * 525
  const y = (point: RecordData) => 135 - (point.value - low) / (high - low || 1) * 105
  return <div className="sentinel-chart"><p className="muted">{series.method} · definición v{series.metric_definition_version}</p>
    {points.length < 2 ? <p>Se necesitan dos observaciones comparables para mostrar evolución.</p> : <svg viewBox="0 0 600 160" role="img" aria-label={`Evolución compatible de ${series.metric_key}`}>
      <line x1="35" y1="135" x2="560" y2="135"/><polyline points={points.map(point => `${x(point)},${y(point)}`).join(' ')}/>
      {points.map((point, index) => <circle key={`${point.run_id}-${index}`} cx={x(point)} cy={y(point)} r="4"><title>{date(point.observed_at)}: {exact(point.exact_value)}</title></circle>)}
      <text x="5" y="140">{number(low)}</text><text x="5" y="25">{number(high)}</text>
    </svg>}
    <div className="table-scroll"><table><thead><tr><th>Observación</th><th>Valor exacto</th><th>Versión</th><th>Ejecución</th></tr></thead><tbody>{(series.points as RecordData[]).map((point, index) => <tr key={`${point.run_id}-${index}`}><td>{date(point.observed_at)}</td><td className="mono">{exact(point.exact_value)}</td><td className="mono">{point.dataset_version_id}</td><td><Link to={`/runs/${point.run_id}`}>Ver ejecución</Link></td></tr>)}</tbody></table></div>
  </div>
}

export function SentinelVisualSummary({ runId }: { runId: string }) {
  const [selectedIndex, setSelectedIndex] = useState(0)
  const summary = useQuery({ queryKey: ['sentinel-visual', runId], queryFn: () => api(`/runs/${runId}/visual-summary`) })
  if (summary.isPending) return <Loading text="Cargando métricas de esta ejecución…"/>
  if (summary.error) return <ErrorState error={summary.error} retry={() => summary.refetch()}/>
  const data = summary.data, indicators = data.indicators || {}, columns: RecordData[] = data.columns || [], evolution: RecordData[] = data.evolution || []
  const selected = evolution[selectedIndex] || evolution[0]
  return <div className="sentinel-visual-summary">
    <p className="muted">Versión {data.dataset_version} · <span className="mono">{data.dataset_version_id}</span> · observada {date(data.observed_at)}</p>
    <Notice>Los controles conformes no equivalen a filas válidas. Las reglas pueden superponerse; sus incumplimientos no se suman como filas distintas.</Notice>
    <div className="run-metrics">{[['Filas de la versión', 'row_count'], ['Columnas', 'column_count'], ['Filas duplicadas exactas', 'duplicate_row_count'], ['Controles conformes', 'conforming_checks']].map(([title, key]) => <div key={key}><span>{title}</span><strong>{exact(indicators[key])}</strong></div>)}</div>
    <div className="sentinel-chart-grid"><ColumnBars columns={columns} metric="null_count" title="Nulos por columna"/><ColumnBars columns={columns} metric="distinct_count" title="Diversidad por columna"/></div>
    <p className="muted">La diversidad cuenta valores distintos sin nulos. La repetición de categorías no implica unicidad incumplida. Las filas duplicadas cuentan las adicionales a la primera de cada grupo idéntico, con nulos iguales.</p>
    <h3>Valores exactos del perfil</h3><div className="table-scroll"><table><thead><tr><th>Columna</th><th>Tipo</th><th>Nulos</th><th>Población</th><th>Valores distintos</th><th>Sin nulos</th><th>Diversidad (ratio)</th><th>Unicidad (ratio)</th></tr></thead><tbody>{columns.map(column => <tr key={column.name}><td className="mono">{column.name}</td><td>{column.logical_type}</td><td>{exact(column.null_count)}</td><td>{exact(column.null_denominator)}</td><td>{exact(column.distinct_count)}</td><td>{exact(column.distinct_denominator)}</td><td>{exact(column.distinct_rate)}</td><td>{exact(column.uniqueness_ratio)}</td></tr>)}</tbody></table></div>
    <h3>Resultados de controles y observaciones</h3>{!data.checks?.length ? <Empty title="Sin controles completados" description="Las mediciones estarán disponibles al terminar la ejecución."/> : <div className="table-scroll"><table><thead><tr><th>Control</th><th>Tipo</th><th>Estado</th><th>Observado</th><th>Esperado</th><th>Detalle</th></tr></thead><tbody>{(data.checks as RecordData[]).map((check, index) => <tr key={`${check.code}-${index}`}><td>{check.name}</td><td>{check.kind === 'OBSERVATION' ? 'Observación' : 'Control'}</td><td>{check.kind === 'OBSERVATION' ? 'Informativo' : <Badge value={check.status}/>}</td><td>{check.actual == null ? 'No disponible' : scalar(check.actual)}</td><td>{scalar(check.expected)}</td><td>{check.message}</td></tr>)}</tbody></table></div>}
    <h3>Evolución compatible hasta esta ejecución</h3>{!evolution.length ? <p className="muted">No hay métricas históricas comparables.</p> : <><Field label="Métrica de evolución"><select value={Math.min(selectedIndex, evolution.length - 1)} onChange={event => setSelectedIndex(Number(event.target.value))}>{evolution.map((item, index) => <option value={index} key={index}>{item.metric_key} · {item.method} v{item.metric_definition_version}</option>)}</select></Field>{selected && <Evolution series={selected}/>}</>}
    {data.evolution_truncated && <Notice>La ventana incluye hasta {data.evolution_limit} muestras de métricas. El historial anterior queda fuera de este gráfico.</Notice>}
    <details><summary>Tabla de métricas exactas de la ejecución</summary><div className="table-scroll"><table><thead><tr><th>Métrica</th><th>Dimensiones</th><th>Valor exacto</th><th>Método</th><th>Definición</th></tr></thead><tbody>{(data.exact_metrics || []).map((item: RecordData, index: number) => <tr key={index}><td>{item.metric_key}</td><td>{JSON.stringify(item.dimensions)}</td><td className="mono">{exact(item.exact_value)}</td><td>{item.method}</td><td>{item.metric_definition_version}</td></tr>)}</tbody></table></div></details>
  </div>
}
