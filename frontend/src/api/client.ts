// eslint-disable-next-line @typescript-eslint/no-explicit-any -- Evidence records retain heterogeneous API fields until a generated DTO client is adopted.
export type RecordData = Record<string, any> // API-defined, heterogeneous evidence and configuration records.
export interface Collection { items: RecordData[]; total: number }
export interface Session { user: { id: string; name: string; email: string; role: string; permissions: string[] }; organization: { id: string; name: string }; csrf_token: string }

let csrfToken = ''
export function setCsrfToken(token: string) { csrfToken = token }
export class ApiError extends Error {
  status: number
  requestId?: string
  constructor(message: string, status: number, requestId?: string) { super(message); this.status = status; this.requestId = requestId }
}
export async function api<T = RecordData>(path: string, options: RequestInit = {}): Promise<T> {
  const headers = new Headers(options.headers)
  if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json')
  if (options.method && options.method !== 'GET') headers.set('X-CSRF-Token', csrfToken)
  let response: Response
  try { response = await fetch(`/api/v1${path}`, { ...options, headers, credentials: 'same-origin' }) }
  catch { throw new ApiError('No pudimos conectar con el servicio local. Comprueba que el servidor esté iniciado.', 0) }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const detail = body.error || body.detail || {}
    throw new ApiError(typeof detail === 'string' ? detail : detail.message || 'No fue posible completar la operación.', response.status, detail.request_id)
  }
  return response.status === 204 ? undefined as T : response.json()
}
export function post<T = RecordData>(path: string, data: unknown = {}) { return api<T>(path, { method: 'POST', body: JSON.stringify(data) }) }
export async function download(path: string, fileName: string) {
  let response: Response
  try { response = await fetch(`/api/v1${path}`, { credentials: 'same-origin' }) }
  catch { throw new ApiError('No pudimos conectar con el servicio local para descargar el archivo.', 0) }
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new ApiError(body.error?.message || 'No se pudo descargar la evidencia. Inténtalo de nuevo.', response.status, body.error?.request_id)
  }
  const disposition = response.headers.get('Content-Disposition') || ''
  const encoded = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1]
  const simple = /filename="([^"]+)"|filename=([^;]+)/i.exec(disposition)
  let suggested = simple?.[1] || simple?.[2] || fileName
  if (encoded) { try { suggested = decodeURIComponent(encoded) } catch { suggested = fileName } }
  // eslint-disable-next-line no-control-regex -- Download filenames must exclude literal control characters.
  const safeName = suggested.replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').replace(/^\.+/, '').trim().slice(0, 180) || fileName
  const url = URL.createObjectURL(await response.blob())
  const link = document.createElement('a'); link.href = url; link.download = safeName
  document.body.appendChild(link); link.click(); link.remove()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
