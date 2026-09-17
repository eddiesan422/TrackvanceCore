import { useQuery } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import {
  ArrowDown,
  ArrowDownToLine,
  ArrowRight,
  ArrowUp,
  ArrowUpRight,
  CheckCheck,
  CircleAlert,
  Database,
  FileCheck2,
  GitCompareArrows,
  Minus,
  Play,
  Plus,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
  SlidersHorizontal,
} from 'lucide-react'
import { api } from '../api/client'
import type { RecordData } from '../api/client'
import { Badge, date, Empty, ErrorState, label, Loading, number, PageHeading } from '../components/ui'
import { useSession } from '../app/session'

type DashboardFilterKey = 'period' | 'dataset_id' | 'module' | 'status' | 'criticality'

const periodLabels: Record<string, string> = {
  '7d': 'Últimos 7 días',
  '30d': 'Últimos 30 días',
  '90d': 'Últimos 90 días',
  all: 'Todo el historial',
}

const operationalLabels: Record<string, string> = {
  ATTENTION: 'Requiere atención',
  HEALTHY: 'Saludable',
  IN_PROGRESS: 'En curso',
  TECHNICAL_FAILURE: 'Fallo técnico',
  NO_DATA: 'Sin datos',
}

const moduleDefinitions = [
  { key: 'intake', label: 'Data Intake', icon: ArrowDownToLine, destination: '/intake', issueLabel: 'fallos y rechazos' },
  { key: 'recon', label: 'ReconOps', icon: GitCompareArrows, destination: '/recon', issueLabel: 'diferencias y fallos' },
  { key: 'sentinel', label: 'Sentinel', icon: ShieldCheck, destination: '/sentinel', issueLabel: 'alertas y fallos' },
]

function formatDuration(value: unknown) {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  const seconds = Math.max(0, Math.round(Number(value)))
  if (seconds < 1) return '<1 s'
  if (seconds < 60) return `${seconds} s`
  const minutes = Math.floor(seconds / 60)
  return `${minutes} min ${String(seconds % 60).padStart(2, '0')} s`
}

function Variation({ value, inverse = false, points = false }: { value?: RecordData | null; inverse?: boolean; points?: boolean }) {
  if (!value || !Number.isFinite(Number(value.delta))) return <span className="metric-variation neutral"><Minus size={12}/> Sin periodo comparable</span>
  const delta = Number(value.delta)
  const improved = inverse ? delta < 0 : delta > 0
  const tone = delta === 0 ? 'neutral' : improved ? 'positive' : 'negative'
  const Icon = delta > 0 ? ArrowUp : delta < 0 ? ArrowDown : Minus
  return <span className={`metric-variation ${tone}`} title={`Valor anterior: ${number(value.previous)}${points ? '%' : ''}`}><Icon size={12}/>{delta > 0 ? '+' : ''}{number(delta)}{points ? ' pp' : ''} frente al periodo anterior</span>
}

function HealthValue({ value }: { value: unknown }) {
  if (value == null || !Number.isFinite(Number(value))) return <span className="health-value empty">Sin datos</span>
  const score = Math.max(0, Math.min(100, Number(value)))
  const tone = score >= 90 ? 'healthy' : score >= 75 ? 'warning' : 'critical'
  return <div className={`health-value ${tone}`}><strong>{number(score)}%</strong><span><i style={{ width: `${score}%` }}/></span></div>
}

function Trend({ value }: { value: unknown }) {
  if (value == null || !Number.isFinite(Number(value))) return <span className="trend neutral"><Minus size={12}/> Sin tendencia</span>
  const delta = Number(value)
  const Icon = delta > 0 ? ArrowUp : delta < 0 ? ArrowDown : Minus
  return <span className={`trend ${delta > 0 ? 'positive' : delta < 0 ? 'negative' : 'neutral'}`}><Icon size={12}/>{delta > 0 ? '+' : ''}{number(delta)} pp</span>
}

function chartDate(value: unknown) {
  const [year, month, day] = String(value || '').split('-').map(Number)
  if (!year || !month || !day) return '—'
  return new Date(year, month - 1, day).toLocaleDateString('es-CO', { day: '2-digit', month: 'short' })
}

function HealthTrend({ points }: { points: RecordData[] }) {
  if (!points.length) return <Empty title="Aún no hay evolución disponible" description="Ejecuta controles durante el periodo seleccionado para construir la tendencia."/>
  const width = 760, height = 230, left = 44, right = 18, top = 18, bottom = 34
  const plotWidth = width - left - right, plotHeight = height - top - bottom
  const x = (index: number) => points.length === 1 ? left + plotWidth / 2 : left + index * plotWidth / (points.length - 1)
  const y = (score: number) => top + (100 - Math.max(0, Math.min(100, score))) * plotHeight / 100
  const series = [
    { key: 'overall', label: 'General', color: '#294d5d', width: 3 },
    { key: 'intake', label: 'Data Intake', color: '#168e7c', width: 2 },
    { key: 'recon', label: 'ReconOps', color: '#6f91b8', width: 2 },
    { key: 'sentinel', label: 'Sentinel', color: '#9b82b7', width: 2 },
  ]
  return <div className="health-trend"><div className="chart-legend">{series.map(item => <span key={item.key}><i style={{ background: item.color }}/>{item.label}</span>)}</div><svg viewBox={`0 0 ${width} ${height}`} role="img" aria-label="Evolución de salud por módulo"><title>Evolución de la salud general, Data Intake, ReconOps y Sentinel</title>{[0, 25, 50, 75, 100].map(score => <g key={score}><line x1={left} x2={width - right} y1={y(score)} y2={y(score)} className="chart-grid-line"/><text x={left - 8} y={y(score) + 4} textAnchor="end" className="chart-axis-label">{score}%</text></g>)}{series.map(item => { const values = points.map((point, index) => ({ index, value: point[item.key] == null ? null : Number(point[item.key]) })).filter(point => point.value != null && Number.isFinite(point.value)); return <g key={item.key}><polyline points={values.map(point => `${x(point.index)},${y(Number(point.value))}`).join(' ')} fill="none" stroke={item.color} strokeWidth={item.width} strokeLinecap="round" strokeLinejoin="round"/>{values.map(point => <circle key={point.index} cx={x(point.index)} cy={y(Number(point.value))} r={item.key === 'overall' ? 3.5 : 2.5} fill="#fff" stroke={item.color} strokeWidth="2"><title>{item.label}: {number(point.value)}% · {chartDate(points[point.index].date)}</title></circle>)}</g>})}<text x={left} y={height - 8} className="chart-axis-label">{chartDate(points[0].date)}</text><text x={width - right} y={height - 8} textAnchor="end" className="chart-axis-label">{chartDate(points.at(-1)?.date)}</text></svg></div>
}

export function RunsTable({ runs, compact = false }: { runs: RecordData[]; compact?: boolean }) {
  return !runs.length ? <Empty title="Aún no hay ejecuciones" description="Ajusta los filtros o ejecuta un control para ver aquí sus resultados."/> : <div className="table-scroll"><table><thead><tr><th>Ejecución / control</th><th>Dataset</th><th>Módulo</th><th>Estado técnico / resultado</th><th>Registros</th>{compact && <><th>Hallazgos</th><th>Duración</th></>}<th>Fecha</th><th/></tr></thead><tbody>{runs.map(run => <tr key={run.id}><td><Link to={`/runs/${run.id}`} className="table-primary">{run.name || run.control_name || run.contract_name || run.monitor_name || `${label(run.module)} · ${String(run.id).slice(0, 8)}`}</Link><small className="table-subtitle">{run.display_id || String(run.id).slice(0, 12)}</small></td><td>{run.dataset_id ? <Link className="table-primary" to={`/datasets/${run.dataset_id}`}>{run.dataset_name || 'Dataset'}</Link> : <span>{run.dataset_name || '—'}</span>}{run.target_dataset_name && <small className="table-subtitle">vs. {run.target_dataset_name}</small>}</td><td><span className={`module-label ${run.module}`}>{label(run.module)}</span></td><td><div className="run-state-stack"><Badge value={run.status}/>{run.decision && <Badge value={run.decision}/>}</div></td><td className="tabular">{number(run.processed_records ?? run.metrics?.total_rows ?? run.metrics?.source_rows ?? run.metrics?.row_count ?? run.row_count)}</td>{compact && <><td className="tabular">{number(run.finding_count)}</td><td className="no-wrap">{formatDuration(run.duration_seconds)}</td></>}<td className="muted no-wrap">{date(run.created_at)}</td><td><Link to={`/runs/${run.id}`} className="icon-button" aria-label={`Ver ejecución ${run.name || run.id}`}><ArrowUpRight size={17}/></Link></td></tr>)}</tbody></table></div>
}

export default function Dashboard() {
  const session = useSession()
  const [searchParams, setSearchParams] = useSearchParams()
  const filters: Record<DashboardFilterKey, string> = {
    period: searchParams.get('period') || '30d',
    dataset_id: searchParams.get('dataset_id') || '',
    module: searchParams.get('module') || '',
    status: searchParams.get('status') || '',
    criticality: searchParams.get('criticality') || '',
  }
  const requestParams = new URLSearchParams({ period: filters.period })
  for (const key of ['dataset_id', 'module', 'status', 'criticality'] as DashboardFilterKey[]) if (filters[key]) requestParams.set(key, filters[key])
  const dashboard = useQuery({
    queryKey: ['dashboard', requestParams.toString()],
    queryFn: () => api(`/dashboard?${requestParams}`),
    refetchInterval: 20_000,
    placeholderData: previous => previous,
  })
  const setFilter = (key: DashboardFilterKey, value: string) => {
    const next = new URLSearchParams(searchParams)
    if (!value || (key === 'period' && value === '30d')) next.delete(key)
    else next.set(key, value)
    setSearchParams(next, { replace: true })
  }
  const resetFilters = () => setSearchParams(new URLSearchParams(), { replace: true })
  if (dashboard.isPending) return <Loading/>
  if (dashboard.error) return <ErrorState error={dashboard.error} retry={() => dashboard.refetch()}/>

  const data = dashboard.data, stats = data.stats || {}, variations = data.variations || {}
  const attention: RecordData[] = data.attention || [], affected: RecordData[] = data.datasets_attention || [], runs: RecordData[] = data.recent_runs || []
  const filterOptions = data.filter_options || {}, hasCustomFilters = Object.entries(filters).some(([key, value]) => value && !(key === 'period' && value === '30d'))
  const firstAttention = attention[0]
  const metrics = [
    { title: 'Salud general', value: stats.health_score == null ? '—' : `${number(stats.health_score)}%`, context: 'Resultado ponderado de los controles', icon: ShieldCheck, href: '/runs', tone: 'teal', variation: variations.health_score, points: true, inverse: false },
    { title: 'Controles con fallo', value: stats.controls_failed, context: 'Fallos técnicos o resultados no conformes', icon: ShieldAlert, href: '/runs', tone: 'red', variation: variations.controls_failed, points: false, inverse: true },
    { title: 'Excepciones abiertas', value: stats.open_exceptions, context: 'Casos activos que requieren gestión', icon: CircleAlert, href: '/exceptions', tone: 'amber', variation: variations.open_exceptions, points: false, inverse: true },
    { title: 'Datasets afectados', value: stats.affected_datasets, context: 'Fuentes vinculadas a problemas activos', icon: Database, href: '/datasets', tone: 'blue', variation: variations.affected_datasets, points: false, inverse: true },
  ]

  return <><PageHeading eyebrow="SUPERVISIÓN OPERATIVA" title="Centro de control" description={`Hola, ${session?.user.name.split(' ')[0]}. Prioriza problemas, revisa la salud y actúa desde un solo lugar.`} action={<><Link to="/datasets?upload=1" className="button secondary"><Plus size={17}/> Cargar dataset</Link><Link to="/recon?new=1" className="button primary"><Play size={15}/> Nueva ejecución</Link></>}/>
    <section className="dashboard-filters" aria-label="Filtros globales"><div className="dashboard-filter-title"><SlidersHorizontal size={17}/><div><strong>Vista operativa</strong><span>{dashboard.isFetching ? 'Actualizando datos…' : 'Filtros aplicados a todo el cockpit'}</span></div></div><label><span>Periodo</span><select aria-label="Filtrar por periodo" value={filters.period} onChange={event => setFilter('period', event.target.value)}>{(filterOptions.periods || ['7d', '30d', '90d', 'all']).map((period: string) => <option key={period} value={period}>{periodLabels[period] || period}</option>)}</select></label><label><span>Dataset</span><select aria-label="Filtrar por dataset" value={filters.dataset_id} onChange={event => setFilter('dataset_id', event.target.value)}><option value="">Todos los datasets</option>{(filterOptions.datasets || []).map((dataset: RecordData) => <option key={dataset.id} value={dataset.id}>{dataset.name}</option>)}</select></label><label><span>Módulo</span><select aria-label="Filtrar por módulo" value={filters.module} onChange={event => setFilter('module', event.target.value)}><option value="">Todos los módulos</option><option value="intake">Data Intake</option><option value="recon">ReconOps</option><option value="sentinel">Sentinel</option></select></label><label><span>Estado</span><select aria-label="Filtrar por estado" value={filters.status} onChange={event => setFilter('status', event.target.value)}><option value="">Todos los estados</option><option value="ATTENTION">Requiere atención</option><option value="HEALTHY">Saludable</option><option value="IN_PROGRESS">En curso</option><option value="TECHNICAL_FAILURE">Fallo técnico</option></select></label><label><span>Criticidad</span><select aria-label="Filtrar por criticidad" value={filters.criticality} onChange={event => setFilter('criticality', event.target.value)}><option value="">Todas</option><option value="CRITICAL">Crítica</option><option value="HIGH">Alta</option><option value="MEDIUM">Media</option><option value="LOW">Baja</option></select></label><button className="icon-button filter-reset" type="button" aria-label="Restablecer filtros" title="Restablecer filtros" disabled={!hasCustomFilters} onClick={resetFilters}><RotateCcw size={16}/></button></section>

    <section className="metric-grid operational-metrics" aria-label="Indicadores operativos">{metrics.map(metric => <Link to={metric.href} className={`metric-card ${metric.tone}`} key={metric.title}><div className="metric-top"><span>{metric.title}</span><span className={`metric-icon ${metric.tone}`}><metric.icon size={18}/></span></div><div className="metric-value">{typeof metric.value === 'number' ? number(metric.value) : metric.value}<ArrowUpRight size={18}/></div><div className="metric-context">{metric.context}</div><Variation value={metric.variation} inverse={metric.inverse} points={metric.points}/></Link>)}</section>

    <section className="panel cockpit-attention"><div className="panel-heading"><div><span className="section-kicker">PRIORIDAD OPERATIVA</span><h2>Requiere tu atención</h2><p>Problemas ordenados por criticidad y fecha para actuar sin perder contexto.</p></div><span className={`attention-total ${attention.length ? 'has-items' : ''}`}>{number(data.attention_total || 0)} pendientes</span></div>{!attention.length ? <div className="all-clear"><CheckCheck size={32}/><strong>Sin problemas para los filtros seleccionados</strong><p>No hay hallazgos, excepciones ni fallos técnicos que requieran acción.</p></div> : <div className="table-scroll"><table className="attention-table"><thead><tr><th>Criticidad</th><th>Dataset</th><th>Módulo</th><th>Problema</th><th>Detectado</th><th>Acción</th></tr></thead><tbody>{attention.map(item => <tr key={`${item.subject_type}-${item.id}`}><td><Badge value={item.criticality}/></td><td>{item.dataset_id ? <Link className="table-primary" to={`/datasets/${item.dataset_id}`}>{item.dataset_name}</Link> : item.dataset_name}</td><td><span className={`module-label ${item.module}`}>{label(item.module)}</span></td><td><Link className="problem-link" to={item.href}><strong>{item.problem}</strong><small>{item.detail}</small></Link></td><td className="muted no-wrap">{date(item.date)}</td><td><Link className="button secondary small no-wrap" to={item.href}>{item.action_label}<ArrowRight size={14}/></Link></td></tr>)}</tbody></table></div>}{firstAttention && <div className="attention-direct"><CircleAlert size={16}/><span>Mayor prioridad: <strong>{firstAttention.dataset_name}</strong> · {firstAttention.problem}</span><Link to={firstAttention.href}>Atender ahora <ArrowRight size={14}/></Link></div>}</section>

    <div className="section-label cockpit-section-label"><div><span className="section-kicker">EVOLUCIÓN Y COBERTURA</span><h2>Salud de los datos</h2></div><span>{periodLabels[filters.period]}</span></div>
    <section className="panel health-panel"><div className="panel-heading"><div><h2>Evolución temporal</h2><p>Salud general y resultado comparable de cada módulo.</p></div><HealthValue value={stats.health_score}/></div><HealthTrend points={data.health_history || []}/></section>

    <section className="operational-module-grid" aria-label="Resumen operativo por módulo">{moduleDefinitions.map(module => { const status = (data.module_status || []).find((item: RecordData) => item.module === module.key) || {}; return <Link to={module.destination} className={`operational-module-card ${module.key}`} key={module.key}><div className="operational-module-head"><span className="module-icon"><module.icon size={23}/></span><div><small>{module.label}</small><strong>{number(status.activity_count || 0)} {status.activity_label || 'ejecuciones'}</strong></div><span className={`operational-state ${String(status.status || 'NO_DATA').toLowerCase()}`}>{operationalLabels[status.status] || status.status || 'Sin datos'}</span></div><div className="module-operational-stats"><div><span>Salud</span><strong>{status.health_score == null ? '—' : `${number(status.health_score)}%`}</strong></div><div><span>{module.issueLabel}</span><strong className={status.issues ? 'danger-text' : ''}>{number(status.issues || 0)}</strong></div><div><span>Registros procesados</span><strong>{number(status.processed_records || 0)}</strong></div></div><div className="module-operational-foot"><span>{status.latest_run_at ? `Última ejecución ${date(status.latest_run_at, false)}` : 'Sin ejecuciones en el periodo'}</span><ArrowRight size={15}/></div></Link>})}</section>

    <section className="panel datasets-attention-panel"><div className="panel-heading"><div><span className="section-kicker">FUENTES AFECTADAS</span><h2>Datasets que necesitan atención</h2><p>Salud, hallazgos y tendencia de las fuentes con problemas activos.</p></div><Link className="text-link" to="/datasets">Ver todos <ArrowRight size={14}/></Link></div>{!affected.length ? <div className="all-clear compact"><CheckCheck size={28}/><strong>Sin datasets afectados</strong><p>Los datasets visibles no presentan problemas activos.</p></div> : <div className="table-scroll"><table><thead><tr><th>Dataset</th><th>Criticidad</th><th>Salud</th><th>Hallazgos</th><th>Excepciones</th><th>Última ejecución</th><th>Tendencia</th><th/></tr></thead><tbody>{affected.map(dataset => <tr key={dataset.dataset_id}><td><Link to={dataset.href} className="table-primary">{dataset.dataset_name}</Link><small className="table-subtitle">{dataset.domain}</small></td><td><Badge value={dataset.criticality}/></td><td><HealthValue value={dataset.health_score}/></td><td className="tabular">{number(dataset.findings)}</td><td className="tabular">{number(dataset.open_exceptions)}</td><td><Link className="table-primary" to={dataset.action_href}>{label(dataset.last_run?.module)} · {label(dataset.last_run?.decision || dataset.last_run?.status)}</Link><small className="table-subtitle">{date(dataset.last_run?.created_at)}</small></td><td><Trend value={dataset.trend_delta}/></td><td><Link to={dataset.action_href} className="icon-button" aria-label={`Revisar última ejecución de ${dataset.dataset_name}`}><ArrowUpRight size={17}/></Link></td></tr>)}</tbody></table></div>}</section>

    <section className="panel recent-panel cockpit-recent"><div className="panel-heading"><div><span className="section-kicker">ACTIVIDAD RECIENTE</span><h2>Ejecuciones recientes</h2><p>Dataset, volumen, hallazgos y duración junto al estado técnico y de negocio.</p></div><Link className="text-link" to="/runs">Ver historial <ArrowRight size={14}/></Link></div><RunsTable runs={runs} compact/><div className="panel-bottom"><FileCheck2 size={15}/><span>Cada fila navega a su resultado y evidencia; el dataset conserva su vínculo directo.</span></div></section>

    <section className="cockpit-quick-actions" aria-label="Acciones rápidas"><div><strong>Actúa desde aquí</strong><span>Inicia el siguiente paso sin salir del cockpit.</span></div><Link to="/datasets?upload=1"><Plus size={15}/> Cargar dataset</Link><Link to="/intake?new=1"><ArrowDownToLine size={15}/> Validar datos</Link><Link to="/recon?new=1"><GitCompareArrows size={15}/> Conciliar</Link><Link to="/sentinel?new=1"><ShieldCheck size={15}/> Evaluar salud</Link></section>
  </>
}
