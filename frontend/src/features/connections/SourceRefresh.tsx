import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { RefreshCw } from 'lucide-react'
import { post } from '../../api/client'
import { usePermission } from '../../app/session'
import { ErrorState, Notice } from '../../components/ui'
import './connections.css'

type ConnectionState = 'ACTIVE' | 'DISABLED' | 'DELETED'

export function SourceRefresh({ datasetId, connectionId, connectionState = 'ACTIVE', onRefreshed }: { datasetId: string; connectionId: string; connectionState?: ConnectionState; onRefreshed: (versionId: string) => void }) {
  const canWrite = usePermission('datasets:write'), canUse = usePermission('connections:use'), cache = useQueryClient()
  const available = connectionState === 'ACTIVE'
  const refresh = useMutation({
    mutationFn: () => post<{ id: string; version: number }>(`/datasets/${datasetId}/refresh-source`),
    onSuccess: version => { cache.invalidateQueries({ queryKey: ['dataset', datasetId] }); cache.invalidateQueries({ queryKey: ['datasets'] }); cache.invalidateQueries({ queryKey: ['dashboard'] }); onRefreshed(version.id) },
  })
  const unavailableMessage = connectionState === 'DISABLED'
    ? 'La conexión de origen está deshabilitada. Los snapshots existentes permanecen disponibles; habilítala para crear otra versión.'
    : connectionState === 'DELETED'
      ? 'La conexión de origen fue eliminada. Los snapshots existentes permanecen disponibles, pero ya no se puede consultar la fuente.'
      : null
  return <div className="source-refresh"><div className="connection-actions">{connectionState !== 'DELETED' && <Link className="button secondary" to={`/connections/${connectionId}`}>Ver conexión de origen</Link>}<button className="button secondary" disabled={!available || !canWrite || !canUse || refresh.isPending} onClick={() => refresh.mutate()}><RefreshCw size={15}/>{refresh.isPending ? 'Consultando fuente…' : 'Nueva versión desde la fuente'}</button></div>{unavailableMessage && <Notice>{unavailableMessage}</Notice>}{refresh.error && <ErrorState error={refresh.error}/>} {refresh.data && <Notice success>Versión {refresh.data.version} creada desde la fuente. Los snapshots anteriores permanecen intactos.</Notice>}</div>
}
