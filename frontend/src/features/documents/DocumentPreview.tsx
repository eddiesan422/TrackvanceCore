import { useEffect, useState } from 'react'
import type { ReactNode } from 'react'

const BUSY_DELAYS = [250, 500, 1000, 2000, 4000]
type Preview = { source: string; revision: number; url?: string; loaded?: boolean; error?: string }

export default function DocumentPreview({ source, alt, zoom, children }: {
  source: string; alt: string; zoom: number; children?: ReactNode
}) {
  const [revision, setRevision] = useState(0)
  const [preview, setPreview] = useState<Preview>({ source: '', revision: -1 })
  const current = preview.source === source && preview.revision === revision ? preview : undefined

  useEffect(() => {
    const controller = new AbortController()
    let active = true
    let url: string | undefined
    setPreview({ source, revision })
    const load = async () => {
      try {
        for (let attempt = 0; ; attempt++) {
          const response = await fetch(source, { credentials: 'same-origin', signal: controller.signal })
          if (!active) return
          if (!response.ok) {
            const body: { error?: { code?: string } } | null = await response.json().catch(() => null)
            if (!active) return
            const busy = response.status === 429 && body?.error?.code === 'DOCUMENT_PREVIEW_BUSY'
            if (busy && attempt < BUSY_DELAYS.length) {
              await new Promise<void>((resolve, reject) => {
                const abort = () => { window.clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')) }
                const timer = window.setTimeout(() => {
                  controller.signal.removeEventListener('abort', abort)
                  resolve()
                }, BUSY_DELAYS[attempt])
                controller.signal.addEventListener('abort', abort, { once: true })
              })
              continue
            }
            if (response.status === 401 || response.status === 403) window.dispatchEvent(new Event('trackvance:session-refresh'))
            throw new Error(busy ? 'La vista previa sigue ocupada. Puedes volver a intentarlo.' : 'No se pudo cargar la vista previa del original.')
          }
          const blob = await response.blob()
          if (!active) return
          url = URL.createObjectURL(blob)
          setPreview({ source, revision, url })
          return
        }
      } catch (error) {
        if (active) setPreview({ source, revision, error: error instanceof Error ? error.message : 'No se pudo cargar la vista previa del original.' })
      }
    }
    void load()
    return () => {
      active = false
      controller.abort()
      if (url) URL.revokeObjectURL(url)
    }
  }, [source, revision])

  return <>
    {!current?.loaded && !current?.error && <p role="status">Cargando página del original…</p>}
    {current?.error && <div role="alert"><p>{current.error}</p><button type="button" className="button secondary small" onClick={() => setRevision(value => value + 1)}>Reintentar vista previa</button></div>}
    <div className="document-image" style={{ width: `${zoom}%` }}>
      <img key={`${source}:${revision}`} alt={alt} src={current?.error ? undefined : current?.url}
        onLoad={() => setPreview(value => value.source === source && value.revision === revision ? { ...value, loaded: true } : value)}
        onError={() => setPreview(value => value.source === source && value.revision === revision ? { ...value, loaded: false, error: 'La imagen de la vista previa no se pudo abrir.' } : value)}/>
      {current?.loaded && children}
    </div>
  </>
}
