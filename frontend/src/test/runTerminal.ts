type RunProjection = {
  status?: unknown
  decision?: unknown
  metrics?: unknown
}

export type DeliveryEvidenceState = 'WAITING' | 'PUBLISHED' | 'REJECTED'

/** A durable remote commit can precede the separate local evidence commit. */
export function deliveryEvidenceState(run: RunProjection): DeliveryEvidenceState {
  if (run.status === 'QUEUED' || run.status === 'RUNNING') return 'WAITING'
  if (run.status !== 'SUCCESS' || run.decision !== 'COMMITTED') return 'REJECTED'
  if (run.metrics == null) return 'WAITING'
  if (typeof run.metrics !== 'object' || Array.isArray(run.metrics)) return 'REJECTED'

  const metrics = run.metrics as Record<string, unknown>
  if (metrics.evidence_status === 'PENDING_REPAIR') return 'REJECTED'
  if (metrics.evidence_status != null && metrics.evidence_status !== 'VALID') return 'REJECTED'
  if (metrics.receipt_artifact_id == null || metrics.receipt_artifact_id === '') return 'WAITING'
  return typeof metrics.receipt_artifact_id === 'string' && metrics.receipt_artifact_id.trim()
    ? 'PUBLISHED'
    : 'REJECTED'
}
