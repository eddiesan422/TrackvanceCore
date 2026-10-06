"""Publish bounded failure facts; never publish messages, data or raw logs."""
from __future__ import annotations

import hashlib
import json
import math
import re
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ci.common import ROOT, SHA, load_manifest, require

MAX_BYTES = 2 * 1024**2
ERROR_TYPES = {'AssertionError', 'RuntimeError', 'ValueError', 'TimeoutError', 'TimeoutExpired',
               'FileNotFoundError', 'ConnectionError', 'HTTPError', 'URLError', 'IncompleteRead',
               'EvidenceError', 'PermissionError', 'OSError', 'KeyError', 'TypeError', 'MemoryError'}
ERROR_CODES = {'REPORT_TIMEOUT', 'REPORT_RESULT_LIMIT', 'REPORT_JOIN_LIMIT', 'REPORT_JOIN_EXPANSION',
               'REPORT_MEMORY_LIMIT', 'REPORT_DISK_LIMIT', 'REPORT_ENGINE_FAILED', 'REPORT_ENGINE_ERROR',
               'REPORT_SOURCE_CHANGED', 'REPORT_CONTEXT_EXPIRED', 'REPORT_CANCELLED',
               'ACQUISITION_ROW_LIMIT', 'CHILD_EXIT_NONZERO', 'CHILD_TIMEOUT', 'GROUP_DEADLINE_EXPIRED',
               'EVIDENCE_VALIDATION_FAILED', 'OWNED_CLEANUP_FAILED', 'UNCLASSIFIED_CHILD_FAILURE'}
ERROR_CODES |= {'CERTIFICATION_ASSERTION_FAILED', 'CERTIFICATION_EXCEPTION'}
CHILD_PHASES = {'backend-build', 'web-build', 'startup', 'postgres-snapshot', 'recovery',
                'ephemeral-observation', 'stop-population-services', 'browser',
                *(f'{prefix}-{rows}' for prefix in ('reports', 'copy') for rows in (120, 400000, 1000000))}
STATUSES = {'RUNNING', 'PASS', 'FAIL', 'SUCCESS', 'FAILED', 'CANCELLED'}
TERMINAL_STATES = {'SUCCESS', 'FAILED', 'RUNNING', 'QUEUED', 'CANCELLED', 'INTERRUPTED',
                   'COMPLETE', 'COMPLETED', 'PENDING', 'APPROVED', 'REJECTED'}
CATALOG_PHASES = {'SCOPE_VALIDATION', 'AUTHENTICATION', 'CATALOG_FIXTURE', 'SQL_GUIDED_PARITY',
    'TRIPLE_RESOLVE_PREVIEW', 'TRIPLE_DATASET_WAIT', 'TRIPLE_DATASET_ASSERT', 'TRIPLE_FULL_ORACLE',
    'MANY_TO_MANY_POLICY', 'TRANSITIVE_PARENT_BLOCK', 'COMPLETE',
    *(f'SOURCE_{alias}_{suffix}' for alias in ('A', 'B', 'C') for suffix in ('FIXTURE_WRITE', 'UPLOAD',
      'DATASET_CREATE', 'ACQUISITION_ENQUEUE', 'ACQUISITION_WAIT', 'ACQUISITION_ASSERT', 'GOVERNANCE',
      'CONTRACT_CREATE', 'INTAKE_ENQUEUE', 'INTAKE_WAIT', 'INTAKE_ASSERT')),
    *(f'{join}_{suffix}' for join in ('INNER', 'LEFT', 'RIGHT', 'FULL') for suffix in ('RESOLVE', 'PREVIEW',
      'FULL_POPULATION_ORACLE', 'DATASET_ENQUEUE_IDEMPOTENCE', 'DATASET_WAIT', 'DATASET_ASSERT', 'MATERIALIZED_FULL_ORACLE')),
    *(f'{fmt}_{suffix}' for fmt in ('CSV', 'XLSX') for suffix in ('DOWNLOAD', 'FULL_ORACLE', 'TERMINAL_ASSERT', 'ABOVE_LIMIT_NEGATIVE'))}


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def allowed(value, values):
    return isinstance(value, str) and value in values


def safe_error(value):
    result = {}
    if isinstance(value, dict):
        if allowed(value.get('type'), ERROR_TYPES):
            result['type'] = value['type']
        if allowed(value.get('code'), ERROR_CODES):
            result['code'] = value['code']
    return result


def sanitize_summary(value, depth=0):
    """Only schema-known facts are retained, independent of child messages."""
    if not isinstance(value, dict):
        return {}
    result = {}
    if allowed(value.get('status'), STATUSES):
        result['status'] = value['status']
    if allowed(value.get('failed_stage'), CHILD_PHASES):
        result['failed_stage'] = value['failed_stage']
    if allowed(value.get('active_phase'), CATALOG_PHASES):
        result['active_phase'] = value['active_phase']
    stamp = value.get('phase_started_at')
    if isinstance(stamp, str) and re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?(?:Z|\+00:00)', stamp):
        try:
            result['phase_started_at'] = datetime.fromisoformat(stamp).isoformat()
        except ValueError:
            pass
    terminal = value.get('last_terminal')
    if isinstance(terminal, dict):
        result['last_terminal'] = {key: terminal[key] for key in ('status', 'decision', 'generation_status', 'transmission_status')
                                   if allowed(terminal.get(key), TERMINAL_STATES)}
        if allowed(terminal.get('error_code'), ERROR_CODES):
            result['last_terminal']['error_code'] = terminal['error_code']
    for key in ('failed_tier', 'rows_per_source', 'rows'):
        if type(value.get(key)) is int and value[key] in {120, 400000, 1000000}:
            result[key] = value[key]
    for key in ('main_unchanged', 'timed_out', 'diagnostics_copied'):
        if type(value.get(key)) is bool:
            result[key] = value[key]
    if number(value.get('duration_seconds')):
        result['duration_seconds'] = value['duration_seconds']
    error = safe_error(value.get('error'))
    if error:
        result['error'] = error
    for key in ('error_type', 'failure_type'):
        if allowed(value.get(key), ERROR_TYPES):
            result[key] = value[key]
    for key in ('error_code', 'failure_code'):
        if allowed(value.get(key), ERROR_CODES):
            result[key] = value[key]
    if depth < 2 and isinstance(value.get('tiers'), list):
        result['tiers'] = [item for raw in value['tiers'][:3] if (item := sanitize_summary(raw, depth + 1))]
    if isinstance(value.get('joins'), list):
        result['joins'] = [{'type': item['type'], 'status': item['status']}
                          for item in value['joins'][:4] if isinstance(item, dict)
                          and allowed(item.get('type'), {'INNER', 'LEFT', 'RIGHT', 'FULL'}) and allowed(item.get('status'), STATUSES)]
    # Child diagnostics are filtered again; nested arbitrary objects are dropped.
    if depth < 2 and isinstance(value.get('failure'), dict):
        result['failure'] = sanitize_summary(value['failure'], depth + 1)
    if depth < 2 and isinstance(value.get('failed_tier'), dict):
        result['failed_tier'] = sanitize_summary(value['failed_tier'], depth + 1)
    return result


def trusted_sources():
    completed = subprocess.run(['git', 'ls-files', '-z', '--', 'scripts', 'backend/src', 'backend/tests',
                                'frontend/tests-e2e'], cwd=ROOT, capture_output=True, check=True, timeout=15)
    return {name for name in completed.stdout.decode('utf-8').split('\0') if name.endswith(('.py', '.ts'))}


def extract_log_facts(path: Path, sources: set[str]):
    """Read only a bounded tail. Source lines and exception messages are omitted."""
    if not path.is_file() or path.is_symlink():
        return {}
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - MAX_BYTES))
        text = stream.read(MAX_BYTES).decode('utf-8', errors='replace')
    frames, tests, types, codes, progress = [], set(), set(), set(), []
    for line in text.splitlines():
        match = re.fullmatch(r'\s*File "([^"]+)", line ([0-9]{1,7}), in ([A-Za-z_][A-Za-z0-9_]*|<module>)\s*', line)
        if match:
            raw, line_number, function = match.groups()
            raw = raw.replace('\\', '/')
            matches = [source for source in sources if raw == source or raw.endswith('/' + source)]
            if len(matches) == 1:
                frame = {'file': matches[0], 'line': int(line_number), 'function': function}
                source_file = ROOT / matches[0]
                content = source_file.read_text(encoding='utf-8') if source_file.is_file() else ''
                declared = function == '<module>' or re.search(r'\b(?:def|function)\s+' + re.escape(function) + r'\b', content)
                if declared and frame not in frames:
                    frames.append(frame)
        failure = re.match(r'^FAILED ([^ :]+)::([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\r\n]*\])?(?:\s+-.*)?$', line)
        if failure and failure[1] in sources:
            source_file = ROOT / failure[1]
            if source_file.is_file() and re.search(r'\bdef\s+' + re.escape(failure[2]) + r'\b', source_file.read_text(encoding='utf-8')):
                tests.add(failure[1] + '::' + failure[2])  # parameter values are never retained
        exception = re.match(r'^([A-Za-z][A-Za-z0-9_]*)(?::|$)', line)
        if exception and exception[1] in ERROR_TYPES:
            types.add(exception[1])
        if exception and exception[1] in ERROR_TYPES:
            for code in ERROR_CODES:
                if re.search(r'(?<![A-Za-z0-9_])' + re.escape(code) + r'(?![A-Za-z0-9_])', line):
                    codes.add(code)
        if line.startswith('{') and len(line) <= 1024:
            try:
                document = json.loads(line)
                if isinstance(document, dict) and allowed(document.get('error_code'), ERROR_CODES):
                    codes.add(document['error_code'])
                if isinstance(document, dict) and document.get('stage') in {'SOURCE_APPROVED', 'JOIN_VERIFIED'}:
                    fact = {'stage': document['stage']}
                    if allowed(document.get('alias'), {'a', 'b', 'c'}):
                        fact['alias'] = document['alias']
                    if allowed(document.get('join'), {'INNER', 'LEFT', 'RIGHT', 'FULL'}):
                        fact['join'] = document['join']
                    if type(document.get('rows')) is int and 0 <= document['rows'] <= 3000000:
                        fact['rows'] = document['rows']
                    progress.append(fact)
            except (ValueError, TypeError):
                pass
    return {'frames': frames[-20:], 'test_names': sorted(tests)[:30],
            'error_types': sorted(types), 'error_codes': sorted(codes), 'progress': progress[-10:]}


def collect_child_failure(group: str, before: set[Path], *, root: Path = ROOT, sources=None):
    if group != 'catalog-reports':
        return []
    sources = trusted_sources() if sources is None else sources
    observations = []
    base = root / '.codex-local/v080'
    for context in sorted(base.glob('trackvance-v080-test-catalog-reports-*')):
        isolation = context / 'isolation.json'
        if not isolation.is_file() or isolation.resolve() in before or context.is_symlink() or isolation.is_symlink():
            continue
        path = context / 'result.json'
        try:
            summary = sanitize_summary(json.loads(path.read_text(encoding='utf-8'))) if path.is_file() and not path.is_symlink() and path.stat().st_size <= MAX_BYTES else {'status': 'FAIL', 'summary_present': False}
        except (OSError, ValueError, RecursionError):
            summary = {'error': {'code': 'UNCLASSIFIED_CHILD_FAILURE'}}
        observations.append({'kind': 'CATALOG_PARTIAL_SUMMARY', 'result': summary})
        for rows in (120, 400000, 1000000):
            tier = path.parent / f'reports-{rows}.json'
            if tier.is_file() and not tier.is_symlink() and tier.stat().st_size <= MAX_BYTES:
                try:
                    observations.append({'kind': 'CATALOG_TIER_PARTIAL', 'rows': rows,
                                         'result': sanitize_summary(json.loads(tier.read_text(encoding='utf-8')))})
                except (OSError, ValueError, RecursionError):
                    pass
            facts = extract_log_facts(path.parent / f'reports-{rows}.private.log', sources)
            if any(facts.values()):
                observations.append({'kind': 'CATALOG_TIER_LOG_FACTS', 'rows': rows, 'facts': facts})
    return observations[:12]


def publish_failure(group: str, output: Path, *, source_sha: str, ci: dict, phases: list[dict],
                    error: Exception, phase: str, before: set[Path], root: Path = ROOT):
    manifest = load_manifest()
    require(group in {entry['id'] for entry in manifest['groups']} and SHA.fullmatch(source_sha), 'INVALID_FAILURE_IDENTITY')
    expected_job = next(entry['job_key'] for entry in manifest['groups'] if entry['id'] == group)
    require(isinstance(phase, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,80}', phase), 'INVALID_FAILURE_PHASE')
    require(set(ci) == {'run_id', 'run_attempt', 'job_id'} and str(ci['run_id']).isdigit() and int(ci['run_id']) > 0
            and str(ci['run_attempt']).isdigit() and int(ci['run_attempt']) > 0 and ci['job_id'] == expected_job, 'INVALID_FAILURE_CI_IDENTITY')
    sanitized_phases = []
    for record in phases:
        if isinstance(record, dict) and isinstance(record.get('name'), str) and re.fullmatch(r'[a-z][a-z0-9-]{0,80}', record['name']):
            sanitized_phases.append({key: record[key] for key in ('name', 'status', 'exit_code', 'timed_out', 'duration_seconds')
                                     if key in record and (key == 'name' or key == 'status' and record[key] in {'PASS', 'FAIL'}
                                     or key == 'exit_code' and type(record[key]) is int
                                     or key == 'timed_out' and type(record[key]) is bool
                                     or key == 'duration_seconds' and number(record[key]))})
    sources = trusted_sources()
    children = collect_child_failure(group, before, root=root, sources=sources)
    # Filenames are trusted phase identifiers from run_suite, not child output.
    log_facts = extract_log_facts(output / 'diagnostics' / group / (phase + '.private.log'), sources)
    code = 'GROUP_DEADLINE_EXPIRED' if isinstance(error, TimeoutError) else 'CHILD_EXIT_NONZERO'
    if isinstance(error, TimeoutError):
        pass
    elif not sanitized_phases or sanitized_phases[-1].get('status') != 'FAIL':
        code = 'EVIDENCE_VALIDATION_FAILED'
    elif sanitized_phases[-1].get('timed_out') is True:
        code = 'CHILD_TIMEOUT'
    record = {'schema_version': 1, 'kind': 'FAILURE_DIAGNOSTIC', 'group': group, 'source_sha': source_sha,
              'ci': ci, 'status': 'FAIL', 'certifies_final': False, 'phase': phase,
              'error': {'type': type(error).__name__ if type(error).__name__ in ERROR_TYPES else 'RuntimeError', 'code': code},
              'phases': sanitized_phases, 'log_facts': log_facts,
              'privacy': {'raw_logs': False, 'messages': False, 'environment': False, 'browser_traces': False, 'row_values': False}}
    evidence = output / 'evidence'; evidence.mkdir(parents=True, exist_ok=True)
    attachments = evidence / 'attachments'; attachments.mkdir(exist_ok=True)
    record['evidence'] = []
    for observation in children:
        target = attachments / ('failure-child-' + uuid4().hex + '.json')
        target.write_text(json.dumps(observation, indent=2) + '\n', encoding='utf-8')
        record['evidence'].append({'path': target.relative_to(evidence).as_posix(),
                                   'sha256': hashlib.sha256(target.read_bytes()).hexdigest(), 'kind': observation['kind']})
    target = evidence / ('failure-' + group + '-' + uuid4().hex + '.json')
    target.write_text(json.dumps(record, indent=2) + '\n', encoding='utf-8')
    return target
