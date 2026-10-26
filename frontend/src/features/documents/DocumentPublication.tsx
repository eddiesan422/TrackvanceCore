import { useState } from 'react'
import { Link } from 'react-router-dom'
import { post } from '../../api/client'
import type { RecordData } from '../../api/client'
import { ErrorState, Notice } from '../../components/ui'
import { publicationBlockReason } from './documentReview'

export default function DocumentPublication({ document: item, outputs = {}, canPublish, refresh }: {
  document: RecordData; outputs?: RecordData; canPublish: boolean; refresh: () => Promise<unknown>
}) {
  const [requestKey] = useState(() => crypto.randomUUID())
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [notice, setNotice] = useState('')
  const reason = publicationBlockReason(item)
  return <section className="document-publication" aria-label="Publicación de la revisión">
    <h3>Publicación y datasets</h3>
    <p>Guardar o confirmar una revisión no acredita por sí solo una publicación. La política individual configurada puede publicar cuando sus controles se cumplan.</p>
    {(item.publications || []).map((publication: RecordData) => <div key={publication.batch_id}>
      <span>Lote {publication.batch_id} · extracción {publication.extraction_id}</span>
      {Object.entries(publication.versions || {}).map(([structure, version]) => outputs[structure] ? <Link key={structure} to={`/datasets/${outputs[structure]}?version=${version}`}>{structure}: abrir versión publicada</Link> : null)}
    </div>)}
    {canPublish ? <>
      <button type="button" className="button primary" disabled={busy || !!reason} aria-describedby={reason ? 'document-publish-blocked' : undefined} onClick={async () => {
        setBusy(true); setError(null); setNotice('')
        try {
          const batch = await post(`/documents/families/${item.family_id}/batches`, { idempotency_key: requestKey, template_id: item.template_id, document_ids: [item.id], ...(item.publications?.length ? { correction: true } : {}) })
          setNotice(`Lote ${batch.id} registrado. Estado de publicación: ${batch.status}.`)
          await refresh()
        } catch (failure) { setError(failure) } finally { setBusy(false) }
      }}>{item.publications?.length ? 'Publicar revisión como corrección' : 'Publicar esta revisión en datasets'}</button>
      {reason && <p id="document-publish-blocked">{reason}</p>}
    </> : <p>Publicar datasets requiere permiso documental de publicación.</p>}
    {notice && <Notice>{notice}</Notice>}{!!error && <ErrorState error={error}/>}
  </section>
}
