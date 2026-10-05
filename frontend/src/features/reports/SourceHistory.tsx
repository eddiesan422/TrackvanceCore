import { useMutation } from '@tanstack/react-query'
import { api, ApiError } from '../../api/client'
import { ErrorState, Pagination } from '../../components/ui'
import type { ReportSource, SourceCandidate } from './types'

export function SourceHistory({ source, candidate, onLoaded }: { source: ReportSource; candidate?: SourceCandidate; onLoaded: (candidate: SourceCandidate) => void }) {
  const history = useMutation({ mutationFn: async (offsets: { revision: number; version: number }) => {
    const query = new URLSearchParams({ contract_id: source.contract_id, offset: '0', limit: '1', revision_offset: String(offsets.revision), version_offset: String(offsets.version) })
    const value = await api<{ items: SourceCandidate[] }>(`/reports/sources?${query}`)
    const found = value.items.find(item => item.input_dataset_id === source.input_dataset_id && item.contract_id === source.contract_id)
    if (!found) throw new ApiError('La fuente ya no aparece en el catálogo autorizado. Vuelve a resolver o selecciona otra fuente.', 409)
    return found
  }, onSuccess: value => onLoaded(value) })
  const revisionOffset = candidate?.contract_revision_offset || 0, versionOffset = candidate?.version_offset || 0
  return <div className="report-source-history">{!candidate && <button className="text-button" disabled={history.isPending} onClick={() => history.mutate({ revision: 0, version: 0 })}>Consultar historial de esta fuente</button>}{candidate && <>
    {(candidate.contract_revision_total || 0) > (candidate.contract_revision_limit || 100) && <fieldset disabled={history.isPending}><legend>Página de revisiones del contrato</legend><Pagination offset={revisionOffset} total={candidate.contract_revision_total!} limit={candidate.contract_revision_limit || 100} onChange={revision => history.mutate({ revision, version: versionOffset })}/></fieldset>}
    {source.policy === 'SPECIFIC' && (candidate.version_total || 0) > (candidate.version_limit || 100) && <fieldset disabled={history.isPending}><legend>Página de versiones de entrada</legend><Pagination offset={versionOffset} total={candidate.version_total!} limit={candidate.version_limit || 100} onChange={version => history.mutate({ revision: revisionOffset, version })}/></fieldset>}
  </>}{history.isPending && <p className="muted" role="status">Consultando página de historia…</p>}{history.error && <ErrorState error={history.error}/>}</div>
}
