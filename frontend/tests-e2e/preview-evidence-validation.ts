export interface PreviewReference {
  originalId: string
  page: number
  bytes: number
  sha256: string
  alt: string
}

export interface PreviewResponseEvidence {
  requestId: number
  originalId: string
  page: number
  path: string
  status?: number
  mediaType?: string
  bytes?: number
  sha256?: string
  jpeg?: boolean
  settled: boolean
  signalAborted: boolean
  errorName?: string
}

export interface DisplayedPreviewEvidence {
  selectedPage: number
  alt: string
  decoded: boolean
  blobSource: boolean
  naturalWidth: number
  bytes: number
  sha256: string
  jpeg: boolean
}

export function assertPreviewResponse(evidence: PreviewResponseEvidence, expected: PreviewReference): void {
  if (evidence.originalId !== expected.originalId || evidence.page !== expected.page) throw new Error('Preview response belongs to another original or page')
  if (!evidence.settled || evidence.signalAborted || evidence.errorName) throw new Error('Preview response was incomplete or cancelled')
  if (evidence.status !== 200 || evidence.mediaType !== 'image/jpeg') throw new Error('Preview response status or media type is invalid')
  if (!Number.isSafeInteger(evidence.bytes) || !evidence.bytes || evidence.bytes !== expected.bytes || !evidence.jpeg) throw new Error('Preview response is empty, invalid, or has the wrong byte count')
  if (!/^[a-f0-9]{64}$/.test(expected.sha256) || evidence.sha256 !== expected.sha256) throw new Error('Preview response differs from the independently bound source page')
}

export function assertDisplayedPreview(displayed: DisplayedPreviewEvidence, response: PreviewResponseEvidence, expected: PreviewReference): void {
  assertPreviewResponse(response, expected)
  if (displayed.selectedPage !== expected.page || displayed.alt !== expected.alt) throw new Error('Displayed preview belongs to an obsolete page selection')
  if (!displayed.decoded || !displayed.blobSource || displayed.naturalWidth <= 0 || !displayed.jpeg || displayed.bytes <= 0) throw new Error('Displayed preview did not decode a nonempty JPEG Blob')
  if (displayed.bytes !== response.bytes || displayed.sha256 !== response.sha256) throw new Error('Displayed Blob differs from the exact HTTP response delivered to the page')
}
