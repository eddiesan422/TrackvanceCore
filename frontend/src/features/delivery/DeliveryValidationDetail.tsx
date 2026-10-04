import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { api, post } from '../../api/client'
import type { RecordData } from '../../api/client'
import { usePermission } from '../../app/session'
import { Badge, date, ErrorState, Loading, Notice, PageHeading } from '../../components/ui'
import { PreflightChecks } from './PreflightChecks'

export function DeliveryValidationDetail() {
  const { id } = useParams(), cache = useQueryClient(), canConfigure = usePermission('delivery:configure')
  const validation = useQuery({ queryKey: ['delivery-validation', id], queryFn: () => api<RecordData>(`/delivery/validations/${id}`),
    refetchInterval: query => ['QUEUED', 'RUNNING'].includes(query.state.data?.status) ? 1500 : false })
  const cancel = useMutation({ mutationFn: () => post(`/delivery/validations/${id}/cancel`), onSuccess: () => cache.invalidateQueries({ queryKey: ['delivery-validation', id] }) })
  if (validation.isPending) return <Loading text="Consultando preflight completo…"/>
  if (validation.error) return <ErrorState error={validation.error} retry={() => validation.refetch()}/>
  const data = validation.data!, pending = ['QUEUED', 'RUNNING'].includes(data.status)
  return <><PageHeading back="/delivery" eyebrow="DATA DELIVERY / VALIDACIÓN COMPLETA" title="Preflight completo" description="Trabajo persistido de solo lectura sobre toda la población."/>
    <section className="panel form-stack"><div className="detail-summary"><Badge value={data.status}/><span>{data.stage}</span><code>{data.id}</code></div>
      <p>Registrado {date(data.created_at)}{data.finished_at && ` · Terminado ${date(data.finished_at)}`}</p>
      <p>DatasetVersion <code>{data.dataset_version_id}</code>{data.dataset_id && <> · <Link to={`/datasets/${data.dataset_id}`}>Ver dataset y versiones</Link></>}</p>
      <Notice>SUCCESS indica que la validación terminó. Solo el resultado PASS permite publicar este mismo borrador. El worker vuelve a validar destino y permisos antes de una entrega.</Notice>
      {pending && canConfigure && <button className="button secondary" disabled={cancel.isPending} onClick={() => cancel.mutate()}>Cancelar validación</button>}
      {data.error && <ErrorState error={new Error(data.error)}/>} {cancel.error && <ErrorState error={cancel.error}/>}
      {data.result && <><Badge value={data.result.status}/><PreflightChecks checks={data.result.checks}/></>}
    </section>
  </>
}
