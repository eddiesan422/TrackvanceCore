import { describe, expect, it } from 'vitest'
import { deliveryEvidenceState } from './runTerminal'

const committed = { status: 'SUCCESS', decision: 'COMMITTED' }

describe('Delivery evidence settlement used by the real browser wait', () => {
  it('keeps waiting across the durable commit until the separate receipt is published', () => {
    const reads = [
      { status: 'RUNNING', decision: null, metrics: {} },
      { ...committed, metrics: {} },
      { ...committed, metrics: {} },
      { ...committed, metrics: { receipt_artifact_id: 'published-receipt' } },
    ].map(run => Object.freeze(run))
    expect(reads.map(deliveryEvidenceState)).toEqual(['WAITING', 'WAITING', 'WAITING', 'PUBLISHED'])
    expect(reads[1].metrics).toEqual({})
  })

  it.each([undefined, null, {}, { receipt_artifact_id: null }, { receipt_artifact_id: '' }])(
    'never reports publication before a receipt ID is persisted (%j)', metrics => {
      expect(deliveryEvidenceState({ ...committed, metrics })).toBe('WAITING')
    },
  )

  it.each([undefined, 'old-receipt'])(
    'rejects explicit repair even when a stale receipt ID is present (%j)', receipt_artifact_id => {
      expect(deliveryEvidenceState({ ...committed, metrics: { evidence_status: 'PENDING_REPAIR', receipt_artifact_id } })).toBe('REJECTED')
    },
  )

  it.each(['FAILED', 'FAILED_PRECONDITION', 'CANCELLED', 'UNKNOWN', 'INTERRUPTED', 'UNRECOGNIZED', undefined])(
    'settles a negative or unknown status for an explicit failure, never publication (%j)', status => {
      expect(deliveryEvidenceState({ status, decision: 'COMMITTED', metrics: { receipt_artifact_id: 'old-receipt' } })).toBe('REJECTED')
    },
  )

  it.each(['UNKNOWN', 'APPROVED', 'FAILED', null, undefined])(
    'rejects an invalid terminal decision even with a receipt (%j)', decision => {
      expect(deliveryEvidenceState({ status: 'SUCCESS', decision, metrics: { receipt_artifact_id: 'old-receipt' } })).toBe('REJECTED')
    },
  )

  it.each(['QUEUED', 'RUNNING'])('does not publish a stale receipt while the Run is %s', status => {
    expect(deliveryEvidenceState({ status, decision: 'COMMITTED', metrics: { receipt_artifact_id: 'old-receipt' } })).toBe('WAITING')
  })

  it.each([false, 1, ' ', [], { evidence_status: 'UNKNOWN', receipt_artifact_id: 'old-receipt' }])(
    'fails closed for malformed receipt or unknown evidence projections (%j)', value => {
      const metrics = typeof value === 'object' ? value : { receipt_artifact_id: value }
      expect(deliveryEvidenceState({ ...committed, metrics })).toBe('REJECTED')
    },
  )

  it('accepts a valid published projection without changing its state or requesting repair', () => {
    const metrics = Object.freeze({ evidence_status: 'VALID', receipt_artifact_id: 'published-receipt' })
    const run = Object.freeze({ ...committed, metrics })
    expect(deliveryEvidenceState(run)).toBe('PUBLISHED')
    expect(run.metrics).toBe(metrics)
  })
})
