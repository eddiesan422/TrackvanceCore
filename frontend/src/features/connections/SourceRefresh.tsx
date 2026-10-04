import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'
import { api } from '../../api/client'
import { AcquisitionStatus } from '../datasets/Acquisitions'
import type { Acquisition } from '../datasets/Acquisitions'
import { usePermission } from '../../app/session'
import { ErrorState, Notice } from '../../components/ui'
import './connections.css'

type ConnectionState = 'ACTIVE' | 'DISABLED' | 'DELETED'

export function SourceRefresh({ datasetId, connectionId, connectionState = 'ACTIVE', onRefreshed }: { datasetId: string; connectionId: string; connectionState?: ConnectionState; onRefreshed: (versionId: string) => void }) {
  const canWrite = usePermission('datasets:write'), canUse = usePermission('connections:use'), cache = useQueryClient()
  const available = connectionState === 'ACTIVE'
  const [acquisitionId, setAcquisitionId] = useState<string | null>(null), [completed, setCompleted] = useState(false)
  const requestKey = useRef(crypto.randomUUID())
  const refresh = useMutation({
    mutationFn: () => api<Acquisition>(`/datasets/${datasetId}/acquisitions/refresh`, { method: 'POST', headers: { 'Idempotency-Key': requestKey.current } }),
    onSuccess: run => { setAcquisitionId(run.id); setCompleted(false); requestKey.current = crypto.randomUUID(); cache.invalidateQueries({ queryKey: ['acquisitions'] }) },
  })
  const unavailableMessage = connectionState === 'DISABLED'
    ? 'La conexión de origen está deshabilitada. Los snapshots existentes permanecen disponibles; habilítala para crear otra versión.'
    : connectionState === 'DELETED'
      ? 'La conexión de origen fue eliminada. Los snapshots existentes permanecen disponibles, pero ya no se puede consultar la fuente.'
      : null
  return <div className="source-refresh"><div className="connection-actions">{connectionState !== 'DELETED' && <Link className="button secondary" to={`/connections/${connectionId}`}>Ver conexión de origen</Link>}<button className="button secondary" disabled={!available || !canWrite || !canUse || refresh.isPending || (!!acquisitionId && !completed)} onClick={() => refresh.mutate()}><RefreshCw size={15}/>{refresh.isPending ? 'Registrando adquisición…' : 'Nueva versión desde la fuente'}</button></div>{unavailableMessage && <Notice>{unavailableMessage}</Notice>}{refresh.error && <ErrorState error={refresh.error}/>} {acquisitionId && <AcquisitionStatus id={acquisitionId} onFinished={() => setCompleted(true)} onCompleted={onRefreshed}/> }</div>
}
import { useRef, useState } from 'react'
