import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import { Activity, ArrowUpRight, Ban, BookOpen, Check, CheckCircle2, ClipboardList, Cpu, RefreshCw, ShieldCheck, Users } from 'lucide-react'
import { api, download } from '../api/client'
import { UsersPanel } from '../features/identity/UsersPanel'
import type { Collection, RecordData } from '../api/client'
import { usePermission } from '../app/session'
import { Badge, date, Empty, ErrorState, Field, label, Loading, Modal, Notice, number, PageHeading, SearchBox } from '../components/ui'

const states = ['OPEN', 'ASSIGNED', 'REOPENED', 'INVESTIGATING', 'PENDING_VALIDATION', 'RESOLVED', 'DISCARDED', 'ACCEPTED', 'NOT_APPLICABLE', 'WAITING_EXTERNAL', 'FALSE_POSITIVE']
const workflowTransitions: Record<string, string[]> = {
  OPEN: ['ASSIGNED', 'INVESTIGATING'],
  ASSIGNED: ['INVESTIGATING'],
  REOPENED: ['ASSIGNED', 'INVESTIGATING'],
  INVESTIGATING: ['ASSIGNED', 'PENDING_VALIDATION'],
  PENDING_VALIDATION: ['INVESTIGATING'],
  WAITING_EXTERNAL: ['INVESTIGATING', 'PENDING_VALIDATION'],
}
const administrativeStates = ['DISCARDED', 'ACCEPTED', 'NOT_APPLICABLE']

function exceptionRunId(item: RecordData) {
  return item.origin_run_id || item.run_id
}

function technicalValidation(item: RecordData) {
  const validation = item.technical_validation || {}
  const validationRunId = validation.validation_run_id || item.validation_run_id
  const validated = validation.validated === true || validation.status === 'VALIDATED'
  return {
    status: validation.status || (validated ? 'VALIDATED' : 'NOT_REQUESTED'),
    eligible: Boolean(validation.eligible),
    validated,
    canResolve: validated && Boolean(validation.can_resolve ?? true),
    reason: validation.reason || (validated ? 'Una ejecución posterior confirmó que el problema ya no está presente.' : 'Se requiere una ejecución posterior del mismo control que confirme la corrección.'),
    validationRunId,
    validatedAt: validation.validated_at || item.validated_at,
    evidence: validation.evidence || item.validation_evidence,
  }
}

function ExceptionEditor({ item, onSaved }: { item: RecordData; onSaved: () => void }) {
  const cache = useQueryClient()
  const canClose = usePermission('exceptions:close')
  const canWrite = usePermission('exceptions:write')
  const assignees = useQuery({ queryKey: ['exception-assignees'], queryFn: () => api<Collection>('/exceptions/assignees') })
  const [assignedUser, setAssignedUser] = useState(item.assigned_user_id || '')
  const [priority, setPriority] = useState(item.priority || item.severity || 'HIGH')
  const [sla, setSla] = useState(item.sla_hours ? String(item.sla_hours) : '')
  const localDate = (value: string) => { const dateValue = new Date(value); return new Date(dateValue.getTime() - dateValue.getTimezoneOffset() * 60000).toISOString().slice(0, 16) }
  const [due, setDue] = useState(item.due_at ? localDate(item.due_at) : '')
  const [automatic, setAutomatic] = useState(Boolean(item.auto_resolve_enabled))
  const [attachment, setAttachment] = useState<File | null>(null), [attachmentNote, setAttachmentNote] = useState('')
  const [state, setState] = useState(item.state), [owner, setOwner] = useState(item.owner)
  const [cause, setCause] = useState(item.root_cause || ''), [resolution, setResolution] = useState(item.resolution || '')
  const [comment, setComment] = useState('')
  const [administrativeState, setAdministrativeState] = useState(''), [administrativeReason, setAdministrativeReason] = useState(item.administrative_reason || '')
  const validation = technicalValidation(item)
  const originRunId = exceptionRunId(item)
  const refresh = async () => {
    // Keep the edit boundary pending until the authoritative revision is rendered.
    // Otherwise a fast next edit can be discarded by the version-keyed remount.
    await Promise.all([
      cache.invalidateQueries({ queryKey: ['exceptions'] }),
      cache.invalidateQueries({ queryKey: ['exception', item.id] }),
      cache.invalidateQueries({ queryKey: ['dashboard'] }),
      cache.invalidateQueries({ queryKey: ['audit'] }),
    ])
    onSaved()
  }
  const update = useMutation({
    mutationFn: () => api(`/exceptions/${item.id}`, { method: 'PATCH', body: JSON.stringify({ version: item.version, state, owner, root_cause: cause, resolution, comment, ...(assignedUser !== (item.assigned_user_id || '') ? { assigned_user_id: assignedUser || null } : {}), priority, ...(sla !== (item.sla_hours ? String(item.sla_hours) : '') ? { sla_hours: sla ? Number(sla) : null } : {}), ...(due !== (item.due_at ? localDate(item.due_at) : '') ? { due_at: due ? new Date(due).toISOString() : null } : {}), ...(canClose ? { auto_resolve_enabled: automatic } : {}) }) }),
    onSuccess: refresh,
  })
  const validate = useMutation({
    mutationFn: () => api(`/exceptions/${item.id}/validate`, { method: 'POST', body: JSON.stringify({ version: item.version }) }),
    onSuccess: refresh,
  })
  const resolve = useMutation({
    mutationFn: () => api(`/exceptions/${item.id}`, { method: 'PATCH', body: JSON.stringify({ version: item.version, state: 'RESOLVED', owner, root_cause: cause, resolution, comment }) }),
    onSuccess: refresh,
  })
  const administrativeClose = useMutation({
    mutationFn: () => api(`/exceptions/${item.id}`, { method: 'PATCH', body: JSON.stringify({ version: item.version, state: administrativeState, owner, root_cause: cause, administrative_reason: administrativeReason, comment }) }),
    onSuccess: refresh,
  })
  const reopen = useMutation({
    mutationFn: () => api(`/exceptions/${item.id}`, { method: 'PATCH', body: JSON.stringify({ version: item.version, state: 'REOPENED', comment }) }),
    onSuccess: refresh,
  })
  const addComment = useMutation({ mutationFn: () => api(`/exceptions/${item.id}/comments`, { method: 'POST', body: JSON.stringify({ version: item.version, comment }) }), onSuccess: refresh })
  const attach = useMutation({ mutationFn: () => { const body = new FormData(); body.set('version', String(item.version)); body.set('description', attachmentNote); if (attachment) body.set('file', attachment); return api(`/exceptions/${item.id}/attachments`, { method: 'POST', body }) }, onSuccess: refresh })
  const downloadAttachment = useMutation({ mutationFn: (entry: RecordData) => download(`/exceptions/${item.id}/attachments/${entry.id}/download`, entry.name) })
  const terminal = ['RESOLVED', ...administrativeStates, 'FALSE_POSITIVE'].includes(item.state)
  const workflowOptions = terminal ? [item.state] : [item.state, ...(workflowTransitions[item.state] || [])].filter((value, index, values) => !administrativeStates.includes(value) && value !== 'RESOLVED' && value !== 'FALSE_POSITIVE' && values.indexOf(value) === index)
  const workflowDirty = state !== item.state || owner !== item.owner || cause !== (item.root_cause || '') || resolution !== (item.resolution || '') || comment.length > 0 || assignedUser !== (item.assigned_user_id || '') || priority !== (item.priority || item.severity) || sla !== (item.sla_hours ? String(item.sla_hours) : '') || due !== (item.due_at ? localDate(item.due_at) : '') || automatic !== Boolean(item.auto_resolve_enabled)
  const resolveFieldsComplete = cause.trim().length > 0 && resolution.trim().length > 0
  const resolveBlockedReason = !canClose ? 'Tu rol no tiene permiso para cerrar excepciones.' : item.state !== 'PENDING_VALIDATION' ? 'La excepción debe estar en Pendiente de validación antes de resolverse.' : !validation.canResolve ? validation.reason : !resolveFieldsComplete ? 'Registra la causa raíz y la corrección aplicada antes de resolver.' : ''
  const validationBlockedReason = item.state !== 'PENDING_VALIDATION'
    ? 'Guarda primero el estado Pendiente de validación.'
    : !validation.eligible && !validation.validated ? validation.reason : ''
  const actionError = update.error || validate.error || resolve.error || administrativeClose.error || reopen.error || addComment.error || attach.error || downloadAttachment.error
  const busy = update.isPending || validate.isPending || resolve.isPending || administrativeClose.isPending || reopen.isPending || addComment.isPending || attach.isPending
  return <form className="form-stack" onSubmit={event => { event.preventDefault(); if (!busy) update.mutate() }}>
    <fieldset className="form-stack" disabled={busy} aria-busy={busy} style={{ border: 0, padding: 0, margin: 0, minWidth: 0 }}>
    <div className="detail-summary"><Badge value={item.severity}/><Badge value={item.state}/><span>{label(item.module)}</span>{originRunId && <Link to={`/runs/${originRunId}`} className="text-link">Ver ejecución de origen <ArrowUpRight size={15}/></Link>}</div>
    <section className="exception-origin" aria-label="Origen de la excepción">
      <div><span>Control o configuración</span><strong>{item.configuration_name || 'Configuración histórica'}</strong>{item.configuration_version && <small>Versión {item.configuration_version}</small>}</div>
      <div><span>ID de configuración</span><strong className="mono">{item.configuration_id || 'No disponible en registros históricos'}</strong></div>
      <div><span>Ejecución de origen</span>{originRunId ? <Link to={`/runs/${originRunId}`} className="text-link mono">{originRunId} <ArrowUpRight size={13}/></Link> : <strong>—</strong>}</div>
    </section>
    <div className="form-grid"><Field label="Estado de gestión"><select value={state} onChange={event => setState(event.target.value)} disabled={terminal || !canWrite}>{workflowOptions.map(value => <option key={value} value={value}>{label(value)}</option>)}</select></Field><Field label="Responsable"><select value={assignedUser} onChange={event => { setAssignedUser(event.target.value); setOwner(assignees.data?.items.find(entry => entry.id === event.target.value)?.name || 'Sin asignar') }} disabled={terminal || !canWrite || !assignees.data}><option value="">{item.owner && !item.assigned_user_id ? `Sin asignación de usuario (${item.owner})` : 'Sin asignar'}</option>{assignees.data?.items.map(entry => <option key={entry.id} value={entry.id}>{entry.name}</option>)}{item.assigned_user_id && !assignees.data?.items.some(entry => entry.id === item.assigned_user_id) && <option value={item.assigned_user_id}>{item.owner} (no disponible)</option>}</select></Field></div>
    {assignees.error && <ErrorState error={assignees.error} retry={() => assignees.refetch()}/>}
    <div className="form-grid"><Field label="Prioridad del caso"><select value={priority} onChange={event => setPriority(event.target.value)} disabled={terminal || !canWrite}>{['CRITICAL','HIGH','MEDIUM','LOW'].map(value => <option key={value} value={value}>{label(value)}</option>)}</select></Field><Field label="SLA (horas)" hint="Plazo desde la creación o reapertura. Una fecha objetivo explícita tiene prioridad."><input type="number" min={1} max={8760} value={sla} onChange={event => setSla(event.target.value)} disabled={terminal || !canWrite}/></Field><Field label="Fecha objetivo"><input type="datetime-local" value={due} onChange={event => setDue(event.target.value)} disabled={terminal || !canWrite}/></Field><div>{item.overdue && <Notice>El plazo de esta excepción está vencido.</Notice>}</div></div>
    <label className="checkbox-label"><input type="checkbox" checked={automatic} onChange={event => setAutomatic(event.target.checked)} disabled={terminal || !canClose}/> Resolver automáticamente tras validación técnica</label><small className="muted">Deshabilitada por defecto. Solo se aplica en Pendiente de validación; una ejecución posterior del mismo control debe confirmar la corrección.</small>
    <Field label="Causa raíz" hint="Describe por qué ocurrió la diferencia."><textarea rows={3} maxLength={10000} value={cause} onChange={event => setCause(event.target.value)} disabled={terminal || !canWrite}/></Field>
    <Field label="Corrección aplicada" hint="Describe el cambio que una ejecución posterior debe confirmar."><textarea rows={3} maxLength={10000} value={resolution} onChange={event => setResolution(event.target.value)} disabled={terminal || !canWrite}/></Field>
    <Field label="Comentario para el historial"><input maxLength={10000} value={comment} onChange={event => setComment(event.target.value)} placeholder={terminal ? "Motivo para reabrir o comentario adicional" : "Qué cambió en esta revisión"} disabled={!canWrite}/></Field>
    {(!terminal || item.state === 'RESOLVED') && <section className={`technical-validation ${validation.validated ? 'validated' : ''} ${item.state === 'RESOLVED' && !validation.validated ? 'historical' : ''}`} aria-labelledby="technical-validation-title">
      <div className="validation-heading"><span className="validation-icon">{validation.validated ? <CheckCircle2 size={19}/> : <ShieldCheck size={19}/>}</span><div><h3 id="technical-validation-title">Validación técnica</h3><p>{validation.validated ? 'Trackvance verificó la corrección en una ejecución posterior del mismo control.' : item.state === 'RESOLVED' ? 'Este cierre se registró antes de que Trackvance exigiera evidencia técnica estructurada.' : 'La excepción solo podrá resolverse cuando una ejecución posterior confirme que el hallazgo desapareció.'}</p></div><Badge value={validation.status}/></div>
      {validation.validated ? <div className="validation-result"><strong>Validada técnicamente</strong><span>{validation.reason}</span><div>{validation.validationRunId && <Link to={`/runs/${validation.validationRunId}`} className="text-link">Ver ejecución de validación <ArrowUpRight size={14}/></Link>}{validation.validatedAt && <small>{date(validation.validatedAt)}</small>}</div></div> : item.state === 'RESOLVED' ? <div className="validation-blocked"><Ban size={16}/><div><strong>Cierre histórico sin evidencia técnica estructurada</strong><p>El estado histórico se conserva, pero no se afirma que Trackvance haya verificado la corrección.</p></div></div> : <div className="validation-blocked"><Ban size={16}/><div><strong>{validation.status === 'FAILED' ? 'El problema sigue presente' : validation.eligible ? 'Ejecución posterior disponible' : 'Resolución bloqueada'}</strong><p>{validation.reason}</p></div></div>}
      {validation.evidence && <details className="validation-evidence"><summary>Detalle de la evidencia técnica</summary><pre>{typeof validation.evidence === 'string' ? validation.evidence : JSON.stringify(validation.evidence, null, 2)}</pre></details>}
      {!terminal && <><div className="validation-actions"><button className="button secondary" type="button" onClick={() => validate.mutate()} disabled={!canWrite || validate.isPending || validation.validated || Boolean(validationBlockedReason)} title={validationBlockedReason}>{validate.isPending ? 'Validando…' : 'Validar corrección'}</button><button className="button primary" type="button" onClick={() => resolve.mutate()} disabled={resolve.isPending || Boolean(resolveBlockedReason)} title={resolveBlockedReason}><Check size={16}/>{resolve.isPending ? 'Resolviendo…' : 'Resolver excepción'}</button></div>{resolveBlockedReason && <small className="blocked-reason" role="note">{resolveBlockedReason}</small>}</>}
    </section>}
    {!terminal && <details className="administrative-close"><summary>Cierre administrativo</summary><div><p>Descartar, aceptar o marcar como no aplicable exige un motivo. Esta decisión no equivale a una resolución verificada.</p><Field label="Decisión administrativa"><select value={administrativeState} onChange={event => setAdministrativeState(event.target.value)}><option value="">Selecciona una decisión</option>{administrativeStates.map(value => <option key={value} value={value}>{label(value)}</option>)}</select></Field><Field label="Motivo administrativo" hint="Explica por qué se cierra el caso sin confirmar una corrección técnica."><textarea rows={3} maxLength={10000} value={administrativeReason} onChange={event => setAdministrativeReason(event.target.value)} required={Boolean(administrativeState)}/></Field><button className="button secondary" type="button" onClick={() => administrativeClose.mutate()} disabled={!canClose || administrativeClose.isPending || !administrativeState || !administrativeReason.trim()}>{administrativeClose.isPending ? 'Registrando…' : 'Registrar cierre administrativo'}</button></div></details>}
    {item.state === 'RESOLVED' && validation.validated && <Notice success>Esta excepción fue resuelta después de una validación técnica.</Notice>}
    {item.state === 'RESOLVED' && !validation.validated && <Notice>El cierre histórico se conserva para no alterar el historial, sin equipararlo a una resolución técnicamente validada.</Notice>}
    {terminal && item.state !== 'RESOLVED' && <Notice>{`Esta excepción terminó mediante una decisión administrativa: ${label(item.state)}. No se considera una resolución técnica.`}</Notice>}
    {actionError && <ErrorState error={actionError} retry={() => cache.invalidateQueries({ queryKey: ['exception', item.id] })}/>}
    <div className="modal-footer"><span className="muted">Versión {item.version} · {date(item.updated_at)}</span>{terminal ? <button className="button secondary" type="button" onClick={() => reopen.mutate()} disabled={!canWrite || reopen.isPending || !comment.trim()}>{reopen.isPending ? 'Reabriendo…' : 'Reabrir excepción'}</button> : <button className="button secondary" disabled={!canWrite || update.isPending || !workflowDirty}><Check size={16}/>{update.isPending ? 'Guardando…' : 'Guardar gestión'}</button>}</div>
    <div className="validation-actions"><button className="button secondary" type="button" onClick={() => addComment.mutate()} disabled={!canWrite || !comment.trim() || addComment.isPending}>Agregar comentario</button></div>
    <section className="panel" aria-label="Evidencia adjunta"><h3>Evidencia adjunta</h3><p className="muted">Máximo 10 MiB por archivo. Cada adjunto conserva su hash y responsable.</p>{(item.attachments || []).map((entry: RecordData) => <div className="history-event" key={entry.id}><div><strong>{entry.name}</strong><p>{entry.description}</p><small className="mono">SHA-256: {entry.sha256}</small><button type="button" className="text-button" onClick={() => downloadAttachment.mutate(entry)} disabled={downloadAttachment.isPending}>Descargar {entry.name}</button></div></div>)}{canWrite && <><Field label="Archivo de evidencia"><input type="file" accept=".txt,.csv,.json,.pdf,.png,.jpg,.jpeg,.xlsx,.parquet" onChange={event => setAttachment(event.target.files?.[0] || null)}/></Field><Field label="Descripción del adjunto"><input maxLength={500} value={attachmentNote} onChange={event => setAttachmentNote(event.target.value)}/></Field>{attachment && attachment.size > 10 * 1024 * 1024 && <p role="alert">El archivo supera 10 MiB.</p>}<button type="button" className="button secondary" onClick={() => attach.mutate()} disabled={!attachment || attachment.size > 10 * 1024 * 1024 || attach.isPending}>{attach.isPending ? 'Adjuntando…' : 'Adjuntar evidencia'}</button></>}</section>
    <section className="case-history"><h3>Historial de la excepción</h3>{[...(item.events || [])].reverse().map((event: RecordData, index: number) => <div className="history-event" key={`${event.timestamp}-${index}`}><Activity size={16}/><div><strong>{event.actor} · {event.event_type === 'TECHNICAL_VALIDATION' ? `Validación técnica · ${label(event.validation_status)}` : label(event.to_state)}</strong><p>{event.comment}</p>{event.administrative_reason && <p>Motivo administrativo: {event.administrative_reason}</p>}<div className="history-meta"><small>{date(event.timestamp)}</small>{event.validation_run_id && <Link to={`/runs/${event.validation_run_id}`} className="text-link">Ver ejecución validada <ArrowUpRight size={12}/></Link>}</div></div></div>)}</section>
    </fieldset>
  </form>
}

export function ExceptionsPage() {
  const [params, setParams] = useSearchParams(), selected = params.get('id')
  const [search, setSearch] = useState(''), [state, setState] = useState(''), [severity, setSeverity] = useState(''), [saved, setSaved] = useState(false)
  const [assigned, setAssigned] = useState(''), [overdue, setOverdue] = useState(false)
  const assignees = useQuery({ queryKey: ['exception-assignees'], queryFn: () => api<Collection>('/exceptions/assignees') })
  const filters = new URLSearchParams(); if (state) filters.set('state', state); if (severity) filters.set('priority', severity); if (assigned) filters.set('assigned_user_id', assigned); if (overdue) filters.set('overdue', 'true'); if (search) filters.set('search', search)
  const query = filters.toString()
  const cases = useQuery({ queryKey: ['exceptions', 'list', query], queryFn: () => api<Collection>(`/exceptions${query ? `?${query}` : ''}`) })
  const detail = useQuery({ queryKey: ['exception', selected], queryFn: () => api(`/exceptions/${selected}`), enabled: !!selected })
  const rows = cases.data?.items || [], filtered = rows.filter(item => `${item.title} ${item.display_id} ${item.owner}`.toLowerCase().includes(search.toLowerCase()) && (!state || item.state === state) && (!severity || (item.priority || item.severity) === severity))
  const open = rows.filter(item => ['OPEN', 'ASSIGNED', 'REOPENED', 'INVESTIGATING', 'PENDING_VALIDATION', 'WAITING_EXTERNAL'].includes(item.state))
  return <><PageHeading eyebrow="DEL HALLAZGO A LA RESOLUCIÓN" title="Excepciones" description="Investiga cada diferencia, asigna un responsable y conserva las decisiones de tu equipo."/>
    <div className="compact-stats"><div><ClipboardList size={19}/><strong>{number(open.length)}</strong><span>pendientes de resolución</span></div><div><ShieldCheck size={19}/><strong>{number(rows.filter(item => item.state === 'RESOLVED').length)}</strong><span>resueltas</span></div><div><Activity size={19}/><span>Historial y evidencia en cada caso</span></div></div>
    {saved && <Notice success>Los cambios de la excepción quedaron registrados.</Notice>}
    <section className="panel"><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar por caso, título o responsable…"/><div className="toolbar-right"><select aria-label="Filtrar por estado" value={state} onChange={event => setState(event.target.value)}><option value="">Todos los estados</option>{states.map(value => <option key={value} value={value}>{label(value)}</option>)}</select><select aria-label="Filtrar por prioridad" value={severity} onChange={event => setSeverity(event.target.value)}><option value="">Todas las prioridades</option>{['CRITICAL','HIGH','MEDIUM','LOW'].map(value => <option key={value} value={value}>{label(value)}</option>)}</select><select aria-label="Filtrar por responsable" value={assigned} onChange={event => setAssigned(event.target.value)}><option value="">Todos los responsables</option>{assignees.data?.items.map(entry => <option key={entry.id} value={entry.id}>{entry.name}</option>)}</select><label className="checkbox-label"><input type="checkbox" checked={overdue} onChange={event => setOverdue(event.target.checked)}/> Solo vencidas</label></div></div>
      {cases.isPending ? <Loading/> : cases.error ? <ErrorState error={cases.error} retry={() => cases.refetch()}/> : !filtered.length ? <Empty title="Sin excepciones en esta vista" description="Ajusta los filtros o crea una excepción desde los hallazgos de una ejecución."/> : <div className="table-scroll"><table><thead><tr><th>Caso</th><th>Módulo</th><th>Prioridad</th><th>Estado</th><th>Responsable</th><th>Fecha objetivo</th><th>Actualización</th><th/></tr></thead><tbody>{filtered.map(item => <tr key={item.id}><td><button className="table-primary text-button" onClick={() => { setSaved(false); setParams({ id: item.id }) }}>{item.title}</button><small className="table-subtitle mono">{item.display_id}</small></td><td><span className={`module-label ${item.module}`}>{label(item.module)}</span></td><td><Badge value={item.priority || item.severity}/></td><td><Badge value={item.state}/></td><td>{item.owner}</td><td>{date(item.due_at)}{item.overdue && <Badge value="WARNING">Vencida</Badge>}</td><td className="muted no-wrap">{date(item.updated_at)}</td><td><button className="icon-button" aria-label={`Abrir ${item.display_id}`} onClick={() => { setSaved(false); setParams({ id: item.id }) }}><ArrowUpRight size={17}/></button></td></tr>)}</tbody></table></div>}
    </section><Modal open={!!selected} onOpenChange={value => { if (!value) setParams({}) }} title={detail.data ? `${detail.data.display_id} · ${detail.data.title}` : 'Detalle de la excepción'} description="Los cambios se guardan con responsable, fecha y versión." wide>{detail.isPending ? <Loading/> : detail.error ? <ErrorState error={detail.error} retry={() => detail.refetch()}/> : detail.data && <ExceptionEditor key={`${detail.data.id}-${detail.data.version}`} item={detail.data} onSaved={() => setSaved(true)}/>}</Modal>
  </>
}

export function AuditPage() {
  const [search, setSearch] = useState(''), [actor, setActor] = useState('')
  const events = useQuery({ queryKey: ['audit'], queryFn: () => api<Collection>('/audit-events') })
  const rows = events.data?.items || [], filtered = rows.filter(item => `${item.message} ${item.event_type} ${item.subject_id}`.toLowerCase().includes(search.toLowerCase()) && (!actor || item.actor === actor))
  return <><PageHeading eyebrow="EVIDENCIA DE CADA ACCIÓN" title="Auditoría" description="Consulta quién realizó cada acción y sobre qué datos, controles o excepciones." action={<button className="button secondary" onClick={() => events.refetch()} disabled={events.isFetching}><RefreshCw size={16}/> Actualizar</button>}/><section className="panel"><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar acción o identificador…"/><select aria-label="Filtrar por actor" value={actor} onChange={event => setActor(event.target.value)}><option value="">Todos los responsables</option>{[...new Set(rows.map(item => String(item.actor)))].map(value => <option key={value}>{value}</option>)}</select></div>
    {events.isPending ? <Loading/> : events.error ? <ErrorState error={events.error} retry={() => events.refetch()}/> : !filtered.length ? <Empty title="Sin eventos en esta vista" description="Las cargas, ejecuciones y decisiones generan registros de auditoría."/> : <div className="table-scroll"><table><thead><tr><th>Fecha</th><th>Responsable</th><th>Acción</th><th>Referencia</th></tr></thead><tbody>{filtered.map(item => <tr key={item.id}><td className="no-wrap muted">{date(item.created_at)}</td><td>{item.actor}</td><td><strong>{item.message}</strong><small className="table-subtitle mono">{item.event_type}</small></td><td>{item.subject_type === 'run' ? <Link className="text-link" to={`/runs/${item.subject_id}`}>Ver ejecución <ArrowUpRight size={14}/></Link> : item.subject_type === 'exception' ? <Link className="text-link" to={`/exceptions?id=${item.subject_id}`}>Ver excepción <ArrowUpRight size={14}/></Link> : <span className="mono muted">{String(item.subject_id).slice(0, 12)}</span>}</td></tr>)}</tbody></table></div>}
  </section></>
}

export function RulesPage() {
  const [search, setSearch] = useState(''), [module, setModule] = useState('')
  const rules = useQuery({ queryKey: ['rules'], queryFn: () => api<Collection>('/rules') })
  const catalog = (rules.data?.items || []).map(item => item.code === 'EXACT_MATCH' ? { ...item, code: 'NUMERIC_TOLERANCE', name: 'Tolerancia numérica', description: 'Compara importes decimales con tolerancia absoluta. Las ejecuciones históricas conservan su configuración.', legacy_aliases: ['EXACT_MATCH'] } : item)
  const filtered = catalog.filter(item => `${item.name} ${item.code} ${item.description} ${(item.legacy_aliases || []).join(' ')}`.toLowerCase().includes(search.toLowerCase()) && (!module || module === item.module))
  return <><PageHeading eyebrow="CRITERIOS CLAROS Y REUTILIZABLES" title="Biblioteca de reglas" description="Estas son las comprobaciones disponibles en el prototipo. Configúralas desde cada módulo."/>
    <section className="panel"><div className="table-toolbar"><SearchBox value={search} onChange={setSearch} placeholder="Buscar una regla…"/><select aria-label="Filtrar por módulo" value={module} onChange={event => setModule(event.target.value)}><option value="">Todos los módulos</option>{['intake','recon','sentinel'].map(value => <option key={value} value={value}>{label(value)}</option>)}</select></div>
    {rules.isPending ? <Loading/> : rules.error ? <ErrorState error={rules.error} retry={() => rules.refetch()}/> : !filtered.length ? <Empty title="No encontramos reglas" description="Prueba otro término o cambia el módulo seleccionado."/> : <div className="table-scroll"><table><thead><tr><th>Regla</th><th>Qué comprueba</th><th>Módulo</th><th/></tr></thead><tbody>{filtered.map(item => <tr key={item.id}><td><div className="cell-with-icon"><span className="data-icon"><BookOpen size={18}/></span><div><strong>{item.name}</strong><small className="table-subtitle mono">{item.code}</small>{item.legacy_aliases?.length > 0 && <small className="table-subtitle">Alias histórico: <code>{item.legacy_aliases.join(', ')}</code></small>}</div></div></td><td>{item.description}</td><td><span className={`module-label ${item.module}`}>{label(item.module)}</span></td><td><Link to={`/${item.module}`} className="text-link">Configurar <ArrowUpRight size={15}/></Link></td></tr>)}</tbody></table></div>}
    </section></>
}

export function SettingsPage() {
  const canSystem = usePermission('system:read'), canUsers = usePermission('users:read')
  const [tab, setTab] = useState(canSystem ? 'system' : 'users')
  const engines = useQuery({ queryKey: ['engines'], queryFn: () => api('/system/engines'), refetchInterval: 10000, enabled: canSystem && tab === 'system' })
  return <><PageHeading eyebrow="TU ENTORNO DE TRABAJO" title="Configuración" description="Consulta el estado del procesamiento local y las cuentas disponibles."/>
    <div className="tabs">{canSystem && <button className={tab === 'system' ? 'active' : ''} onClick={() => setTab('system')}><Cpu size={16}/> Procesamiento y entorno</button>}{canUsers && <button className={tab === 'users' ? 'active' : ''} onClick={() => setTab('users')}><Users size={16}/> Usuarios locales</button>}</div>
    {!canSystem && !canUsers ? <Notice>Tu rol no permite administrar este entorno.</Notice> : tab === 'system' && canSystem ? engines.isPending ? <Loading/> : engines.error ? <ErrorState error={engines.error} retry={() => engines.refetch()}/> : <>
      <section className="panel"><div className="panel-heading"><div><h2>Estado del procesador</h2><p>Los controles se ejecutan en un proceso local independiente.</p></div><Badge value={engines.data.worker?.status}/></div><div className="profile-stats"><div><span>Última señal</span><strong style={{ fontSize: '1rem' }}>{date(engines.data.worker?.last_seen)}</strong></div><div><span>Máximo por carga</span><strong>{number(engines.data.limits?.max_upload_mb)} <small>MiB</small></strong></div><div><span>Filas por archivo</span><strong>{number(engines.data.limits?.max_rows)}</strong></div></div></section>
      <section className="panel" style={{ marginTop: 24 }}><div className="panel-heading"><div><h2>Motores de procesamiento</h2><p>Disponibilidad efectiva de esta instalación.</p></div></div><div className="table-scroll"><table><thead><tr><th>Motor</th><th>Disponibilidad</th><th>Versión</th><th>Capacidad</th></tr></thead><tbody>{engines.data.items?.map((item: RecordData) => <tr key={item.id}><td><strong>{item.name}</strong></td><td><Badge value={item.available ? 'ACTIVE' : 'PLANNED'}>{item.available ? 'Disponible' : 'Próxima etapa'}</Badge></td><td className="mono">{item.version || '—'}</td><td>{item.description}</td></tr>)}</tbody></table></div></section>
      <Notice>Las cargas admiten CSV, Excel XLSX, JSON, Parquet y TXT delimitado; los informes se descargan en Excel XLSX. Las reglas avanzadas se configuran desde cada módulo. Los permisos se aplican por rol y se administran desde Usuarios locales.</Notice>
    </> : canUsers ? <UsersPanel/> : <Notice>Tu rol no permite consultar usuarios.</Notice>}
  </>
}
