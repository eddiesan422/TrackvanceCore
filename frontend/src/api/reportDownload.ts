import { api, ApiError } from './client'

export type TransferPhase = 'PREPARING' | 'GENERATING' | 'TRANSFERRING' | 'VERIFYING' | 'SUCCESS' | 'INTERRUPTED'
type WritableFile = { write: (data: Uint8Array) => Promise<void>; close: () => Promise<void>; abort: () => Promise<void> }
type PickerWindow = Window & { showSaveFilePicker?: (options: { suggestedName: string }) => Promise<{ createWritable: () => Promise<WritableFile> }> }
const fallbackBytes = 32 * 1024 * 1024

const cancelled = () => new ApiError('Transferencia cancelada; el archivo está incompleto.', 0, undefined, 'TRANSFER_CANCELLED')
function interruptible<T>(operation: Promise<T>, signal?: AbortSignal | null): Promise<T> {
  if (!signal) return operation
  return new Promise((resolve, reject) => {
    const abort = () => reject(cancelled())
    signal.addEventListener('abort', abort, { once: true })
    operation.then(resolve, reject).finally(() => signal.removeEventListener('abort', abort)).catch(() => {})
    if (signal.aborted) abort()
  })
}

async function generationFailure(identity: string): Promise<ApiError | undefined> {
  const controller = new AbortController(), timer = window.setTimeout(() => controller.abort(), 3000)
  try {
    while (!controller.signal.aborted) {
      const run = await api(`/reports/executions/${encodeURIComponent(identity)}`, { signal: controller.signal })
      if (['FAILED', 'CANCELLED', 'INTERRUPTED'].includes(run.status)) {
        return run.error_code ? new ApiError(run.error_message || 'La generación falló; archivo incompleto.', 422, undefined, run.error_code) : undefined
      }
      if (run.status === 'SUCCESS') return undefined // A local write can fail after valid server generation.
      await new Promise(resolve => window.setTimeout(resolve, 100))
    }
  } catch { /* A lost connection cannot establish a generator cause. */ }
  finally { window.clearTimeout(timer) }
  return undefined
}

/** Called before the first network await, retaining the user's activation. */
export async function prepareReportFile(name: string): Promise<WritableFile | undefined> {
  const picker = (window as PickerWindow).showSaveFilePicker
  if (!picker || !window.isSecureContext) return undefined
  try { return await (await picker.call(window, { suggestedName: name })).createWritable() }
  catch { throw new ApiError('Selección de archivo cancelada; la descarga no comenzó.', 0, undefined, 'TRANSFER_CANCELLED') }
}

export async function receiveReport(response: Response, fileName: string, writable: WritableFile | undefined,
  signal?: AbortSignal | null, phase: (value: TransferPhase) => void = () => {}) {
  const identity = response.headers.get('X-Report-Execution-Id')
  const reader = response.body?.getReader()
  const chunks: BlobPart[] = []
  let bytes = 0
  let cleanupPromise: Promise<void> | undefined
  const cleanup = () => {
    if (!cleanupPromise) cleanupPromise = new Promise<void>(resolve => {
      const timer = window.setTimeout(resolve, 1500)
      void Promise.allSettled([Promise.resolve().then(() => reader?.cancel()), Promise.resolve().then(() => writable?.abort())])
        .then(() => { window.clearTimeout(timer); resolve() })
    })
    return cleanupPromise
  }
  const abortTransfer = () => { void cleanup() }
  signal?.addEventListener('abort', abortTransfer, { once: true })
  try {
    if (!identity || !/^[a-f0-9-]{36}$/i.test(identity) || !reader) throw new ApiError('La respuesta no identifica una ejecución verificable.', 0, undefined, 'REPORT_STREAM_UNVERIFIED')
    phase('TRANSFERRING')
    while (true) {
      if (signal?.aborted) throw new ApiError('Transferencia cancelada; el archivo está incompleto.', 0, undefined, 'TRANSFER_CANCELLED')
      const { value, done } = await interruptible(reader.read(), signal)
      if (done) break
      if (signal?.aborted) throw cancelled()
      bytes += value.byteLength
      if (writable) await interruptible(writable.write(value), signal) // Backpressure: at most one network chunk pending.
      else {
        if (bytes > fallbackBytes) throw new ApiError('Este navegador admite descargas de hasta 32 MiB en memoria. Para esta salida, usa Chrome o Edge con guardado directo de archivos en un contexto seguro.', 422, undefined, 'REPORT_BROWSER_MEMORY_LIMIT')
        chunks.push(value.slice().buffer as ArrayBuffer)
      }
    }
    phase('VERIFYING')
    const deadline = Date.now() + 15000
    while (true) {
      if (signal?.aborted) throw new ApiError('Transferencia cancelada; el archivo está incompleto.', 0, undefined, 'TRANSFER_CANCELLED')
      const poll = new AbortController(), timeout = window.setTimeout(() => poll.abort(), Math.max(1, deadline - Date.now()))
      const abortPoll = () => poll.abort()
      signal?.addEventListener('abort', abortPoll, { once: true })
      let run
      try { run = await api(`/reports/executions/${encodeURIComponent(identity)}`, { signal: poll.signal }) }
      finally { window.clearTimeout(timeout); signal?.removeEventListener('abort', abortPoll) }
      if (['FAILED', 'CANCELLED', 'INTERRUPTED'].includes(run.status)) throw new ApiError(run.error_message || 'La generación falló; el archivo está incompleto.', 422, undefined, run.error_code || 'REPORT_STREAM_UNVERIFIED')
      if (run.status === 'SUCCESS') {
        if (run.generation_status !== 'COMPLETE' || run.transmission_status !== 'COMPLETE' || run.metrics?.serialization_complete !== true || run.metrics?.serialized_bytes !== bytes) throw new ApiError('El cierre de la ejecución no confirma los bytes completos recibidos.', 422, undefined, 'REPORT_STREAM_UNVERIFIED')
        break
      }
      if (Date.now() >= deadline) throw new ApiError('No se confirmó el estado final a tiempo. El archivo se considera incompleto; consulta el historial.', 422, undefined, 'REPORT_SETTLEMENT_TIMEOUT')
      await new Promise(resolve => window.setTimeout(resolve, 250))
    }
    if (signal?.aborted) throw cancelled()
    if (writable) await interruptible(writable.close(), signal) // Publish local write only after authoritative success.
    else {
      const url = URL.createObjectURL(new Blob(chunks, { type: response.headers.get('Content-Type') || 'application/octet-stream' }))
      const link = document.createElement('a'); link.href = url; link.download = fileName
      document.body.appendChild(link); link.click(); link.remove()
      window.setTimeout(() => URL.revokeObjectURL(url), 1000)
    }
    phase('SUCCESS')
    return { executionId: identity, bytes, method: writable ? 'DIRECT_FILE' : 'BOUNDED_MEMORY' }
  } catch (error) {
    phase('INTERRUPTED')
    await cleanup()
    if (signal?.aborted) throw cancelled()
    if (error instanceof ApiError) throw error
    if (identity && !signal?.aborted) {
      const original = await generationFailure(identity)
      if (original) throw original
    }
    throw new ApiError(signal?.aborted ? 'Transferencia cancelada; archivo incompleto.' : 'La transferencia o escritura se interrumpió; archivo incompleto.', 0, undefined, signal?.aborted ? 'TRANSFER_CANCELLED' : 'REPORT_TRANSFER_INTERRUPTED')
  } finally {
    signal?.removeEventListener('abort', abortTransfer)
    try { reader?.releaseLock() } catch { /* Cancellation can leave an underlying read settling. */ }
  }
}
