import { describe, expect, it } from 'vitest'
import { assertDisplayedPreview, assertPreviewResponse } from '../../../tests-e2e/preview-evidence-validation'
import type { DisplayedPreviewEvidence, PreviewReference, PreviewResponseEvidence } from '../../../tests-e2e/preview-evidence-validation'

const expected: PreviewReference = { originalId: 'synthetic-original', page: 3, bytes: 50_000, sha256: 'a'.repeat(64), alt: 'Página 3 del original PDF' }
const captured = (): PreviewResponseEvidence => ({ requestId: 2, originalId: expected.originalId, page: 3,
  path: '/api/v1/documents/originals/synthetic-original/pages/3', settled: true, signalAborted: false,
  status: 200, mediaType: 'image/jpeg', bytes: 50_000, jpeg: true, sha256: expected.sha256 })
const displayed = (): DisplayedPreviewEvidence => ({ selectedPage: 3, alt: expected.alt, decoded: true,
  blobSource: true, naturalWidth: 1800, bytes: 50_000, sha256: expected.sha256, jpeg: true })

describe('Exact preview capture assertions retain the image integrity objective', () => {
  it('accepts the same nonempty JPEG body and selected decoded Blob', () => {
    expect(() => assertDisplayedPreview(displayed(), captured(), expected)).not.toThrow()
  })
  it.each([
    { bytes: 0 }, { bytes: 49_999 }, { jpeg: false }, { mediaType: 'application/json' }, { status: 429 },
    { sha256: 'b'.repeat(64) }, { originalId: 'another-original' }, { page: 2 },
    { settled: false }, { signalAborted: true }, { errorName: 'AbortError' },
  ])('rejects invalid or unrelated exact-response evidence: %j', change => {
    expect(() => assertPreviewResponse({ ...captured(), ...change }, expected)).toThrow()
  })
  it.each([
    { decoded: false }, { naturalWidth: 0 }, { blobSource: false }, { bytes: 0 }, { jpeg: false },
    { selectedPage: 2 }, { alt: 'Página 2 del original PDF' }, { sha256: 'b'.repeat(64) }, { bytes: 49_999 },
  ])('rejects empty, incorrect, or stale displayed content: %j', change => {
    expect(() => assertDisplayedPreview({ ...displayed(), ...change }, captured(), expected)).toThrow()
  })
})
