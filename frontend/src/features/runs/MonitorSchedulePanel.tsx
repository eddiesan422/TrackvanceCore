import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { CalendarClock, RefreshCw } from 'lucide-react'
import { api, post } from '../../api/client'
import type { Collection, RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Notice, number } from '../../components/ui'
import './monitor-schedule.css'

type Schedule = {
  id: string; version: number; enabled: boolean; interval_seconds: number;
  next_run_at: string; starts_at: string;
}
type MetricPoint = { run_id: string; configuration_id: string; dataset_version_id: string; observed_at: string; value: number | null; exact_value?: string | null; decision: string }
type MetricSeries = { metric_key: string; dimensions: Record<string, unknown>; method: string; metric_definition_version: number; points: MetricPoint[] }
type SeriesResponse = { items: MetricSeries[]; sample_count: number; limit: number }

function ScheduleEditor({ monitorId, schedule }: { monitorId: string; schedule: Schedule | null }) {
  const cache = useQueryClient()
  const canConfigure = usePermission('configurations:write'), canExecute = usePermission('runs:execute')
  const [minutes, setMinutes] = useState(String((schedule?.interval_seconds || 3600) / 60))
  const [enabled, setEnabled] = useState(schedule?.enabled ?? true), [start, setStart] = useState('')
  const save = useMutation({
    mutationFn: () => post(`/monitors/${monitorId}/schedule`, {
      interval_seconds: Math.round(Number(minutes) * 60), enabled,
      starts_at: start ? new Date(start).toISOString() : null,
      expected_version: schedule?.version ?? null,
    }),
    onSuccess: () => { cache.invalidateQueries({ queryKey: ['monitor-schedule', monitorId] }); cache.invalidateQueries({ queryKey: ['audit'] }) },
  })
  return <form className="form-stack" onSubmit={event => { event.preventDefault(); save.mutate() }}>
    <div className="schedule-fields">
      <Field label="Periodicidad (minutos)"><input type="number" min="1" max="44640" step="1" required value={minutes} onChange={event => setMinutes(event.target.value)} disabled={!canConfigure}/></Field>
      <Field label="Primera ejecución (hora local)" hint="Vacío: al guardar. Cada cambio inicia una nueva periodicidad."><input type="datetime-local" value={start} onChange={event => setStart(event.target.value)} disabled={!canConfigure}/></Field>
      <label className="schedule-enabled"><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} disabled={!canConfigure || !canExecute}/> Programación activa</label>
    </div>
    <Notice>Evalúa el último snapshot registrado. Para incluir cambios de una base externa, actualiza primero su dataset. Los intervalos atrasados se agrupan y se omite un intervalo si el monitor sigue ejecutándose.</Notice>
    {schedule && <p className="muted">Revisión {schedule.version} · {schedule.enabled ? `Próxima ejecución: ${date(schedule.next_run_at)}` : 'Programación pausada'}</p>}
    {save.error && <ErrorState error={save.error}/>}
    <div><button className="button secondary small" disabled={!canConfigure || (enabled && !canExecute) || save.isPending || !minutes}>{save.isPending ? 'Guardando…' : 'Guardar programación'}</button></div>
  </form>
}

function MetricEvolution({ series }: { series: MetricSeries }) {
  const numeric = series.points.filter((point): point is MetricPoint & { value: number } => typeof point.value === 'number' && Number.isFinite(point.value) && Number.isFinite(Date.parse(point.observed_at))).sort((a, b) => Date.parse(a.observed_at) - Date.parse(b.observed_at))
  const values = numeric.map(point => point.value), low = Math.min(...values), high = Math.max(...values)
  const firstTime = Date.parse(numeric[0]?.observed_at || ''), lastTime = Date.parse(numeric.at(-1)?.observed_at || '')
  const range = high - low || 1, timeRange = lastTime - firstTime || 1
  const x = (point: MetricPoint) => 55 + (Date.parse(point.observed_at) - firstTime) * 510 / timeRange
  const y = (point: MetricPoint & { value: number }) => 145 - (point.value - low) * 115 / range
  const points = numeric.map(point => `${x(point)},${y(point)}`).join(' ')
  return <div className="metric-evolution">
    <p className="muted">Método {series.method} · definición v{series.metric_definition_version} · {numeric.length} observaciones comparables</p>
    {numeric.length > 1 && <><svg viewBox="0 0 600 170" role="img" aria-label={`Evolución de ${series.metric_key}`}><line x1="55" y1="145" x2="565" y2="145" className="metric-axis"/><text x="45" y="149" textAnchor="end">{number(low)}</text>{high !== low && <text x="45" y="34" textAnchor="end">{number(high)}</text>}<polyline points={points}/>{numeric.map((point, index) => <a key={`${point.run_id}-${index}`} href={`/runs/${point.run_id}`}><circle cx={x(point)} cy={y(point)} r="4"><title>{date(point.observed_at)}: {number(point.value)}</title></circle></a>)}</svg><div className="metric-time-range"><span>{date(numeric[0].observed_at)}</span><span>{date(numeric.at(-1)!.observed_at)}</span></div></>}
    <div className="table-scroll"><table><thead><tr><th>Fecha</th><th>Valor observado</th><th>Resultado</th><th>Evidencia</th></tr></thead><tbody>{series.points.slice(-20).reverse().map((point, index) => <tr key={`${point.run_id}-${index}`}><td>{date(point.observed_at)}</td><td>{point.exact_value ?? (point.value == null ? 'No disponible' : number(point.value))}</td><td><Badge value={point.decision}/></td><td><Link to={`/runs/${point.run_id}`}>Ver ejecución</Link></td></tr>)}</tbody></table></div>
  </div>
}

function ScheduleContent({ monitorId }: { monitorId: string }) {
  const cache = useQueryClient()
  const [seriesIndex, setSeriesIndex] = useState(0)
  const schedule = useQuery({ queryKey: ['monitor-schedule', monitorId], queryFn: () => api<Schedule | null>(`/monitors/${monitorId}/schedule`) })
  const occurrences = useQuery({ queryKey: ['monitor-occurrences', monitorId], queryFn: () => api<Collection>(`/monitors/${monitorId}/occurrences`), refetchInterval: 15000 })
  const series = useQuery({ queryKey: ['monitor-series', monitorId], queryFn: () => api<SeriesResponse>(`/monitors/${monitorId}/series`), refetchInterval: 15000 })
  const alerts = useQuery({ queryKey: ['monitor-alerts', monitorId], queryFn: () => api<Collection>(`/monitors/${monitorId}/alerts`), refetchInterval: 15000 })
  const selected = series.data?.items[seriesIndex] || series.data?.items[0]
  return <div className="monitor-schedule-content">
    <h4>Programación local</h4>
    {schedule.isPending ? <Loading text="Cargando programación…"/> : schedule.error ? <ErrorState error={schedule.error} retry={() => schedule.refetch()}/> : <ScheduleEditor key={schedule.data?.version || 'new'} monitorId={monitorId} schedule={schedule.data}/>}
    <div className="schedule-section-title"><h4>Intervalos y ejecuciones</h4><button className="text-button" onClick={() => { occurrences.refetch(); series.refetch(); alerts.refetch(); schedule.refetch(); cache.invalidateQueries({ queryKey: ['configurations', 'sentinel'] }); cache.invalidateQueries({ queryKey: ['runs', 'sentinel'] }) }}><RefreshCw size={13}/> Actualizar</button></div>
    {occurrences.isPending ? <Loading/> : occurrences.error ? <ErrorState error={occurrences.error} retry={() => occurrences.refetch()}/> : !occurrences.data.items.length ? <p className="muted">Aún no hay intervalos programados ejecutados.</p> : <div className="table-scroll"><table><thead><tr><th>Fecha planificada</th><th>Inicio real</th><th>Estado técnico</th><th>Resultado</th><th>Evidencia</th></tr></thead><tbody>{occurrences.data.items.map((item: RecordData) => <tr key={item.id}><td>{date(item.planned_at)}{item.coalesced_intervals > 0 && <small className="reason-code">{item.coalesced_intervals} intervalos atrasados agrupados</small>}</td><td>{date(item.started_at)}</td><td>{item.status === 'SKIPPED' ? <span>{item.reason_code === 'MONITOR_BUSY' ? 'Omitido: ejecución en curso' : 'Omitido: sin versión de dataset'}</span> : <span><Badge value={item.status}/>{item.reason_code && <small className="reason-code">{item.reason_code}</small>}</span>}</td><td>{item.decision ? <Badge value={item.decision}/> : '—'}</td><td>{item.run_id ? <Link to={`/runs/${item.run_id}`}>Ver ejecución</Link> : 'Sin ejecución'}</td></tr>)}</tbody></table></div>}
    <h4>Histórico y evolución</h4>
    {series.isPending ? <Loading/> : series.error ? <ErrorState error={series.error} retry={() => series.refetch()}/> : !series.data.items.length ? <Empty title="Sin métricas históricas" description="La primera ejecución completada iniciará las series de este monitor."/> : <><Field label="Métrica histórica"><select value={Math.min(seriesIndex, series.data.items.length - 1)} onChange={event => setSeriesIndex(Number(event.target.value))}>{series.data.items.map((item, index) => <option key={index} value={index}>{item.metric_key} · {item.method} v{item.metric_definition_version}{Object.keys(item.dimensions).length ? ` · ${JSON.stringify(item.dimensions)}` : ''}</option>)}</select></Field>{selected && <MetricEvolution series={selected}/>}<small className="muted">Hasta {series.data.limit} muestras recientes entre versiones del monitor. Los métodos y definiciones incompatibles permanecen separados.</small></>}
    <h4>Alertas internas</h4>
    {alerts.isPending ? <Loading/> : alerts.error ? <ErrorState error={alerts.error} retry={() => alerts.refetch()}/> : !alerts.data.items.length ? <p className="muted">Sin hallazgos registrados.</p> : <ul className="schedule-alerts">{alerts.data.items.slice(0, 10).map(item => <li key={item.id}><Badge value={item.severity}/><Link to={`/runs/${item.run_id}`}>{item.title}</Link><small>{date(item.created_at)}</small>{item.exception_id && <Link to={`/exceptions?id=${item.exception_id}`}>Ver excepción</Link>}</li>)}</ul>}
  </div>
}

export function MonitorSchedulePanel({ monitorId }: { monitorId: string }) {
  const [expanded, setExpanded] = useState(false)
  return <details className="monitor-schedule" onToggle={event => setExpanded(event.currentTarget.open)}><summary><CalendarClock size={15}/> Programación e histórico</summary>{expanded && <ScheduleContent monitorId={monitorId}/>}</details>
}
