import { describe, expect, it } from 'vitest'
import type { RecordData as Json } from '../src/api/client'
import { verifySentinelExpectation, type SentinelExpectationContext, type SentinelVolumeAlertExpectation } from '../tests-e2e/sentinel-expectation'

function fixture(previous = 2, current = 10) {
  const source = { id: 'version-current', dataset_id: 'dataset-one', profile_status: 'READY', version: 3, row_count: current }
  const baseline = { id: 'version-before', dataset_id: 'dataset-one', profile_status: 'READY', version: 2, row_count: previous }
  const context: SentinelExpectationContext = { dataset_id: source.dataset_id, source_version_id: source.id, source_profile: source,
    monitor: { id: 'monitor-one', module: 'sentinel', dataset_id: source.dataset_id, config: { max_volume_change_pct: 15 } },
    key_columns: ['record_id'], baseline_profile: baseline, dataset_versions: [source, baseline] }
  const expected: SentinelVolumeAlertExpectation = { kind: 'VOLUME_CHANGE_ALERT', baseline_version_id: baseline.id,
    baseline_rows: previous, current_rows: current, max_pct: 15 }
  const change = previous ? Number((Math.abs(current - previous) / previous * 100).toFixed(2)) : current ? 100 : 0
  const volume = { code: 'VOLUME_CHANGE', status: 'FAIL', classification: 'FAIL', actual: change,
    expected: { baseline_rows: previous, max_pct: 15, method: 'previous_version' } }
  const run: Json = { id: 'run-one', module: 'sentinel', config_id: context.monitor.id, dataset_version_id: source.id,
    status: 'SUCCESS', decision: 'ALERT', metrics: { profile_dataset_version_id: source.id, row_count: current,
      profile_snapshot: { row_count: current }, checks: [
        { code: 'SCHEMA_REQUIRED', status: 'PASS' }, { code: 'NULL_RATE_RECORD_ID', status: 'PASS' }, { code: 'FRESHNESS', status: 'PASS' }, volume,
      ], total_checks: 4, failed_checks: 1, conforming_checks: 3 },
    findings: [{ run_id: 'run-one', code: 'VOLUME_CHANGE', details: structuredClone(volume) }] }
  return { context, expected, run }
}

describe('Native Sentinel execution and fixture-specific control outcomes', () => {
  it('derives a declared alert from immutable counts rather than a corpus-specific percentage', () => {
    const { context, expected, run } = fixture()
    expect(verifySentinelExpectation(run, context, expected)).toMatchObject({ run_status: 'SUCCESS', decision: 'ALERT',
      volume_change_pct: 400, baseline_rows: 2, current_rows: 10, max_pct: 15, other_controls_passed: 3 })
    const second = fixture(1, 8)
    expect(verifySentinelExpectation(second.run, second.context, second.expected).volume_change_pct).toBe(700)
  })

  it('retains HEALTHY as the default and never accepts an unspecified ALERT', () => {
    const { context, run } = fixture()
    expect(() => verifySentinelExpectation(run, context)).toThrow('HEALTHY remains required')
    run.decision = 'HEALTHY'; run.findings = []
    for (const check of run.metrics.checks) check.status = 'PASS'
    run.metrics.failed_checks = 0; run.metrics.conforming_checks = 4
    expect(verifySentinelExpectation(run, context).decision).toBe('HEALTHY')
  })

  it.each(['status', 'dataset_version_id', 'config_id'])('rejects an incorrect native run %s', key => {
    const { context, expected, run } = fixture()
    run[key] = 'different'
    expect(() => verifySentinelExpectation(run, context, expected)).toThrow()
  })

  it('rejects another dataset or an earlier but non-previous baseline', () => {
    const { context, expected, run } = fixture()
    context.baseline_profile!.dataset_id = 'different'
    expect(() => verifySentinelExpectation(run, context, expected)).toThrow('immutable profiles')
    context.baseline_profile!.dataset_id = context.dataset_id
    context.baseline_profile!.version = 1
    context.dataset_versions!.push({ id: 'intervening-ready', dataset_id: context.dataset_id, version: 2, profile_status: 'READY' })
    expect(() => verifySentinelExpectation(run, context, expected)).toThrow('actual previous READY version')
  })

  it('rejects altered row counts, altered threshold or an alert not justified by the counts', () => {
    const { context, expected, run } = fixture()
    expected.current_rows = 9
    expect(() => verifySentinelExpectation(run, context, expected)).toThrow('immutable profiles')
    expected.current_rows = 10; context.monitor.config.max_volume_change_pct = 500
    expect(() => verifySentinelExpectation(run, context, expected)).toThrow('actual monitor threshold')
    context.monitor.config.max_volume_change_pct = 15
    const same = fixture(10, 10)
    expect(() => verifySentinelExpectation(same.run, same.context, same.expected)).toThrow('does not justify')
  })

  it('rejects a wrong reported percentage or finding even if the run says ALERT', () => {
    const { context, expected, run } = fixture()
    run.metrics.checks[3].actual = 999
    expect(() => verifySentinelExpectation(run, context, expected)).toThrow('calculated volume')
    run.metrics.checks[3].actual = 400; run.findings[0].details.expected.baseline_rows = 1
    expect(() => verifySentinelExpectation(run, context, expected)).toThrow('native finding')
  })

  it('rejects extra failed checks or omitted controls instead of accepting any ALERT', () => {
    const { context, expected, run } = fixture()
    run.metrics.checks[1].status = 'FAIL'; run.metrics.failed_checks = 2; run.metrics.conforming_checks = 2
    expect(() => verifySentinelExpectation(run, context, expected)).toThrow('every other declared control')
    run.metrics.checks.splice(1, 1); run.metrics.total_checks = 3; run.metrics.failed_checks = 1
    expect(() => verifySentinelExpectation(run, context, expected)).toThrow('every other declared control')
  })
})
