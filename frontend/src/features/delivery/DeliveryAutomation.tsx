import { useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { CalendarClock, Play, Plus } from 'lucide-react'
import { api, post } from '../../api/client'
import type { Collection } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, Empty, ErrorState, Field, Loading, Notice, PageHeading, Pagination } from '../../components/ui'
import { DeliveryNavigation } from './DeliveryNavigation'
import type { DeliveryConfiguration } from './types'

type Settings = { mode: string; timezone: string; starts_at: string; interval_seconds: number; local_time: string; weekdays: number[]; source_policy: string; intake_configuration_id: string | null; allow_warnings: boolean; allow_empty: boolean; repeat_versions: boolean }
type Automation = { id: string; name: string; configuration_id: string; responsible_name: string; responsible_user_id: string; version: number; enabled: boolean; next_run_at: string | null; settings: Settings }
const modes = { ONCE: 'Una vez', INTERVAL: 'Cada intervalo', DAILY: 'Diaria', WEEKLY: 'Semanal', CHAINED: 'Después de Intake' }
const policies = { FIXED_VERSION: 'Versión fija de la configuración', LATEST_REGISTERED: 'Última versión registrada', INTAKE_OUTPUT: 'Salida concreta del Intake disparador' }

function wallTime(instant: Date, zone: string) {
  const parts = new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(instant)
  const value = (type: string) => parts.find(part => part.type === type)?.value || ''
  return `${value('year')}-${value('month')}-${value('day')}T${value('hour')}:${value('minute')}`
}

export function zonedStart(local: string, zone: string) {
  const base = Date.parse(`${local}:00Z`)
  if (!Number.isFinite(base)) throw new Error('Selecciona una fecha de inicio válida.')
  const candidates = new Set<number>()
  for (const shift of [-86400000, 0, 86400000]) {
    const probe = base + shift
    const displayed = Date.parse(`${wallTime(new Date(probe), zone)}:00Z`)
    const candidate = base - (displayed - probe)
    if (wallTime(new Date(candidate), zone) === local) candidates.add(candidate)
  }
  if (!candidates.size) throw new Error('La hora seleccionada no existe en esa zona por el cambio de horario. Elige otra hora.')
  return new Date(Math.min(...candidates)).toISOString()
}

function Editor({ item, configurations }: { item?: Automation; configurations: DeliveryConfiguration[] }) {
  const cache = useQueryClient(), navigate = useNavigate(), canSchedule = usePermission('delivery:schedule'), canIntake = usePermission('intake:read')
  const [name, setName] = useState(item?.name || ''), [configuration, setConfiguration] = useState(item?.configuration_id || configurations[0]?.id || '')
  const [enabled, setEnabled] = useState(item?.enabled ?? true), [settings, setSettings] = useState<Settings>(item?.settings || { mode: 'INTERVAL', timezone: 'America/Bogota', starts_at: '', interval_seconds: 3600, local_time: '09:00', weekdays: [0], source_policy: 'FIXED_VERSION', intake_configuration_id: null, allow_warnings: false, allow_empty: false, repeat_versions: false })
  const [start, setStart] = useState(wallTime(new Date(Date.now() + 60000), settings.timezone))
  const intake = useQuery({ queryKey: ['automation-intake-contracts'], queryFn: () => api<Collection>('/intake/contracts'), enabled: canIntake && settings.mode === 'CHAINED' })
  const update = <K extends keyof Settings>(key: K, value: Settings[K]) => setSettings(previous => ({ ...previous, [key]: value }))
  const save = useMutation({ mutationFn: () => post<Automation>(item ? `/delivery/automations/${item.id}/versions` : '/delivery/automations', { name, configuration_id: configuration, enabled, responsible_user_id: item?.responsible_user_id || null, expected_version: item?.version || null, settings: { ...settings, starts_at: zonedStart(start, settings.timezone) } }),
    onSuccess: result => { void cache.invalidateQueries({ queryKey: ['delivery-automations'] }); void cache.invalidateQueries({ queryKey: ['delivery-automation', result.id] }); navigate(`/delivery/automation/${result.id}`) } })
  return <form className="panel form-stack" onSubmit={event => { event.preventDefault(); save.mutate() }}><h2>{item ? 'Nueva revisión de la automatización' : 'Crear automatización'}</h2><div className="schedule-fields">
    <Field label="Nombre"><input required maxLength={160} value={name} onChange={event => setName(event.target.value)} disabled={!canSchedule}/></Field>
    <Field label="Configuración publicada"><select required value={configuration} onChange={event => setConfiguration(event.target.value)} disabled={!canSchedule}><option value="">Selecciona una entrega</option>{configurations.map(config => <option key={config.id} value={config.id}>{config.name} · v{config.version}</option>)}</select></Field>
    <Field label="Disparador"><select value={settings.mode} disabled={!canSchedule} onChange={event => setSettings(previous => ({ ...previous, mode: event.target.value, source_policy: event.target.value === 'CHAINED' ? 'INTAKE_OUTPUT' : 'FIXED_VERSION' }))}>{Object.entries(modes).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>
    <Field label="Zona horaria" hint="Zona IANA; la programación conserva la hora local al cambiar el horario de verano."><input required value={settings.timezone} disabled={!canSchedule} onChange={event => update('timezone', event.target.value)}/></Field>
    <Field label="Inicio en la zona seleccionada"><input type="datetime-local" required value={start} disabled={!canSchedule} onChange={event => setStart(event.target.value)}/></Field>
    {settings.mode === 'INTERVAL' && <Field label="Intervalo (minutos)"><input type="number" min={1} max={44640} required value={settings.interval_seconds / 60} disabled={!canSchedule} onChange={event => update('interval_seconds', Number(event.target.value) * 60)}/></Field>}
    {['DAILY', 'WEEKLY'].includes(settings.mode) && <Field label="Hora local de ejecución"><input type="time" required value={settings.local_time} disabled={!canSchedule} onChange={event => update('local_time', event.target.value)}/></Field>}
    {settings.mode === 'WEEKLY' && <fieldset><legend>Días de ejecución</legend>{['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'].map((day, index) => <label key={day}><input type="checkbox" disabled={!canSchedule} checked={settings.weekdays.includes(index)} onChange={event => update('weekdays', event.target.checked ? [...settings.weekdays, index] : settings.weekdays.filter(value => value !== index))}/>{day} </label>)}</fieldset>}
    {settings.mode === 'CHAINED' ? <Field label="Contrato Intake disparador"><select required value={settings.intake_configuration_id || ''} disabled={!canSchedule || !canIntake} onChange={event => update('intake_configuration_id', event.target.value)}><option value="">Selecciona un contrato</option>{intake.data?.items.map(contract => <option key={contract.id} value={contract.id}>{contract.name} · v{contract.version}</option>)}</select></Field> : <Field label="Política de entrada"><select value={settings.source_policy} disabled={!canSchedule} onChange={event => update('source_policy', event.target.value)}>{Object.entries(policies).filter(([value]) => value !== 'INTAKE_OUTPUT').map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></Field>}
    <label><input type="checkbox" checked={enabled} disabled={!canSchedule} onChange={event => setEnabled(event.target.checked)}/> Automatización activa</label>
    {settings.mode === 'CHAINED' && <label><input type="checkbox" checked={settings.allow_warnings} disabled={!canSchedule} onChange={event => update('allow_warnings', event.target.checked)}/> Permitir APPROVED_WITH_WARNINGS</label>}
    <label><input type="checkbox" checked={settings.allow_empty} disabled={!canSchedule} onChange={event => update('allow_empty', event.target.checked)}/> Permitir entrada vacía</label>
    <label><input type="checkbox" checked={settings.repeat_versions} disabled={!canSchedule} onChange={event => update('repeat_versions', event.target.checked)}/> Permitir procesar de nuevo la misma versión en futuras ocurrencias</label>
    </div><Notice>{settings.mode === 'CHAINED' ? 'Entrega exclusivamente la salida publicada de la ejecución Intake que dispara esta automatización. SUCCESS + APPROVED es la condición inicial; REJECTED no habilita entregas.' : 'Los horarios atrasados se agrupan en una sola ocurrencia. Se omite el despacho mientras exista una entrega activa.'} Cada revisión conserva destino, target, columnas y estrategia de la configuración seleccionada.</Notice>{settings.allow_empty && <Notice>Una entrada vacía con OVERWRITE puede vaciar el target. Esta opción autoriza deliberadamente esa entrada.</Notice>}{intake.error && <ErrorState error={intake.error}/>} {save.error && <ErrorState error={save.error}/>}<button className="button primary" disabled={!canSchedule || save.isPending || !configuration}>{save.isPending ? 'Guardando…' : item ? 'Guardar nueva revisión' : 'Crear automatización'}</button></form>
}

export function DeliveryAutomationPage() {
  const { id } = useParams(), cache = useQueryClient(), canRead = usePermission('delivery:read'), canExecute = usePermission('delivery:execute'), canSchedule = usePermission('delivery:schedule')
  const [offset, setOffset] = useState(0), [sourceRun, setSourceRun] = useState(''), [repeat, setRepeat] = useState(false), key = useRef<string | null>(null)
  const list = useQuery({ queryKey: ['delivery-automations', offset], queryFn: () => api<{ items: Automation[]; total: number }>(`/delivery/automations?offset=${offset}&limit=25`), enabled: canRead && !id, refetchInterval: 15000 })
  const detail = useQuery({ queryKey: ['delivery-automation', id], queryFn: () => api<Automation>(`/delivery/automations/${id}`), enabled: canRead && !!id, refetchInterval: 15000 })
  const configurations = useQuery({ queryKey: ['delivery-configurations'], queryFn: () => api<{ items: DeliveryConfiguration[]; total: number }>('/delivery/configurations'), enabled: canRead })
  const occurrences = useQuery({ queryKey: ['delivery-occurrences', id, offset], queryFn: () => api<Collection>(`/delivery/automations/${id}/occurrences?offset=${offset}&limit=25`), enabled: canRead && !!id, refetchInterval: 15000 })
  const run = useMutation({ mutationFn: () => { key.current ||= crypto.randomUUID(); return post(`/delivery/automations/${id}/dispatch`, { request_key: key.current, repeat, source_run_id: sourceRun || null }) }, onSuccess: () => { key.current = null; void cache.invalidateQueries({ queryKey: ['delivery-occurrences', id] }) } })
  if (!canRead) return <Notice>Tu rol no permite consultar las automatizaciones.</Notice>
  const item = detail.data
  return <><DeliveryNavigation/><PageHeading title={item?.name || 'Automatizaciones de Delivery'} description="Programa entregas o conecta una salida validada de Intake con un destino publicado." back={id ? '/delivery/automation' : undefined}/>
    {id ? detail.isPending ? <Loading/> : detail.error ? <ErrorState error={detail.error} retry={() => detail.refetch()}/> : item && <><section className="panel"><div className="table-toolbar"><Badge value={item.enabled ? 'ACTIVE' : 'PENDING'}/><span>Revisión {item.version} · Responsable: {item.responsible_name}</span></div><p>{modes[item.settings.mode as keyof typeof modes]} · {item.settings.timezone} · {policies[item.settings.source_policy as keyof typeof policies]}</p><p>Próximo despacho: {item.next_run_at ? date(item.next_run_at) : item.settings.mode === 'CHAINED' ? 'Al terminar un Intake elegible' : 'Sin próximo horario'}</p><form className="form-stack" onSubmit={event => { event.preventDefault(); run.mutate() }}>{item.settings.mode === 'CHAINED' && <Field label="Ejecución Intake concreta para el despacho manual"><input required value={sourceRun} onChange={event => { setSourceRun(event.target.value); key.current = null }} disabled={!canExecute}/></Field>}<label><input type="checkbox" checked={repeat} disabled={!canExecute || run.isPending} onChange={event => { setRepeat(event.target.checked); key.current = null }}/> Repetición deliberada de la versión para este despacho</label><button className="button secondary" disabled={!canExecute || run.isPending}><Play size={16}/> Despachar ahora</button>{run.error && <ErrorState error={run.error}/>}</form></section><section className="panel"><h2>Ocurrencias y resultados</h2>{occurrences.isPending ? <Loading/> : occurrences.error ? <ErrorState error={occurrences.error}/> : <><div className="table-scroll"><table><thead><tr><th>Planificada</th><th>Origen</th><th>Estado técnico</th><th>Decisión</th><th>Detalle</th></tr></thead><tbody>{occurrences.data.items.map(occurrence => <tr key={occurrence.id}><td>{date(occurrence.planned_at)}{occurrence.coalesced_intervals > 0 && <small>{occurrence.coalesced_intervals} intervalos agrupados</small>}</td><td>{occurrence.origin}</td><td><Badge value={occurrence.status}/>{occurrence.reason_code && <small className="reason-code">{occurrence.reason_code}</small>}</td><td>{occurrence.decision ? <Badge value={occurrence.decision}/> : '—'}</td><td>{occurrence.run_id ? <Link to={`/runs/${occurrence.run_id}`}>Ver entrega</Link> : 'Sin entrega despachada'}{occurrence.source_run_id && <Link to={`/runs/${occurrence.source_run_id}`}>Ver Intake origen</Link>}</td></tr>)}</tbody></table></div><Pagination offset={offset} total={occurrences.data.total} limit={25} onChange={setOffset}/></>}</section></> : list.isPending ? <Loading/> : list.error ? <ErrorState error={list.error}/> : <section className="panel">{!list.data.items.length ? <Empty title="Sin automatizaciones" description="Publica una configuración de Delivery y crea su programación."/> : list.data.items.map(automation => <article className="config-card" key={automation.id}><CalendarClock/><div className="config-info"><h3><Link to={`/delivery/automation/${automation.id}`}>{automation.name}</Link></h3><p>{modes[automation.settings.mode as keyof typeof modes]} · {automation.settings.timezone} · Revisión {automation.version}</p><span>Responsable: {automation.responsible_name}</span></div><Badge value={automation.enabled ? 'ACTIVE' : 'PENDING'}/></article>)}<Pagination offset={offset} total={list.data.total} limit={25} onChange={setOffset}/></section>}
    {configurations.error && <ErrorState error={configurations.error}/>} {canSchedule && (!id || item) && configurations.data && <Editor key={`${id || 'new'}:${item?.version || 0}`} item={item} configurations={configurations.data.items}/>} {!canSchedule && <Notice>Tu rol permite consultar las automatizaciones. Para cambiarlas necesitas el permiso de programación.</Notice>}
    {!configurations.data?.items.length && configurations.data && <Link className="button secondary" to="/delivery/new"><Plus size={16}/> Preparar configuración de entrega</Link>}
  </>
}
