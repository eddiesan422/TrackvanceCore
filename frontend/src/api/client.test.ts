import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, download, setCsrfToken } from './client'

afterEach(() => { vi.useRealTimers() })

describe('Local API requests and downloads', () => {
  it('sends same-origin credentials and the session CSRF token for writes', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response('{"id":"new-run"}', { headers: { 'Content-Type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)
    setCsrfToken('csrf-test')
    expect(await api('/intake/runs', { method: 'POST', body: '{}' })).toEqual({ id: 'new-run' })
    const [path, options] = fetchMock.mock.calls[0]
    expect(path).toBe('/api/v1/intake/runs')
    expect(options.credentials).toBe('same-origin')
    expect(options.headers.get('X-CSRF-Token')).toBe('csrf-test')
  })

  it('uses a sanitized server filename for the Excel download', async () => {
    vi.useFakeTimers()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(new Blob(['xlsx']), { headers: {
      'Content-Type': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
      'Content-Disposition': "attachment; filename*=UTF-8''..%2Ftrackvance_intake_run.xlsx",
    } })))
    Object.defineProperty(URL, 'createObjectURL', { configurable: true, writable: true, value: vi.fn().mockReturnValue('blob:report') })
    Object.defineProperty(URL, 'revokeObjectURL', { configurable: true, writable: true, value: vi.fn() })
    let filename = ''
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { filename = this.download })
    await download('/runs/run/export.xlsx', 'fallback.xlsx')
    expect(filename).toBe('_trackvance_intake_run.xlsx')
    expect(filename).not.toContain('/')
    expect(document.querySelector('a')).toBeNull()
    await vi.runAllTimersAsync()
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:report')
  })

  it('keeps authorization failures and request references available to the UI', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(JSON.stringify({ error: { message: 'No tienes permiso para exportar.', request_id: 'req-denied' } }), { status: 403 })))
    await expect(download('/runs/run/export.xlsx', 'report.xlsx')).rejects.toMatchObject({ status: 403, requestId: 'req-denied', message: 'No tienes permiso para exportar.' })
  })

  it('turns network failures into readable local-service errors', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Network error')))
    await expect(download('/runs/run/export.xlsx', 'report.xlsx')).rejects.toBeInstanceOf(ApiError)
    await expect(api('/datasets')).rejects.toMatchObject({ status: 0, message: expect.stringContaining('servicio local') })
  })
})
