import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, post } from '../../api/client'
import type { RecordData } from '../../api/client'
import { usePermission, useSession } from '../../app/session'
import { date, ErrorState, Field, Loading, Notice } from '../../components/ui'

function AutomaticEditor({ monitorId, data }: { monitorId: string; data: RecordData }) {
  const cache = useQueryClient(), session = useSession()
  const canConfigure = usePermission('sentinel:schedule'), canExecute = usePermission('sentinel:execute')
  const [enabled, setEnabled] = useState(data.enabled), [hours, setHours] = useState(String(data.review_interval_seconds / 3600)), [assignSelf, setAssignSelf] = useState(false)
  const save = useMutation({ mutationFn: () => post(`/monitors/${monitorId}/automatic`, {
    enabled, expected_version: data.version, review_interval_seconds: Math.round(Number(hours) * 3600),
    ...(assignSelf ? { responsible_user_id: session?.user.id } : {}),
  }), onSuccess: () => { cache.invalidateQueries({ queryKey: ['monitor-automatic', monitorId] }); cache.invalidateQueries({ queryKey: ['configurations', 'sentinel'] }) } })
  return <div className="form-stack"><Notice>Cada versión completa se evalúa con el responsable autorizado. La revisión de frescura y estado usa metadata, sin volver a recorrer las filas.</Notice>
    <form className="form-stack" onSubmit={event => { event.preventDefault(); save.mutate() }}><div className="schedule-fields"><Field label="Revisión ligera (horas)"><input type="number" required min="1" max="744" step="1" value={hours} disabled={!canConfigure} onChange={event => setHours(event.target.value)}/></Field><label><input type="checkbox" checked={enabled} disabled={!canConfigure || !canExecute} onChange={event => setEnabled(event.target.checked)}/> Evaluar nuevas versiones y revisar frescura</label></div>
      <label><input type="checkbox" checked={assignSelf} disabled={!canConfigure || !canExecute} onChange={event => setAssignSelf(event.target.checked)}/> Asignarme como responsable</label>
      <p className="muted">Revisión {data.version} · próxima revisión ligera {date(data.next_review_at)}. Las reglas se ajustan mediante Nueva versión.</p>
      {save.error && <ErrorState error={save.error}/>}<div><button className="button secondary small" disabled={!canConfigure || save.isPending || enabled && (!canExecute || data.requires_executor_assignment && !assignSelf)}>{save.isPending ? 'Guardando…' : 'Guardar monitoreo automático'}</button></div>
    </form>{data.reviews?.length > 0 && <details><summary>Revisiones ligeras recientes</summary><div className="table-scroll"><table><thead><tr><th>Fecha</th><th>Estado</th><th>Edad (horas)</th><th>Versión</th></tr></thead><tbody>{(data.reviews as RecordData[]).map(review => <tr key={review.id}><td>{date(review.observed_at)}</td><td>{({ FRESH: 'Fresco', STALE: 'Antiguo', BLOCKED: 'Bloqueado', PAUSED: 'Pausado', WAITING_FIRST_VERSION: 'Esperando primera versión' } as Record<string, string>)[review.status] || review.status}{review.reason_code && <small className="reason-code">{review.reason_code}</small>}</td><td>{review.metrics?.age_hours == null ? 'No disponible' : String(review.metrics.age_hours)}</td><td className="mono">{review.dataset_version_id || '—'}</td></tr>)}</tbody></table></div></details>}
  </div>
}

export function AutomaticMonitorPanel({ monitorId }: { monitorId: string }) {
  const automatic = useQuery({ queryKey: ['monitor-automatic', monitorId], queryFn: () => api<RecordData | null>(`/monitors/${monitorId}/automatic`) })
  if (automatic.isPending) return <Loading text="Cargando monitoreo automático…"/>
  if (automatic.error) return <ErrorState error={automatic.error} retry={() => automatic.refetch()}/>
  if (!automatic.data) return null
  const data = automatic.data
  const state = data.state === 'WAITING_FIRST_VERSION' ? 'Esperando primera versión' : data.state === 'PAUSED' ? 'Monitoreo automático pausado' : data.requires_executor_assignment ? 'Requiere responsable autorizado' : 'Monitoreo automático activo'
  return <><p className="muted">{state}</p><details className="monitor-schedule"><summary>Monitoreo automático por dataset</summary><div className="monitor-schedule-content"><AutomaticEditor key={data.version} monitorId={monitorId} data={data}/></div></details></>
}
