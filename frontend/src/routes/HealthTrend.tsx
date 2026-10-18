import { useId, useState } from 'react'
import type { RecordData } from '../api/client'
import { Empty, number } from '../components/ui'

const series = [
  { key: 'overall', label: 'General', color: '#1a3026', meaning: 'Media ponderada de las métricas comparables del universo filtrado. Seleccionar una serie no cambia esta fórmula.' },
  { key: 'intake', label: 'Data Intake', color: '#08786f', meaning: 'Porcentaje de aceptación, ponderado por registros. Un proceso completado puede contener datos rechazados.' },
  { key: 'recon', label: 'ReconOps', color: '#377f94', meaning: 'Porcentaje de coincidencia, ponderado por registros conciliados.' },
  { key: 'sentinel', label: 'Sentinel', color: '#578247', meaning: 'Puntuación de controles comparables, ponderada por controles. Se excluyen los monitores de conteos y los automáticos sin métrica comparable.' },
] as const

export function healthCoordinates(points: RecordData[], key: string) {
  const ordered = points.map((point): RecordData & { timestamp: number } => ({ ...point, timestamp: Date.parse(`${point.date}T00:00:00Z`) }))
    .filter(point => Number.isFinite(point.timestamp)).sort((a, b) => a.timestamp - b.timestamp)
  const start = ordered[0]?.timestamp || 0, span = (ordered.at(-1)?.timestamp || start) - start
  const segments: { x: number; y: number; point: RecordData; value: number }[][] = []
  let previous: number | null = null
  for (const point of ordered) {
    const value = point[key] == null ? null : Number(point[key])
    if (value == null || !Number.isFinite(value)) { previous = null; continue }
    if (previous == null || point.timestamp - previous > 86400000) segments.push([])
    segments[segments.length - 1].push({ x: span ? 44 + (point.timestamp - start) * 698 / span : 393,
      y: 18 + (100 - Math.max(0, Math.min(100, value))) * 178 / 100, point, value })
    previous = point.timestamp
  }
  return { segments, ordered }
}

function chartDate(value: unknown) {
  const parsed = Date.parse(`${String(value)}T00:00:00Z`)
  return Number.isFinite(parsed) ? new Date(parsed).toLocaleDateString('es-CO', { timeZone: 'UTC', day: '2-digit', month: 'short', year: 'numeric' }) : '—'
}

export default function HealthTrend({ points, overall, modules, globalModule }: {
  points: RecordData[]; overall: unknown; modules: RecordData[]; globalModule: string
}) {
  const [selected, setSelected] = useState('overall'), selectorId = useId()
  const item = series.find(value => value.key === selected)!
  const { segments, ordered } = healthCoordinates(points, selected)
  const excluded = selected !== 'overall' && !!globalModule && globalModule !== selected
  const score = selected === 'overall' ? overall : modules.find(module => module.module === selected)?.health_score
  function description(point: RecordData, value: number) {
    const count = point.comparable_runs?.[selected]
    const failures = point.technical_failures?.[selected]
    return `${chartDate(point.date)} (UTC) · ${number(value)}% · ${count == null ? 'Cantidad de ejecuciones no disponible' : `${number(count)} ejecuciones consideradas`}${failures == null ? '' : `, ${number(failures)} fallos técnicos con contribución de 0`} · ${item.meaning}`
  }
  return <section className="panel health-panel" aria-label="Gráfica de salud">
    <div className="panel-heading"><div><h2>Evolución temporal · {item.label}</h2><p>{item.meaning}</p></div><strong aria-label={`Salud de ${item.label}`}>{!excluded && score != null && Number.isFinite(Number(score)) ? `${number(score)}%` : 'Sin mediciones comparables'}</strong></div>
    <fieldset className="health-series-selector"><legend>Serie de esta gráfica</legend>{series.map(option => <label key={option.key}><input type="radio" name={selectorId} value={option.key} checked={selected === option.key} onChange={() => setSelected(option.key)}/>{option.label}</label>)}</fieldset>
    {excluded ? <p role="status">Los filtros globales excluyen {item.label}. Cambia el filtro global de módulo para consultar esta serie.</p> : !segments.length ? <Empty title="Sin mediciones comparables" description="Las métricas ausentes no representan un 100 %. Ajusta el periodo o los filtros para consultar mediciones compatibles."/> : <div className="health-trend">
      <svg viewBox="0 0 760 230" role="img" aria-label={`Evolución de salud · ${item.label}`}><title>{item.label} · fechas reales; las interrupciones indican días sin mediciones comparables</title>{[0, 25, 50, 75, 100].map(value => <g key={value}><line x1="44" x2="742" y1={18 + (100 - value) * 1.78} y2={18 + (100 - value) * 1.78} className="chart-grid-line"/><text x="36" y={22 + (100 - value) * 1.78} textAnchor="end" className="chart-axis-label">{value}%</text></g>)}{segments.map((segment, index) => <g key={index}><polyline data-testid="health-segment" points={segment.map(point => `${point.x},${point.y}`).join(' ')} fill="none" stroke={item.color} strokeWidth="3"/>{segment.map(({ x, y, point, value }) => <circle key={String(point.date)} cx={x} cy={y} r="4" fill="#fff" stroke={item.color} strokeWidth="2" tabIndex={0} aria-label={description(point, value)}><title>{description(point, value)}</title></circle>)}</g>)}<text x="44" y="222" className="chart-axis-label">{chartDate(ordered[0]?.date)}</text><text x="742" y="222" textAnchor="end" className="chart-axis-label">{chartDate(ordered.at(-1)?.date)}</text></svg>
      <p className="muted">Fechas agrupadas en UTC. Los espacios sin puntos indican ausencia de mediciones comparables; no se interpola una tendencia. Por el contrato histórico, los fallos técnicos contribuyen con 0 a la salud: ese cero no demuestra un porcentaje de aceptación o coincidencia observado.</p>
      <details><summary>Consultar puntos y ejecuciones consideradas</summary><ul>{segments.flat().map(({ point, value }) => <li key={String(point.date)}>{description(point, value)}</li>)}</ul></details>
    </div>}
  </section>
}
