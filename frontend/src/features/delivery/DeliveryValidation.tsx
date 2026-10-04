import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api, post } from '../../api/client'
import { Badge, date, ErrorState, Notice } from '../../components/ui'
import type { DeliveryDraft, DeliveryPreflight } from './types'

interface Validation {
  id: string; status: string; stage: string; dataset_version_id: string
  created_at: string; finished_at?: string; error?: string; result?: DeliveryPreflight
  draft?: DeliveryDraft
}

function canonical(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`
  if (value && typeof value === 'object') return `{${Object.entries(value).sort(([a], [b]) => a.localeCompare(b)).map(([key, item]) => `${JSON.stringify(key)}:${canonical(item)}`).join(',')}}`
  return JSON.stringify(value)
}

export function DeliveryValidation({ draft, ready, onUse }: {
  draft: DeliveryDraft; ready: boolean; onUse: (result: DeliveryPreflight, id: string) => void
}) {
  const cache = useQueryClient()
  const history = useQuery({ queryKey: ['delivery-validations'], queryFn: () => api<{ items: Validation[]; total: number }>('/delivery/validations'),
    refetchInterval: query => query.state.data?.items.some(item => ['QUEUED', 'RUNNING'].includes(item.status)) ? 1500 : false })
  const refresh = () => cache.invalidateQueries({ queryKey: ['delivery-validations'] })
  const create = useMutation({ mutationFn: () => post<Validation>('/delivery/validations', draft), onSuccess: refresh })
  const cancel = useMutation({ mutationFn: (id: string) => post(`/delivery/validations/${id}/cancel`), onSuccess: refresh })
  const items = history.data?.items.filter(item => item.dataset_version_id === draft.dataset_version_id) || []
  return <section className="delivery-sample">
    <h3>Preflight completo en segundo plano</h3>
    <Notice>La validación recorre todas las filas. El historial y el procesamiento permanecen disponibles al salir de esta página. La publicación requiere un resultado aprobado para este mismo borrador y versión.</Notice>
    <button type="button" className="button primary" disabled={!ready || create.isPending} onClick={() => create.mutate()}>{create.isPending ? 'Registrando…' : 'Registrar preflight completo'}</button>
    {create.error && <ErrorState error={create.error}/>} {cancel.error && <ErrorState error={cancel.error}/>} {history.error && <ErrorState error={history.error} retry={() => history.refetch()}/>}
    {!!items.length && <div className="table-scroll"><table><thead><tr><th>Validación</th><th>Estado / etapa</th><th>Resultado</th><th>Acción</th></tr></thead><tbody>{items.map(item => {
      const active = ['QUEUED', 'RUNNING'].includes(item.status)
      const matches = item.draft && canonical(item.draft) === canonical(draft)
      return <tr key={item.id}><td><code>{item.id}</code><small className="table-subtitle">{date(item.created_at)}</small></td><td><Badge value={item.status}/><small className="table-subtitle">{item.stage}</small>{item.error && <small>{item.error}</small>}</td><td>{item.result ? <Badge value={item.result.status}/> : 'Pendiente'}</td><td>{active ? <button type="button" className="text-button" disabled={cancel.isPending} onClick={() => cancel.mutate(item.id)}>Cancelar validación</button> : item.result && <button type="button" className="button secondary small" disabled={!matches} title={!matches ? 'El borrador cambió desde esta validación.' : undefined} onClick={() => onUse(item.result!, item.id)}>Ver y usar resultado</button>}</td></tr>
    })}</tbody></table></div>}
  </section>
}
