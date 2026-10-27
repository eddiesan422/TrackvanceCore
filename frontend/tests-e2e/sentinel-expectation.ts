import type { RecordData as Json } from '../src/api/client'

// A fixture describes an expected control outcome; it never changes the monitor.
export interface SentinelVolumeAlertExpectation {
  kind: 'VOLUME_CHANGE_ALERT'
  baseline_version_id: string
  baseline_rows: number
  current_rows: number
  max_pct: number
}

export interface SentinelExpectationContext {
  dataset_id: string
  source_version_id: string
  source_profile: Json
  monitor: Json
  key_columns: string[]
  baseline_profile?: Json
  dataset_versions?: Json[]
}

function requireThat(condition: unknown, message: string): asserts condition {
  if (!condition) throw new Error('Sentinel fixture: ' + message)
}
function sameKeys(actual: string[], expected: string[]) {
  return JSON.stringify([...actual].sort()) === JSON.stringify([...expected].sort())
}

export function verifySentinelExpectation(run: Json, context: SentinelExpectationContext, expected?: SentinelVolumeAlertExpectation) {
  const { source_profile: source, monitor } = context
  requireThat(run.status === 'SUCCESS' && run.module === 'sentinel', 'the native Sentinel run must execute successfully')
  requireThat(run.config_id === monitor.id && monitor.module === 'sentinel' && monitor.dataset_id === context.dataset_id,
    'the run must use this native monitor and dataset')
  requireThat(run.dataset_version_id === context.source_version_id && source.id === context.source_version_id
    && source.dataset_id === context.dataset_id && source.profile_status === 'READY', 'the source dataset/version does not match')
  const metrics = run.metrics
  requireThat(metrics && metrics.profile_dataset_version_id === source.id && metrics.row_count === source.row_count
    && metrics.profile_snapshot?.row_count === source.row_count, 'the control must measure the complete source version')
  requireThat(Array.isArray(metrics.checks) && metrics.checks.length > 0, 'actual control checks are required')
  const checks: Json[] = metrics.checks
  requireThat(checks.every(check => typeof check.code === 'string' && ['PASS', 'FAIL'].includes(check.status)), 'unexpected control status')
  requireThat(new Set(checks.map(check => check.code)).size === checks.length, 'duplicate control codes cannot establish the expected result')
  const failed = checks.filter(check => check.status === 'FAIL')
  requireThat(metrics.total_checks === checks.length && metrics.failed_checks === failed.length
    && metrics.conforming_checks === checks.length - failed.length, 'control counters must match the actual checks')
  if (expected === undefined) {
    requireThat(run.decision === 'HEALTHY' && failed.length === 0 && Array.isArray(run.findings) && run.findings.length === 0,
      'HEALTHY remains required unless a precise alert fixture is declared')
    return { run_status: run.status, decision: run.decision, expected_decision: 'HEALTHY', checks: checks.length }
  }
  requireThat(expected.kind === 'VOLUME_CHANGE_ALERT', 'only an explicitly declared volume-change alert is supported')
  requireThat(Number.isSafeInteger(expected.baseline_rows) && expected.baseline_rows >= 0
    && Number.isSafeInteger(expected.current_rows) && expected.current_rows >= 0
    && Number.isFinite(expected.max_pct) && expected.max_pct >= 0, 'row counts and threshold must be explicit valid fixture values')
  const baseline = context.baseline_profile
  requireThat(baseline && baseline.id === expected.baseline_version_id && baseline.id !== source.id
    && baseline.dataset_id === context.dataset_id && baseline.profile_status === 'READY'
    && baseline.row_count === expected.baseline_rows && source.row_count === expected.current_rows,
  'the declared baseline/current counts must come from the corresponding immutable profiles')
  requireThat(Number.isSafeInteger(baseline.version) && Number.isSafeInteger(source.version) && baseline.version < source.version,
    'the baseline must precede this version')
  requireThat(Array.isArray(context.dataset_versions), 'the dataset version inventory is required for the baseline selection')
  const preceding = context.dataset_versions.filter(version => version.dataset_id === context.dataset_id
    && version.profile_status === 'READY' && Number.isSafeInteger(version.version) && version.version < source.version)
    .sort((left, right) => right.version - left.version)
  requireThat(preceding[0]?.id === baseline.id, 'the baseline must be the actual previous READY version, not an arbitrary historical row count')
  requireThat(monitor.config?.max_volume_change_pct === expected.max_pct, 'the actual monitor threshold differs from the frozen fixture')
  const rawChange = expected.baseline_rows ? Math.abs(expected.current_rows - expected.baseline_rows) / expected.baseline_rows * 100
    : expected.current_rows ? 100 : 0
  const change = Number(rawChange.toFixed(2))
  requireThat(rawChange > expected.max_pct, 'this fixture does not justify a volume alert')
  const passCodes = ['SCHEMA_REQUIRED', ...context.key_columns.map(column => 'NULL_RATE_' + column.toUpperCase()), 'FRESHNESS']
  requireThat(sameKeys(checks.map(check => check.code), [...passCodes, 'VOLUME_CHANGE'])
    && passCodes.every(code => checks.find(check => check.code === code)?.status === 'PASS'), 'every other declared control must pass without being removed')
  requireThat(run.decision === 'ALERT' && failed.length === 1 && failed[0].code === 'VOLUME_CHANGE', 'an unrelated or additional ALERT is not an expected result')
  const check = failed[0]
  const matchesVolume = (value: Json) => !!value && value.code === 'VOLUME_CHANGE' && value.status === 'FAIL'
    && value.classification === 'FAIL' && value.actual === change
    && value.expected && sameKeys(Object.keys(value.expected), ['baseline_rows', 'max_pct', 'method'])
    && value.expected.baseline_rows === expected.baseline_rows && value.expected.max_pct === expected.max_pct
    && value.expected.method === 'previous_version'
  requireThat(matchesVolume(check), 'the failed check must report the calculated volume, baseline, rule and unchanged threshold')
  requireThat(Array.isArray(run.findings) && run.findings.length === 1 && run.findings[0].code === 'VOLUME_CHANGE'
    && run.findings[0].run_id === run.id && matchesVolume(run.findings[0].details), 'the native finding must explain this exact expected volume alert')
  return { run_status: run.status, decision: run.decision, expected_decision: 'ALERT', expected_control: check.code,
    source_version_id: source.id, baseline_version_id: baseline.id, current_rows: expected.current_rows,
    baseline_rows: expected.baseline_rows, volume_change_pct: change, max_pct: expected.max_pct, other_controls_passed: passCodes.length }
}
