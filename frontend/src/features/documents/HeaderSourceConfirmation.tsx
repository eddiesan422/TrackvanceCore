import { useEffect, useState } from 'react'
import { ErrorState, Field, Notice } from '../../components/ui'

export type HeaderConfirmation = {
  observation_id: string; expected_observation_hash: string; expected_payload_hash: string
  decision: 'SOURCE_HEADER_ONLY_NO_PRODUCTS_CONFIRMED'; source_note: string
}
type Observation = { observation_id: string; observation_hash: string; table: string; page: number; region: number[]; reason: string; text: string; resolved: boolean }
export type HeaderReview = { payload_hash: string; eligible: boolean; observations: Observation[] }

export default function HeaderSourceConfirmation({ review, sourceUrl, canView = true, busy, pendingChanges, onConfirm }: {
  review?: HeaderReview; sourceUrl: (page: number) => string; canView?: boolean; busy: boolean
  pendingChanges: boolean; onConfirm: (value: HeaderConfirmation) => Promise<void>
}) {
  const [selected, setSelected] = useState<Observation | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [attested, setAttested] = useState(false)
  const [note, setNote] = useState('')
  const [error, setError] = useState<unknown>(null)
  const [openedPayloadHash, setOpenedPayloadHash] = useState('')
  useEffect(() => {
    setSelected(null); setLoaded(false); setAttested(false); setNote(''); setError(null); setOpenedPayloadHash('')
  }, [review?.payload_hash, canView])
  if (!review) return null
  const eligible = review.eligible && !pendingChanges
  return <section aria-label="Comprobar encabezados contra el original">
    <h3>Encabezados pendientes de comprobar</h3>
    <p>Abre cada región y comprueba en el original que contiene sólo encabezado o contexto, sin productos. La observación automática y sus palabras se conservan.</p>
    {!eligible && <Notice>Guarda primero todos los valores válidos y confirmados. Las regiones del cuerpo pendientes deben resolverse antes de confirmar encabezados.</Notice>}
    {!canView && <Notice>Se requiere permiso de descarga para comprobar el original.</Notice>}
    {review.observations.map(item => <div key={item.observation_id}>
      <p>{item.reason === 'CONTINUATION_CONTEXT_UNVERIFIED' ? 'Contexto de continuación' : 'Encabezado de tabla'} · {item.table} · página {item.page} · {item.resolved ? 'Confirmado contra el original' : 'Sin confirmar'}</p>
      <p>Texto observado: {item.text}</p>
      <button type="button" className="button secondary small" disabled={busy || !canView} onClick={() => { setSelected(item); setLoaded(false); setAttested(false); setNote(''); setError(null); setOpenedPayloadHash(review.payload_hash) }}>Abrir región del original</button>
    </div>)}
    {selected && canView && <div>
      <div className="document-image"><img key={`${selected.observation_id}:${review.payload_hash}`} alt={`Original para comprobar encabezado, página ${selected.page}`} src={sourceUrl(selected.page)} onLoad={() => setLoaded(true)} onError={() => setLoaded(false)}/>
        <span className="document-origin-region" style={{ left: `${selected.region[0] * 100}%`, top: `${selected.region[1] * 100}%`, width: `${(selected.region[2] - selected.region[0]) * 100}%`, height: `${(selected.region[3] - selected.region[1]) * 100}%` }}/>
      </div>
      <Field label="Comprobación del encabezado en la fuente"><textarea value={note} maxLength={2000} disabled={!loaded || !eligible || busy || selected.resolved || openedPayloadHash !== review.payload_hash} onChange={event => setNote(event.target.value)}/></Field>
      <label><input type="checkbox" checked={attested} disabled={!loaded || !eligible || busy || selected.resolved || openedPayloadHash !== review.payload_hash} onChange={event => setAttested(event.target.checked)}/> He comprobado la región en el original: sólo encabezado o contexto y ningún producto.</label>
      <button type="button" className="button primary" disabled={!loaded || !eligible || !attested || !note.trim() || busy || selected.resolved || openedPayloadHash !== review.payload_hash} onClick={async () => {
        try {
          await onConfirm({ observation_id: selected.observation_id, expected_observation_hash: selected.observation_hash,
            expected_payload_hash: review.payload_hash, decision: 'SOURCE_HEADER_ONLY_NO_PRODUCTS_CONFIRMED', source_note: note.trim() })
          setSelected(null); setAttested(false); setLoaded(false); setNote('')
        } catch (error) { setError(error) }
      }}>Confirmar encabezado contra original</button>
      {!!error && <ErrorState error={error}/>}
    </div>}
  </section>
}
