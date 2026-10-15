import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { CalendarClock } from 'lucide-react'
import { api, post } from '../../api/client'
import type { Collection, RecordData } from '../../api/client'
import { usePermission, useSession } from '../../app/session'
import { Badge, date, ErrorState, Field, Loading, Modal, Notice, Pagination } from '../../components/ui'
import { timeZoneError, wallTime, zonedStart } from '../delivery/automationTime'

function ScheduleEditor({ contract, schedule }: { contract: RecordData; schedule: RecordData | null }) {
  const session = useSession(), cache = useQueryClient()
  const canSchedule = usePermission('intake:schedule'), canUsers = usePermission('users:read')
  const initial = schedule?.settings
  const [mode, setMode] = useState(initial?.mode || 'EVENT'), [zone, setZone] = useState(initial?.timezone || 'America/Bogota')
  const [start, setStart] = useState(wallTime(initial ? new Date(initial.starts_at) : new Date(Date.now() + 60000), initial?.timezone || 'America/Bogota'))
  const [minutes, setMinutes] = useState(String((initial?.interval_seconds || 3600) / 60)), [localTime, setLocalTime] = useState(initial?.local_time || '09:00')
  const [weekdays, setWeekdays] = useState<number[]>(initial?.weekdays || [0]), [enabled, setEnabled] = useState(schedule?.enabled ?? true)
  const [responsible, setResponsible] = useState(schedule?.responsible_user_id || session?.user.id || '')
  const [useSelectedRevision, setUseSelectedRevision] = useState(false)
  const users = useQuery({ queryKey: ['intake-schedule-users'], queryFn: () => api<Collection>('/users'), enabled: canUsers && canSchedule })
  const save = useMutation({ mutationFn: () => {
    const configurationId = schedule && !useSelectedRevision ? schedule.configuration_id : contract.id
    const unchangedStart = initial && start === wallTime(new Date(initial.starts_at), initial.timezone) && zone === initial.timezone
    return post(`/intake/contracts/${configurationId}/schedule`, { settings: { mode, timezone: zone,
      starts_at: unchangedStart ? initial.starts_at : zonedStart(start, zone), interval_seconds: Math.round(Number(minutes) * 60),
      local_time: localTime, weekdays, requested_engine: initial?.requested_engine || 'AUTO' }, enabled,
      responsible_user_id: responsible, expected_version: schedule?.version || null })
  }, onSuccess: () => {
    void cache.invalidateQueries({ queryKey: ['intake-schedule'] }); void cache.invalidateQueries({ queryKey: ['intake-occurrences'] })
  } })
  const usersList = users.data?.items || (session ? [{ id: session.user.id, name: session.user.name }] : [])
  return <form className="form-stack intake-schedule-editor" onSubmit={event => { event.preventDefault(); save.mutate() }}>
    <div className="form-grid"><Field label="Disparo Intake"><select value={mode} onChange={event => setMode(event.target.value)} disabled={!canSchedule}>
      <option value="EVENT">Cada nueva versión publicada</option><option value="INTERVAL">Por intervalo</option><option value="DAILY">Diario</option><option value="WEEKLY">Semanal</option>
    </select></Field><Field label="Zona horaria"><input required maxLength={80} value={zone} onChange={event => setZone(event.target.value)} disabled={!canSchedule}/></Field>
    <Field label="Desde (hora en la zona seleccionada)"><input type="datetime-local" required value={start} onChange={event => setStart(event.target.value)} disabled={!canSchedule}/></Field>
    <Field label="Responsable efectivo"><select required value={responsible} onChange={event => setResponsible(event.target.value)} disabled={!canSchedule}>
      {schedule?.responsible_user_id && !usersList.some(item => item.id === schedule.responsible_user_id) && <option value={schedule.responsible_user_id}>{schedule.responsible_name || schedule.responsible_user_id}</option>}
      {usersList.map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
    </select></Field>
    {mode === 'INTERVAL' && <Field label="Intervalo (minutos)"><input type="number" min="1" max="44640" step="1" required value={minutes} onChange={event => setMinutes(event.target.value)} disabled={!canSchedule}/></Field>}
    {['DAILY', 'WEEKLY'].includes(mode) && <Field label="Hora diaria"><input type="time" required value={localTime} onChange={event => setLocalTime(event.target.value)} disabled={!canSchedule}/></Field>}</div>
    {mode === 'WEEKLY' && <fieldset><legend>Días de la semana</legend>{['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'].map((day, index) => <label key={day}><input type="checkbox" checked={weekdays.includes(index)} disabled={!canSchedule} onChange={event => setWeekdays(event.target.checked ? [...weekdays, index].sort() : weekdays.filter(value => value !== index))}/>{day} </label>)}</fieldset>}
    <label><input type="checkbox" checked={enabled} onChange={event => setEnabled(event.target.checked)} disabled={!canSchedule}/> Programación activa</label>
    {schedule && <p>Revisión del contrato fijada: v{schedule.configuration_version}. {schedule.enabled ? `Próximo horario: ${date(schedule.next_run_at)}` : 'Programación pausada'}.</p>}
    {schedule?.configuration_id !== contract.id && schedule && <label><input type="checkbox" checked={useSelectedRevision} onChange={event => setUseSelectedRevision(event.target.checked)} disabled={!canSchedule}/> Actualizar la programación a esta revisión v{contract.version}</label>}
    {schedule?.update_available && <Notice>Existe una revisión posterior del contrato. Publicarla conserva los trabajos ya registrados; puedes actualizar la programación desde esa revisión.</Notice>}
    <Notice>El modo por evento conserva cada versión publicada. Las ejecuciones manuales y automáticas esperan su turno por contrato completo. Los horarios atrasados se agrupan en el último intervalo; los rechazos de calidad requieren revisión.</Notice>
    {timeZoneError(zone) && <Notice>{timeZoneError(zone)}</Notice>}{save.error && <ErrorState error={save.error}/>} {users.error && <ErrorState error={users.error}/>}
    <button className="button secondary small" disabled={!canSchedule || save.isPending || !!timeZoneError(zone) || mode === 'WEEKLY' && !weekdays.length}>{save.isPending ? 'Guardando…' : 'Guardar programación'}</button>
  </form>
}

function DeleteDialog({ contract, close }: { contract: RecordData; close: () => void }) {
  const cache = useQueryClient(), [note, setNote] = useState('')
  const impact = useQuery({ queryKey: ['intake-deletion-impact', contract.id], queryFn: () => api(`/intake/contracts/${contract.id}/deletion-impact`), staleTime: 0 })
  const remove = useMutation({ mutationFn: () => api(`/intake/contracts/${contract.id}`, { method: 'DELETE', body: JSON.stringify({ impact_token: impact.data?.impact_token, note }) }),
    onSuccess: () => { void cache.invalidateQueries({ queryKey: ['configurations', 'intake'] }); void cache.invalidateQueries({ queryKey: ['intake-schedule'] }); close() },
    onError: () => { void impact.refetch() } })
  return <Modal open onOpenChange={value => { if (!value && !remove.isPending) close() }} title={`Eliminar contrato ${contract.name}`} description="Baja lógica del contrato completo y todas sus revisiones operativas.">
    {impact.isPending ? <Loading text="Revalidando el impacto…"/> : impact.error ? <ErrorState error={impact.error} retry={() => impact.refetch()}/> : <form className="form-stack" onSubmit={event => { event.preventDefault(); remove.mutate() }}>
      <p>{impact.data?.revision_count} revisiones. Se deshabilitará {impact.data?.intake_schedule_enabled ? 'la programación Intake' : 'cualquier programación activa'} y {impact.data?.delivery_automation_ids.length || 0} disparos futuros Delivery dependientes.</p>
      {!!impact.data?.pending_run_ids.length && <Notice>Bloqueado por {impact.data.pending_run_ids.length} ejecuciones pendientes o activas.</Notice>}
      {!!impact.data?.open_exception_ids.length && <Notice>Bloqueado por {impact.data.open_exception_ids.length} excepciones abiertas.</Notice>}
      <Notice>Se conservan datasets, aprobaciones, revisiones, hashes, historial y evidencia. Las entregas ya registradas y los monitores Sentinel se conservan.</Notice>
      <Field label="Motivo de baja"><textarea required minLength={3} maxLength={2000} value={note} onChange={event => setNote(event.target.value)}/></Field>
      {remove.error && <ErrorState error={remove.error}/>}<div className="modal-footer"><button type="button" className="button secondary" onClick={close} disabled={remove.isPending}>Cancelar</button><button className="button danger" disabled={!impact.data?.can_delete || impact.isFetching || remove.isPending || note.trim().length < 3}>{remove.isPending ? 'Eliminando…' : 'Confirmar baja lógica'}</button></div>
    </form>}
  </Modal>
}

function ScheduleContent({ contract }: { contract: RecordData }) {
  const [offset, setOffset] = useState(0), [deleting, setDeleting] = useState(false), canDelete = usePermission('intake:delete')
  const schedule = useQuery({ queryKey: ['intake-schedule', contract.id], queryFn: () => api<RecordData | null>(`/intake/contracts/${contract.id}/schedule`) })
  const occurrences = useQuery({ queryKey: ['intake-occurrences', contract.id, offset], queryFn: () => api<Collection>(`/intake/contracts/${contract.id}/occurrences?offset=${offset}&limit=25`), refetchInterval: 5000 })
  return <div className="form-stack">
    {contract.deleted ? <Notice>Contrato eliminado. Las revisiones y ejecuciones históricas siguen disponibles.</Notice> : schedule.isPending ? <Loading/> : schedule.error ? <ErrorState error={schedule.error} retry={() => schedule.refetch()}/> : <ScheduleEditor key={schedule.data?.version || 'new'} contract={contract} schedule={schedule.data}/>}
    <h4>Ocurrencias y versiones de entrada</h4>{occurrences.isPending ? <Loading/> : occurrences.error ? <ErrorState error={occurrences.error} retry={() => occurrences.refetch()}/> : !occurrences.data.items.length ? <p>Aún no hay ocurrencias registradas.</p> : <><div className="table-scroll"><table><thead><tr><th>Fecha</th><th>Versión de entrada</th><th>Estado técnico</th><th>Resultado</th><th>Evidencia</th></tr></thead><tbody>{occurrences.data.items.map(item => <tr key={item.id}><td>{date(item.planned_at)} · {item.origin}</td><td className="mono">{item.dataset_version_id || 'Sin versión'}</td><td>{item.queue_status === 'WAITING' ? 'Pendiente del contrato' : <Badge value={item.status}/>} {item.reason_code && <small>{item.reason_code}</small>}</td><td>{item.decision ? <Badge value={item.decision}/> : '—'}</td><td>{item.run_id ? <Link to={`/runs/${item.run_id}`}>Ver ejecución</Link> : 'Sin ejecución'}</td></tr>)}</tbody></table></div><Pagination offset={offset} total={occurrences.data.total} limit={25} onChange={setOffset}/></>}
    {canDelete && !contract.deleted && <button type="button" className="button secondary small" onClick={() => setDeleting(true)}>Revisar impacto y eliminar contrato</button>}
    {deleting && <DeleteDialog contract={contract} close={() => setDeleting(false)}/>}
  </div>
}

export function IntakeSchedulePanel({ contract }: { contract: RecordData }) {
  const [expanded, setExpanded] = useState(false)
  return <details className="monitor-schedule" onToggle={event => setExpanded(event.currentTarget.open)}><summary><CalendarClock size={15}/> Programación, histórico y baja lógica</summary>{expanded && <ScheduleContent contract={contract}/>}</details>
}
