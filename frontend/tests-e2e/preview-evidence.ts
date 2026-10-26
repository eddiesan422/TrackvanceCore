import type { Page } from '@playwright/test'
import { assertDisplayedPreview, assertPreviewResponse } from './preview-evidence-validation'
import type { DisplayedPreviewEvidence, PreviewReference, PreviewResponseEvidence } from './preview-evidence-validation'

interface Capture extends PreviewResponseEvidence {
  startedAt: number
  completedAt?: number
  abortAt?: number
}

// Chromium/Playwright1.63 can evict fetch bodies after in-page blob() consumption:
// https://github.com/microsoft/playwright/issues/42862
// Clone the exact response before consumption. This makes no second HTTP request,
// changes no status/body given to the app, and keeps the original AbortSignal.
export async function installPreviewEvidence(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const shared = window as typeof window & { __trackvancePreviewEvidence?: { version: number; nextId: number; responses: Capture[] } }
    if (shared.__trackvancePreviewEvidence) return
    const state = { version: 1, nextId: 1, responses: [] as Capture[] }
    shared.__trackvancePreviewEvidence = state
    const originalFetch = window.fetch.bind(window)
    window.fetch = async (input, init) => {
      const value = input instanceof Request ? input.url : String(input)
      const path = new URL(value, location.href).pathname
      const match = path.match(/^\/api\/v1\/documents\/originals\/([^/]+)\/pages\/(\d+)$/)
      if (!match) return originalFetch(input, init)
      if (state.responses.length >= 128) throw new Error('Bounded preview evidence capacity exceeded')
      const signal = init?.signal ?? (input instanceof Request ? input.signal : undefined)
      const record: Capture = { requestId: state.nextId++, originalId: match[1], page: Number(match[2]), path,
        settled: false, signalAborted: false, startedAt: performance.now() }
      Object.defineProperty(record, 'signalAborted', { get: () => Boolean(signal?.aborted), enumerable: true })
      signal?.addEventListener('abort', () => { record.abortAt = performance.now() }, { once: true })
      state.responses.push(record)
      try {
        const response = await originalFetch(input, init)
        record.status = response.status
        record.mediaType = response.headers.get('content-type')?.split(';')[0]
        const clone = response.clone()
        void clone.arrayBuffer().then(async data => {
          record.bytes = data.byteLength
          record.jpeg = new Uint8Array(data)[0] === 255 && new Uint8Array(data)[1] === 216
          record.sha256 = [...new Uint8Array(await crypto.subtle.digest('SHA-256', data))].map(value => value.toString(16).padStart(2, '0')).join('')
        }).catch(error => { record.errorName = error instanceof Error ? error.name : 'UnknownCaptureError' }).finally(() => {
          record.completedAt = performance.now(); record.settled = true
        })
        return response
      } catch (error) {
        record.errorName = error instanceof Error ? error.name : 'UnknownTransportError'
        record.completedAt = performance.now(); record.settled = true
        throw error
      }
    }
  })
}

export async function previewResponses(page: Page): Promise<Capture[]> {
  return page.evaluate(() => (window as typeof window & { __trackvancePreviewEvidence?: { responses: Capture[] } }).__trackvancePreviewEvidence?.responses ?? [])
}

export async function nextPreviewRequestId(page: Page): Promise<number> {
  return page.evaluate(() => (window as typeof window & { __trackvancePreviewEvidence?: { nextId: number } }).__trackvancePreviewEvidence?.nextId ?? 1)
}

export async function exactPreviewResponse(page: Page, expected: PreviewReference, minimumRequestId: number): Promise<Capture> {
  await page.waitForFunction(({ originalId, pageNumber, minimum }) => {
    const state = (window as typeof window & { __trackvancePreviewEvidence?: { responses: Capture[] } }).__trackvancePreviewEvidence
    return state?.responses.some(item => item.requestId >= minimum && item.originalId === originalId && item.page === pageNumber && item.status === 200 && item.settled)
  }, { originalId: expected.originalId, pageNumber: expected.page, minimum: minimumRequestId }, { timeout: 30_000 })
  const captures = (await previewResponses(page)).filter(item => item.requestId >= minimumRequestId && item.originalId === expected.originalId && item.page === expected.page && item.status === 200 && item.settled)
  const capture = captures.at(-1)!
  assertPreviewResponse(capture, expected)
  return capture
}

export async function displayedPreview(page: Page): Promise<DisplayedPreviewEvidence> {
  const selectedPage = Number(await page.getByRole('dialog').getByLabel('Página del original', { exact: true }).inputValue())
  return page.getByRole('dialog').locator('.document-source img').evaluate(async (node, selectedPage) => {
    const image = node as HTMLImageElement
    const blobSource = image.currentSrc.startsWith('blob:')
    const bytes = blobSource ? await (await fetch(image.currentSrc)).arrayBuffer() : new ArrayBuffer(0)
    const data = new Uint8Array(bytes)
    return { selectedPage, alt: image.alt, decoded: image.complete && image.naturalWidth > 0,
      blobSource, naturalWidth: image.naturalWidth, bytes: bytes.byteLength, jpeg: data[0] === 255 && data[1] === 216,
      sha256: [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(value => value.toString(16).padStart(2, '0')).join('') }
  }, selectedPage)
}

export async function verifyDisplayedPreview(page: Page, expected: PreviewReference, minimumRequestId: number) {
  const response = await exactPreviewResponse(page, expected, minimumRequestId)
  const displayed = await displayedPreview(page)
  assertDisplayedPreview(displayed, response, expected)
  return { response, displayed, capture_method: 'SAME_RESPONSE_CLONE_BEFORE_APP_CONSUMPTION', second_http_get: false }
}
