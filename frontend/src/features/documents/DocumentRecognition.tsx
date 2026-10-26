import { useState } from 'react'
import { post } from '../../api/client'
import type { RecordData } from '../../api/client'
import { ErrorState, Field, Notice } from '../../components/ui'

export default function DocumentRecognition({ document: item, canDownload, canReplay, refresh, resource = 'originals' }: {
  document: RecordData; canDownload: boolean; canReplay: boolean; refresh: () => Promise<unknown>; resource?: 'originals' | 'builder/references'
}) {
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [notice, setNotice] = useState('')
  const artifacts: RecordData[] = item.recognition_artifacts || []
  if (!artifacts.length) return null
  const blocked = ['QUEUED', 'RUNNING'].includes(item.status) || item.execution?.resources_released === false
  return <section className="document-recognition panel" aria-label="Reconocimiento original conservado">
    <h3>Reconocimiento original conservado</h3>
    <p>Los resultados originales y la extracción adaptada se conservan por separado. Una nueva adaptación genera otra revisión.</p>
    <details><summary>Resultados por página y etapa ({artifacts.length})</summary><ul>{artifacts.map((artifact, index) => <li key={artifact.artifact_id || index}>
      <strong>{artifact.kind} · {artifact.page ? `página ${artifact.page}` : 'documento'} · {artifact.stage}</strong>
      <small>Intento {artifact.attempt_id} · SHA-256 {artifact.sha256} · {artifact.bytes} bytes</small>
      {canDownload ? <a download href={`/api/v1/documents/${resource}/${encodeURIComponent(item.id)}/recognition-artifacts/${encodeURIComponent(artifact.artifact_id)}/content`}>Descargar resultado original de {artifact.page ? `página ${artifact.page}` : 'documento'}</a> : <small>La descarga del resultado original requiere permiso documental de descarga.</small>}
    </li>)}</ul></details>
    {canReplay && resource === 'originals' && <form onSubmit={async event => {
      event.preventDefault(); setBusy(true); setError(null); setNotice('')
      try {
        const result = await post(`/documents/originals/${item.id}/replay`, { expected_extraction_id: item.extraction?.id || null, reason: reason.trim() })
        setNotice(`Adaptación guardada como revisión ${result.extraction?.revision ?? 'nueva'}. Estado: ${result.status}. El reconocimiento anterior permanece conservado.`)
        setReason(''); await refresh()
      } catch (failure) { setError(failure) } finally { setBusy(false) }
    }}>
      <Field label="Motivo de la nueva adaptación"><input value={reason} maxLength={1800} disabled={busy || blocked} onChange={event => setReason(event.target.value)}/></Field>
      <button type="submit" className="button secondary" disabled={busy || blocked || !reason.trim()}>Adaptar resultados guardados sin nueva inferencia</button>
      {blocked && <small>Espera el cierre de la ejecución activa y la liberación de sus recursos antes de adaptar sus resultados.</small>}
    </form>}
    {notice && <Notice success>{notice}</Notice>}{!!error && <ErrorState error={error}/>}
  </section>
}
