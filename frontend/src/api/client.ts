// eslint-disable-next-line @typescript-eslint/no-explicit-any -- Evidence records retain heterogeneous API fields until a generated DTO client is adopted.
export type RecordData = Record<string, any> // API-defined, heterogeneous evidence and configuration records.
export interface Collection { items: RecordData[]; total: number }
export interface Session { user: { id: string; name: string; username?: string; email: string; role: string; role_id?: string; role_version?: number; must_change_password?: boolean; permissions: string[] }; organization: { id: string; name: string }; csrf_token: string }

let csrfToken = ''
export function setCsrfToken(token: string) { csrfToken = token }
export class ApiError extends Error {
  status: number
  requestId?: string
  code?: string
  details?: Record<string, unknown> | null
  constructor(message: string, status: number, requestId?: string, code?: string, details?: Record<string, unknown> | null) { super(message); this.status = status; this.requestId = requestId; this.code = code; this.details = details }
}
export async function api<T = RecordData>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData) && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrfToken)
  let response: Response
  try { response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: 'same-origin' }) }
  catch { throw new ApiError('No pudimos conectar con el servicio local. Comprueba que el servidor esté iniciado.', 0) }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const detail = body.error || body.detail || {}
    if ((response.status === 401 || response.status === 403) && path !== '/me' && !path.startsWith('/auth/')) window.dispatchEvent(new Event('trackvance:session-refresh'))
    throw new ApiError(typeof detail === 'string' ? detail : detail.message || 'No fue posible completar la operación.', response.status, detail.request_id, detail.code, detail.details)
  }
  return response.status === 204 ? undefined as T : response.json()
}
export function post<T = RecordData>(path: string, data: unknown = {}) { return api<T>(path, { method: 'POST', body: JSON.stringify(data) }) }
export function uploadBinary<T>(path: string, file: File, progress: (bytes: number) => void, signal?: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    request.open('POST', `/api/v1${path}`)
    request.withCredentials = true
    request.setRequestHeader('Content-Type', 'application/octet-stream')
    request.setRequestHeader('X-CSRF-Token', csrfToken)
    request.upload.onprogress = event => progress(event.loaded)
    const abort = () => request.abort()
    const finish = () => signal?.removeEventListener('abort', abort)
    request.onload = () => {
      finish()
      let body: { error?: { message?: string; request_id?: string; code?: string; details?: Record<string, unknown> | null }; detail?: string | { message?: string } } & T
      try { body = JSON.parse(request.responseText) } catch { reject(new ApiError('El servidor devolvió una respuesta incompleta.', request.status)); return }
      if (request.status >= 200 && request.status < 300) { progress(file.size); resolve(body); return }
      if (request.status === 401 || request.status === 403) window.dispatchEvent(new Event('trackvance:session-refresh'))
      reject(new ApiError(body.error?.message || (typeof body.detail === 'string' ? body.detail : body.detail?.message) || 'No se pudo recibir el archivo.', request.status, body.error?.request_id, body.error?.code, body.error?.details))
    }
    request.onerror = () => { finish(); reject(new ApiError('No pudimos conectar para recibir el archivo.', 0)) }
    request.onabort = () => { finish(); reject(new ApiError('Transferencia cancelada antes de registrar la adquisición.', 0)) }
    signal?.addEventListener('abort', abort, { once: true })
    if (signal?.aborted) { reject(new ApiError('Transferencia cancelada.', 0)); return }
    request.send(file)
  })
}
export async function download(path: string, fileName: string, options: RequestInit = {}) {
  let response: Response
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrfToken)
  try { response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: 'same-origin' }) }
  catch { throw new ApiError(options.signal?.aborted ? 'Transferencia cancelada.' : 'No pudimos conectar con el servicio local para descargar el archivo.', 0, undefined, options.signal?.aborted ? 'TRANSFER_CANCELLED' : undefined) }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const detail = body.error || body.detail || {}
    if (response.status === 401 || response.status === 403) window.dispatchEvent(new Event('trackvance:session-refresh'))
    throw new ApiError(typeof detail === 'string' ? detail : detail.message || 'No se pudo descargar el archivo. Inténtalo de nuevo.', response.status, detail.request_id, detail.code, detail.details)
  }
  const disposition = response.headers.get('Content-Disposition') || ''
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1]
  const simple = /filename="([^"]+)"|filename=([^;]+)/i.exec(disposition)
  let suggested = simple?.[1] || simple?.[2] || fileName
  if (encoded) { try { suggested = decodeURIComponent(encoded) } catch { suggested = fileName } }
  // eslint-disable-next-line no-control-regex -- Download filenames must exclude literal control characters.
  const safeName = suggested.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').replace(/^\.+/, '').trim().slice(0, 180) || fileName
  let content: Blob
  try { content = await response.blob() }
  catch { throw new ApiError(options.signal?.aborted ? 'Transferencia cancelada.' : 'La transferencia se interrumpió antes de recibir el archivo completo.', 0, undefined, options.signal?.aborted ? 'TRANSFER_CANCELLED' : undefined) }
  if (options.signal?.aborted) throw new ApiError('Transferencia cancelada.', 0, undefined, 'TRANSFER_CANCELLED')
  const url = URL.createObjectURL(content)
  const link = document.createElement('a'); link.href = url; link.download = safeName
  document.body.appendChild(link); link.click(); link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
