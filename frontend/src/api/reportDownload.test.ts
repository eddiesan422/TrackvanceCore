import { beforeEach, expect, it, vi } from 'vitest'
import { receiveReport } from './reportDownload'

const fetchMock = vi.fn()
const jsonResponse = (value: unknown) => new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
const id = '12345678-1234-1234-1234-123456789abc'
function response(chunks: Uint8Array[] = [new TextEncoder().encode('complete')]) {
  return new Response(new ReadableStream({ start(controller) { chunks.forEach(chunk => controller.enqueue(chunk)); controller.close() } }), { headers: { 'X-Report-Execution-Id': id } })
}
const success = { status: 'SUCCESS', generation_status: 'COMPLETE', transmission_status: 'COMPLETE', metrics: { serialization_complete: true, serialized_bytes: 8 } }
beforeEach(() => { vi.clearAllMocks(); vi.stubGlobal('fetch', fetchMock); fetchMock.mockResolvedValue(jsonResponse(success)) })

it('streams to local writable and closes only after authoritative settlement', async () => {
  const order: string[] = []
  const writable = { write: vi.fn(async () => { order.push('write') }), close: vi.fn(async () => { order.push('close') }), abort: vi.fn(async () => {}) }
  fetchMock.mockImplementation(async () => { order.push('verified'); return jsonResponse(success) })
  const input = response(), blob = vi.spyOn(input, 'blob')
  expect(await receiveReport(input, 'report.xlsx', writable)).toMatchObject({ executionId: id, bytes: 8, method: 'DIRECT_FILE' })
  expect(order).toEqual(['write', 'verified', 'close']); expect(blob).not.toHaveBeenCalled(); expect(writable.abort).not.toHaveBeenCalled()
})

it('retains generation failure and aborts the incomplete local write', async () => {
  fetchMock.mockResolvedValue(jsonResponse({ status: 'FAILED', error_code: 'REPORT_RESULT_LIMIT', error_message: 'Original generator diagnostic' }))
  const writable = { write: vi.fn(async () => {}), close: vi.fn(async () => {}), abort: vi.fn(async () => {}) }
  await expect(receiveReport(response(), 'report.xlsx', writable)).rejects.toMatchObject({ code: 'REPORT_RESULT_LIMIT', message: 'Original generator diagnostic' })
  expect(writable.abort).toHaveBeenCalledOnce(); expect(writable.close).not.toHaveBeenCalled()
})

it('waits for terminal publication rather than accepting RUNNING after stream EOF', async () => {
  fetchMock.mockResolvedValueOnce(jsonResponse({ status: 'RUNNING' })).mockResolvedValueOnce(jsonResponse(success))
  const writable = { write: vi.fn(async () => {}), close: vi.fn(async () => {}), abort: vi.fn(async () => {}) }
  await receiveReport(response(), 'report.xlsx', writable)
  expect(fetchMock).toHaveBeenCalledTimes(2); expect(writable.close).toHaveBeenCalledOnce()
})

it('rejects byte mismatch even when HTTP and execution say success', async () => {
  fetchMock.mockResolvedValue(jsonResponse({ ...success, metrics: { serialization_complete: true, serialized_bytes: 7 } }))
  await expect(receiveReport(response(), 'report.xlsx', undefined)).rejects.toMatchObject({ code: 'REPORT_STREAM_UNVERIFIED' })
})

it('bounds the fallback before creating a blob or offering a failed file', async () => {
  const input = response([new Uint8Array(32 * 1024 * 1024), new Uint8Array(1)])
  const blob = vi.spyOn(input, 'blob'), create = vi.spyOn(document, 'createElement')
  await expect(receiveReport(input, 'report.xlsx', undefined)).rejects.toMatchObject({ code: 'REPORT_BROWSER_MEMORY_LIMIT' })
  expect(blob).not.toHaveBeenCalled(); expect(create).not.toHaveBeenCalled(); expect(fetchMock).not.toHaveBeenCalled()
})

it('cancellation never closes a partial file', async () => {
  const controller = new AbortController(); controller.abort()
  const writable = { write: vi.fn(async () => {}), close: vi.fn(async () => {}), abort: vi.fn(async () => {}) }
  await expect(receiveReport(response(), 'report.xlsx', writable, controller.signal)).rejects.toMatchObject({ code: 'TRANSFER_CANCELLED' })
  expect(writable.close).not.toHaveBeenCalled(); expect(writable.abort).toHaveBeenCalledOnce()
})

it('cancels pending disk writes without waiting for them to finish or closing the file', async () => {
  const controller = new AbortController()
  let began!: () => void
  const started = new Promise<void>(resolve => { began = resolve })
  const writable = { write: vi.fn(() => { began(); return new Promise<void>(() => {}) }), close: vi.fn(async () => {}), abort: vi.fn(async () => {}) }
  const transfer = receiveReport(response(), 'report.xlsx', writable, controller.signal)
  const rejected = expect(transfer).rejects.toMatchObject({ code: 'TRANSFER_CANCELLED' })
  await started; controller.abort(); await rejected
  expect(writable.abort).toHaveBeenCalledOnce(); expect(writable.close).not.toHaveBeenCalled()
})

it('correlates a broken stream with the original generator failure in history', async () => {
  fetchMock.mockResolvedValue(jsonResponse({ status: 'FAILED', error_code: 'REPORT_XLSX_CELL_LIMIT', error_message: 'Original cell diagnostic' }))
  const input = new Response(new ReadableStream({ start(controller) { controller.error(new TypeError('Connection terminated')) } }), { headers: { 'X-Report-Execution-Id': id } })
  const writable = { write: vi.fn(async () => {}), close: vi.fn(async () => {}), abort: vi.fn(async () => {}) }
  await expect(receiveReport(input, 'report.xlsx', writable)).rejects.toMatchObject({ code: 'REPORT_XLSX_CELL_LIMIT', message: 'Original cell diagnostic' })
  expect(fetchMock).toHaveBeenCalledOnce(); expect(writable.abort).toHaveBeenCalledOnce(); expect(writable.close).not.toHaveBeenCalled()
})
