"""Publish bounded failure facts; never publish messages, data or raw logs."""
from __future__ import annotations

import ast
import hashlib
import json
import math
import posixpath
import re
import subprocess
import traceback
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from ci.common import ROOT, SHA, load_manifest, require
from ci.compose_preflight import PREFLIGHT_CODES

MAX_BYTES = 2 * 1024**2
ERROR_TYPES = {'AssertionError', 'RuntimeError', 'ValueError', 'TimeoutError', 'TimeoutExpired',
               'FileNotFoundError', 'ConnectionError', 'HTTPError', 'URLError', 'IncompleteRead',
               'EvidenceError', 'PermissionError', 'OSError', 'KeyError', 'TypeError', 'MemoryError',
               'RecoveryCheckError', 'RecoveryCommandError', 'DeliveryEvidenceError',
               'ImportError', 'ModuleNotFoundError', 'ComposePreflightError', 'CalledProcessError',
               'SmokeFailure'}
ERROR_CODES = {'REPORT_TIMEOUT', 'REPORT_RESULT_LIMIT', 'REPORT_JOIN_LIMIT', 'REPORT_JOIN_EXPANSION',
               'REPORT_MEMORY_LIMIT', 'REPORT_DISK_LIMIT', 'REPORT_ENGINE_FAILED', 'REPORT_ENGINE_ERROR',
               'REPORT_SOURCE_CHANGED', 'REPORT_CONTEXT_EXPIRED', 'REPORT_CANCELLED',
               'ACQUISITION_ROW_LIMIT', 'CHILD_EXIT_NONZERO', 'CHILD_TIMEOUT', 'GROUP_DEADLINE_EXPIRED',
               'EVIDENCE_VALIDATION_FAILED', 'OWNED_CLEANUP_FAILED', 'UNCLASSIFIED_CHILD_FAILURE'}
ERROR_CODES |= {'CERTIFICATION_ASSERTION_FAILED', 'CERTIFICATION_EXCEPTION'}
ERROR_CODES |= PREFLIGHT_CODES
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
RECOVERY_PHASES = {'freshness_guards', 'external_postgresql', 'source_trackvance',
    'postgres_migration', 'source_trackvance_fixtures', 'delivery_operational_fixtures',
    'identity_fixtures', 'backup', 'destroy_source', 'restore', 'functional_recovery',
    'doctor_and_logs', 'freshness', 'authentic_source_build', 'source_fixtures',
    'source_backup', 'enable_disposable_access', 'current_credentials', 'current_backup_privacy',
    'authentic_archive', 'authentic_source_start', 'historical_fixtures', 'authentic_backup',
    'destroy_authentic_source', 'restore_080_stopped', 'multipart_fixture', 'native_backup',
    'fresh_restore', 'restored_multipart', 'native', 'legacy'}
RECOVERY_FUNCTIONAL_STAGES = {'IDENTITY', 'HISTORICAL_CONNECTION_AND_ARTIFACTS',
    'HISTORICAL_MONITOR', 'OPERATIONAL_DELIVERY', 'RESTORED_DELIVERY',
    'CORRECTED_SOURCE_ACQUISITION', 'COMPLETE_POPULATION_COMPARISON', 'CORRECTED_INTAKE',
    'EXCEPTION_VALIDATION', 'HISTORICAL_MONITOR_RECHECK'}
RECOVERY_CODES = {'RECOVERY_API_UNEXPECTED_HTTP', 'RECOVERY_RUN_NOT_SUCCESS',
    'INITIAL_DATASET_MUST_BE_PROVISIONAL',
    'RECOVERY_ACQUISITION_NOT_PUBLISHED', 'CANONICAL_ARTIFACT_HASH_MISMATCH',
    'CANONICAL_BUNDLE_INVENTORY_MISMATCH', 'CANONICAL_DESCRIPTOR_INVALID',
    'CANONICAL_DESCRIPTOR_HASH_MISMATCH', 'CANONICAL_DESCRIPTOR_TOTALS_MISMATCH',
    'CANONICAL_PART_SIZE_MISMATCH', 'CANONICAL_PART_HASH_MISMATCH',
    'SOURCE_CONNECTION_TEST_FAILED', 'SOURCE_SCHEMA_UNAVAILABLE', 'SOURCE_OBJECTS_UNAVAILABLE',
    'SOURCE_PREVIEW_MISMATCH', 'SOURCE_SNAPSHOT_MISMATCH', 'SOURCE_SNAPSHOT_LINEAGE_MISMATCH',
    'SOURCE_INTAKE_DECISION_MISMATCH', 'DELIVERY_EVIDENCE_NOT_COMMITTED',
    'DELIVERY_EVIDENCE_PENDING_REPAIR', 'DELIVERY_EVIDENCE_TIMEOUT'}
RECOVERY_CODES |= PREFLIGHT_CODES
RECOVERY_CODES |= {'RECOVERY_COMPARISON_POPULATION_MISMATCH', 'RECOVERY_CORRECTION_NOT_VALIDATED',
    'RECOVERY_COMPARISON_NOT_SUCCESS', 'RECOVERY_COMPARISON_REPORT_INVALID', 'RECOVERY_COMPARISON_TIMEOUT'}
COMPOSE_FAILURES = {'CONTAINER_UNHEALTHY', 'BUILD_FAILED', 'PORT_BIND_FAILED', 'OTHER_COMPOSE_FAILURE'}
COMPOSE_PHASES = {'docker', 'doctor', 'postgres_migrations', 'regression_smoke', 'browser',
    'storage_snapshot', 'restart_readiness', 'source_fixture', 'source_streaming',
    'source_certification', 'source_temporal', 'post_restart_source_check', 'credential_privacy'}
COMPOSE_HTTP_CODES = frozenset({'UNAUTHENTICATED', 'CSRF_FAILED', 'PERMISSION_DENIED',
    'PASSWORD_CHANGE_REQUIRED', 'NOT_FOUND', 'CONNECTION_DISABLED',
    'CONNECTION_CONFIGURATION_MISSING', 'CREDENTIAL_STORE_UNAVAILABLE',
    'SOURCE_AUTH_FAILED', 'SOURCE_PERMISSION_DENIED', 'SOURCE_TIMEOUT', 'SOURCE_UNAVAILABLE'})
RUNTIME_SERVICES = {'postgres', 'api', 'worker', 'delivery-worker', 'acquisition-worker',
                    'document-worker', 'comparison-worker',
                    'report-worker', 'scheduler', 'events-notifications', 'events-chaining',
                    'web', 'mock-oidc', 'source-postgres', 'source-sqlserver'}
SAFE_FAILURE_RUNNERS = {
    'delivery': ('suite-delivery', 'delivery-e2e', r'trackvance-delivery-e2e-[a-f0-9]{12}', 'DELIVERY'),
    'identity-sso': ('suite-identity-sso', 'v070', r'trackvance-v070-test-identity-[a-f0-9]{12}', 'IDENTITY'),
}
IDENTITY_FAILURE_PHASES = {'SCOPE', 'STARTUP', 'HEALTH', 'DOCTOR', 'BROWSER', 'TEMPORARY_CREDENTIALS',
                           'CREDENTIAL_PRIVACY', 'STORAGE_BEFORE', 'NO_EMAIL_RECORDS', 'RESTART',
                           'RESTART_READINESS', 'STORAGE_AFTER', 'STATE_PRESERVATION'}
DOCTOR_HEALTH_LABELS = {'API', 'Base de datos', 'Almacenamiento', 'Migraciones Alembic'}
DOCTOR_HEALTH_UNAVAILABLE = {'API / base de datos / almacenamiento / migraciones'}
DOCTOR_COMMAND_LABELS = {'Docker Linux disponible', 'Configuración Compose', 'Worker Compose DEFAULT',
                         'Worker Compose DELIVERY', 'Worker Compose ACQUISITION', 'Worker Compose REPORT',
                         'Componente Compose scheduler', 'Componente Compose events-notifications', 'Componente Compose events-chaining'}


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


def sanitize_compose_http_failure(value):
    """Retain an HTTP status and known public code, never response content."""
    if not isinstance(value, dict):
        return {}
    status = value.get('http_status')
    if type(status) is not int or not 100 <= status <= 599:
        return {}
    result = {'http_status': status}
    if allowed(value.get('api_error_code'), COMPOSE_HTTP_CODES):
        result['api_error_code'] = value['api_error_code']
    return result


def sanitize_download_oracles(value):
    """Retain complete count/checksum comparisons, without source data or text."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for format, limit in (('CSV', 100000), ('XLSX', 50000)):
        oracle = value.get(format)
        if (not isinstance(oracle, dict) or set(oracle) != {'actual', 'expected', 'status'}
                or not allowed(oracle.get('status'), {'PASS', 'FAIL'})):
            continue
        valid = True
        for key, maximum in (('actual', 1000000), ('expected', limit)):
            item = oracle[key]
            if (not isinstance(item, dict) or set(item) != {'rows', 'sha256'}
                    or type(item.get('rows')) is not int or not 0 <= item['rows'] <= maximum
                    or not isinstance(item.get('sha256'), str)
                    or not re.fullmatch(r'[a-f0-9]{64}', item['sha256'])):
                valid = False
                break
        if not valid or (oracle['actual'] == oracle['expected']) != (oracle['status'] == 'PASS'):
            continue
        result[format] = {'actual': dict(oracle['actual']), 'expected': dict(oracle['expected']),
                          'status': oracle['status']}
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
    oracles = sanitize_download_oracles(value.get('download_oracles'))
    if oracles:
        result['download_oracles'] = oracles
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


def trusted_test_source(raw: str, function: str, sources: set[str]) -> str | None:
    """Resolve pytest's cwd-relative spelling only to one tracked declaration."""
    if not function.startswith('test') or any(mark in raw for mark in (':', '\n', '\r', '\0', '?', '#')):
        return None
    relative = posixpath.normpath(raw.replace('\\', '/'))
    while relative.startswith('../'):
        relative = relative[3:]
    if relative.startswith('/') or len(relative.split('/')) < 2 or not relative.endswith('.py'):
        return None
    matches = [source for source in sources if source == relative or source.endswith('/' + relative)]
    if len(matches) != 1:
        return None
    source = ROOT / matches[0]
    if not source.is_file() or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
                                   for part in (source, *source.parents)):
        return None
    try:
        tree = ast.parse(source.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, SyntaxError):
        return None
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function
               for node in ast.walk(tree)):
        return None
    return matches[0]


def extract_log_facts(path: Path, sources: set[str]):
    """Read only a bounded tail. Source lines and exception messages are omitted."""
    if not path.is_file() or path.is_symlink():
        return {}
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - MAX_BYTES))
        text = stream.read(MAX_BYTES).decode('utf-8', errors='replace')
    frames, tests, types, codes, progress = [], set(), set(), set(), []
    for line in text.splitlines():
        short = re.fullmatch(r'\s*((?:[A-Za-z]:)?[^:\r\n\0]+\.py):([0-9]{1,7}):[^\r\n\0]*', line)
        if short and (frame := trusted_short_python_frame(short[1], int(short[2]), sources)) and frame not in frames:
            frames.append(frame)
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
        failure = re.match(r'^\s*(?:FAILED|ERROR) ([^ :]+)::([A-Za-z_][A-Za-z0-9_]*)(?:\[[^\r\n]*\])?(?:\s+-.*)?$', line)
        if failure and (source := trusted_test_source(failure[1], failure[2], sources)):
            tests.add(source + '::' + failure[2])  # parameter values are never retained
        # Python qualifies this trusted exception by its defining module. No
        # arbitrary module names, exception message or resolved values survive.
        exception = re.match(r'^(?:E\s+)?(?:(?:scripts\.)?ci\.compose_preflight\.)?([A-Za-z][A-Za-z0-9_]*)(?::|$)', line)
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


def trusted_short_python_frame(raw, line, sources):
    """Resolve pytest short trace locations solely to bounded tracked Python."""
    if any(token in raw for token in ('://', '?', '#', '\n', '\r', '\0')):
        return None
    relative = posixpath.normpath(raw.replace('\\', '/'))
    while relative.startswith('../'):
        relative = relative[3:]
    matches = [source for source in sources if source.endswith('.py')
        and (relative == source or relative.endswith('/' + source) or source.endswith('/' + relative))]
    if len(matches) != 1:
        return None
    path = ROOT / matches[0]
    if (not path.is_file() or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
                                for part in (path, *path.parents))):
        return None
    try:
        content = path.read_text(encoding='utf-8')
        if not 1 <= line <= len(content.splitlines()):
            return None
        tree = ast.parse(content)
    except (OSError, UnicodeError, SyntaxError):
        return None
    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= line <= (node.end_lineno or node.lineno)]
    function = max(functions, key=lambda node: node.lineno).name if functions else '<module>'
    return {'file': matches[0], 'line': line, 'function': function}


def collect_child_failure(group: str, before: set[Path], *, root: Path = ROOT, sources=None, source_sha=None, ci=None):
    group = {"compose-functional": "compose-critical", "connections-functional": "connections",
             "backup-basic": "backup-restore", "catalog-reports-functional": "catalog-reports"}.get(group, group)
    if group == 'corrections-browser':
        return collect_corrections_browser_failure(before, root=root, sources=sources, source_sha=source_sha, ci=ci)
    if group == 'delivery':
        return collect_delivery_failure(before, root=root, sources=sources, source_sha=source_sha, ci=ci)
    if group == 'backup-restore':
        return collect_recovery_failure(before, root=root)
    if group == 'corrections-recovery':
        return collect_corrections_recovery_failure(before, root=root)
    if group in {'async-volume-100', 'async-volume-500', 'async-volume-1024'}:
        return collect_async_failure(group, before, root=root)
    if group in {'compose-critical', 'identity-sso', 'connections'}:
        observations = collect_compose_failure(group, before, root=root, sources=sources)
        if group == 'identity-sso':
            observations.extend(collect_identity_failure(before, root=root, sources=sources, source_sha=source_sha, ci=ci))
        return observations[:4]
    if group != 'catalog-reports':
        return []
    sources = trusted_sources() if sources is None else sources
    observations = []
    base = root / '.codex-local/v080'
    if not base.is_dir() or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
                               for part in (base, base.parent, base.parent.parent)):
        return []
    for context in sorted(base.glob('trackvance-v080-test-catalog-reports-*')):
        isolation = context / 'isolation.json'
        if (not re.fullmatch(r'trackvance-v080-test-catalog-reports-[a-f0-9]{12}', context.name)
                or not context.is_dir() or context.is_symlink() or getattr(context, 'is_junction', lambda: False)()
                or not fresh_catalog_file(isolation, before)):
            continue
        try:
            identity = json.loads(isolation.read_text(encoding='utf-8'))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(identity, dict) or identity.get('project') != context.name:
            continue
        path = context / 'result.json'
        try:
            summary = sanitize_summary(json.loads(path.read_text(encoding='utf-8'))) if fresh_catalog_file(path, before) else {'status': 'FAIL', 'summary_present': False}
        except (OSError, ValueError, RecursionError):
            summary = {'error': {'code': 'UNCLASSIFIED_CHILD_FAILURE'}}
        observations.append({'kind': 'CATALOG_PARTIAL_SUMMARY', 'result': summary})
        browser = context / 'browser-summary.json'
        if fresh_catalog_file(browser, before):
            try:
                value = json.loads(browser.read_text(encoding='utf-8'))
                if isinstance(value, dict) and (value.get('status') == 'FAIL'
                        or any(type(value.get(key)) is int and value[key] > 0 for key in ('unexpected', 'skipped', 'flaky'))
                        or type(value.get('expected')) is int and value['expected'] != 2):
                    observations.append({'kind': 'CATALOG_BROWSER_FAILURE',
                                         'result': sanitize_browser_failure(value, sources)})
            except (OSError, ValueError, RecursionError):
                pass
        for rows in (120, 400000, 1000000):
            tier = path.parent / f'reports-{rows}.json'
            if fresh_catalog_file(tier, before):
                try:
                    observations.append({'kind': 'CATALOG_TIER_PARTIAL', 'rows': rows,
                                         'result': sanitize_summary(json.loads(tier.read_text(encoding='utf-8')))})
                except (OSError, ValueError, RecursionError):
                    pass
            log = path.parent / f'reports-{rows}.private.log'
            facts = extract_log_facts(log, sources) if fresh_catalog_file(log, before, bounded=False) else {}
            if any(facts.values()):
                observations.append({'kind': 'CATALOG_TIER_LOG_FACTS', 'rows': rows, 'facts': facts})
        observations.extend(collect_catalog_recovery_failure(context, before, sources))
        http = context / 'reports-ephemeral-http.json'
        if fresh_catalog_file(http, before):
            try:
                value = json.loads(http.read_text(encoding='utf-8'))
                if (isinstance(value, dict) and isinstance(value.get('project'), str)
                        and re.fullmatch(r'trackvance-v080-test-reports-http-[a-f0-9]{12}', value['project'])
                        and allowed(value.get('status'), {'PASS', 'FAIL'})):
                    observations.append({'kind': 'CATALOG_HTTP_PARTIAL_SUMMARY', 'result': sanitize_catalog_http(value)})
            except (OSError, ValueError, RecursionError):
                pass
    return observations[:24]


def fresh_catalog_file(path, before, *, bounded=True):
    return (path.is_file() and not path.is_symlink() and not getattr(path, 'is_junction', lambda: False)()
            and path.resolve() not in before and (not bounded or path.stat().st_size <= MAX_BYTES))


def delivery_diagnostic_context(evidence, project, environment):
    return _runner_diagnostic_context('delivery', evidence, project, environment)


def identity_diagnostic_context(evidence, project, environment):
    return _runner_diagnostic_context('identity-sso', evidence, project, environment)


def _runner_diagnostic_context(runner, evidence, project, environment):
    """Bind diagnostics to the existing private profile; copy no configuration."""
    if runner not in SAFE_FAILURE_RUNNERS:
        return None
    job, folder, pattern, prefix = SAFE_FAILURE_RUNNERS[runner]
    sha = environment.get('CI_SOURCE_SHA', environment.get('GITHUB_SHA', ''))
    ci = {'run_id': environment.get('GITHUB_RUN_ID', ''), 'run_attempt': environment.get('GITHUB_RUN_ATTEMPT', ''),
          'job_id': environment.get('CI_JOB_ID', environment.get('GITHUB_JOB', ''))}
    if (not SHA.fullmatch(sha) or ci['job_id'] != job
            or not all(isinstance(ci[key], str) and ci[key].isdigit() and int(ci[key]) > 0 for key in ('run_id', 'run_attempt'))
            or not re.fullmatch(pattern, project)
            or evidence.resolve() != (ROOT / '.codex-local' / folder / project).resolve()
            or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)() for part in (evidence, *evidence.parents))):
        return None
    actual = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, capture_output=True,
                            text=True, check=True, timeout=15).stdout.strip()
    if actual != sha:
        return None
    references = {}
    for name in ('private-compose.json', 'private.empty.env'):
        path = evidence / name
        if not fresh_catalog_file(path, set()) or (name == 'private.empty.env' and path.stat().st_size != 0):
            return None
        references[name] = {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size}
    path = evidence / (prefix.lower() + '-diagnostic-context.json')
    with path.open('x', encoding='utf-8') as stream:
        json.dump({'schema_version': 1, 'kind': prefix + '_DIAGNOSTIC_CONTEXT', 'project': project,
                   'source_sha': sha, 'ci': ci, 'diagnostic_id': uuid4().hex, 'isolation': references}, stream)
    return path


def write_delivery_failure(error, context):
    return _write_runner_failure('delivery', error, context)


def write_identity_failure(error, context, phase):
    return _write_runner_failure('identity-sso', error, context, phase=phase)


def _write_runner_failure(runner, error, context, *, phase=None):
    """Retain trusted traceback locations and source hashes, never exception text."""
    if (runner not in SAFE_FAILURE_RUNNERS or context is None or not fresh_catalog_file(context, set())
            or runner == 'identity-sso' and not allowed(phase, IDENTITY_FAILURE_PHASES)):
        return
    prefix = SAFE_FAILURE_RUNNERS[runner][3]
    frames = capture_runner_frames(error)
    path = context.parent / (prefix.lower() + '-failure-safe.json')
    with path.open('x', encoding='utf-8') as stream:
        json.dump({'schema_version': 1, 'kind': prefix + '_FAILURE_SAFE', 'status': 'FAIL',
                   'context_sha256': hashlib.sha256(context.read_bytes()).hexdigest(),
                   'error_type': type(error).__name__ if type(error).__name__ in ERROR_TYPES else 'RuntimeError',
                   'frames': frames, **({'phase': phase} if runner == 'identity-sso' else {})}, stream)


def capture_runner_frames(error):
    """Capture tracked traceback locations without any exception/source text."""
    try:
        sources = trusted_sources()
    except (OSError, subprocess.SubprocessError):
        return []
    frames = []
    for frame in traceback.extract_tb(error.__traceback__)[-20:]:
        source = Path(frame.filename)
        if (not source.is_absolute() or not source.is_relative_to(ROOT)
                or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)() for part in (source, *source.parents))):
            continue
        name = source.relative_to(ROOT).as_posix()
        if name not in sources or not source.is_file():
            continue
        raw = source.read_bytes()
        if (not 1 <= frame.lineno <= len(raw.decode('utf-8').splitlines())
                or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*|<module>', frame.name)):
            continue
        frames.append({'file': name, 'line': frame.lineno, 'function': frame.name,
                       'source_sha256': hashlib.sha256(raw).hexdigest()})
    return frames


def sanitize_runner_frames(value, sources, root=ROOT):
    if not isinstance(value, list):
        return []
    frames = []
    for frame in value[:20]:
        if not isinstance(frame, dict) or frame.get('file') not in sources:
            continue
        path = root / frame['file']
        if (not path.is_file() or not path.resolve().is_relative_to(root.resolve())
                or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)() for part in (path, *path.parents))):
            continue
        raw = path.read_bytes()
        line, function = frame.get('line'), frame.get('function')
        if (frame.get('source_sha256') != hashlib.sha256(raw).hexdigest()
                or type(line) is not int or not 1 <= line <= len(raw.decode('utf-8').splitlines())
                or not isinstance(function, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*|<module>', function)):
            continue
        if function != '<module>' and not re.search(r'\b(?:def|function)\s+' + re.escape(function) + r'\b', raw.decode('utf-8')):
            continue
        frames.append({'file': frame['file'], 'line': line, 'function': function, 'source_sha256': frame['source_sha256']})
    return frames


def collect_delivery_failure(before, *, root=ROOT, sources=None, source_sha=None, ci=None):
    return _collect_runner_failure('delivery', before, root=root, sources=sources, source_sha=source_sha, ci=ci)


def collect_identity_failure(before, *, root=ROOT, sources=None, source_sha=None, ci=None):
    return _collect_runner_failure('identity-sso', before, root=root, sources=sources, source_sha=source_sha, ci=ci)


def safe_doctor_result(value, exit_code):
    """Accept only the existing Doctor --docker --json fixed, message-free schema."""
    if (not isinstance(value, dict) or set(value) != {'checks', 'ok', 'command_diagnostics'}
            or type(exit_code) is not int or exit_code not in {0, 1} or type(value['ok']) is not bool
            or not isinstance(value['checks'], dict) or not isinstance(value['command_diagnostics'], dict)
            or set(value['command_diagnostics']) != DOCTOR_COMMAND_LABELS
            or set(value['checks']) not in (DOCTOR_HEALTH_LABELS | DOCTOR_COMMAND_LABELS, DOCTOR_HEALTH_UNAVAILABLE | DOCTOR_COMMAND_LABELS)
            or any(type(item) is not bool for item in value['checks'].values())
            or DOCTOR_HEALTH_UNAVAILABLE <= set(value['checks']) and any(value['checks'][label] for label in DOCTOR_HEALTH_UNAVAILABLE)
            or value['ok'] != all(value['checks'].values()) or exit_code != int(not value['ok'])):
        return None
    for label, diagnostic in value['command_diagnostics'].items():
        if (not isinstance(diagnostic, dict) or set(diagnostic) != {'reason', 'duration_seconds', 'timeout_seconds', 'exit_code'}
                or not allowed(diagnostic['reason'], {'OK', 'EXIT_CODE', 'EXECUTABLE_NOT_FOUND', 'TIMEOUT'})
                or not number(diagnostic['duration_seconds']) or not number(diagnostic['timeout_seconds'])
                or diagnostic['timeout_seconds'] != 15 or value['checks'][label] != (diagnostic['reason'] == 'OK')
                or diagnostic['reason'] in {'TIMEOUT', 'EXECUTABLE_NOT_FOUND'} and diagnostic['exit_code'] is not None
                or diagnostic['reason'] == 'OK' and (type(diagnostic['exit_code']) is not int or diagnostic['exit_code'] != 0)
                or diagnostic['reason'] == 'EXIT_CODE' and (type(diagnostic['exit_code']) is not int or not -255 <= diagnostic['exit_code'] <= 255 or diagnostic['exit_code'] == 0)):
            return None
    return value


def write_delivery_doctor_result(stdout, exit_code, context):
    if context is None or not isinstance(stdout, str) or len(stdout.encode('utf-8')) > MAX_BYTES or not fresh_catalog_file(context, set()):
        return
    value = safe_doctor_result(json.loads(stdout), exit_code)
    source = ROOT / 'scripts/doctor.py'
    if value is None or not source.is_file() or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)() for part in (source, *source.parents)):
        return
    with (context.parent / 'delivery-doctor-safe.json').open('x', encoding='utf-8') as stream:
        json.dump({'schema_version': 1, 'kind': 'DELIVERY_DOCTOR_SAFE',
                   'context_sha256': hashlib.sha256(context.read_bytes()).hexdigest(),
                   'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'exit_code': exit_code, 'result': value}, stream)


def collect_delivery_doctor(path, context_hash, before, root, sources):
    if not fresh_catalog_file(path, before):
        return None
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        source = root / 'scripts/doctor.py'
        if (isinstance(value, dict) and set(value) == {'schema_version', 'kind', 'context_sha256', 'source_sha256', 'exit_code', 'result'}
                and type(value['schema_version']) is int and value['schema_version'] == 1 and value['kind'] == 'DELIVERY_DOCTOR_SAFE'
                and value['context_sha256'] == context_hash and 'scripts/doctor.py' in sources
                and source.is_file() and not any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)() for part in (source, *source.parents))
                and value['source_sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
                and (result := safe_doctor_result(value['result'], value['exit_code'])) is not None):
            return {'exit_code': value['exit_code'], 'source_sha256': value['source_sha256'], **result}
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        pass  # Optional Doctor details cannot suppress the actual failure frames.
    return None


def _collect_runner_failure(runner, before, *, root=ROOT, sources=None, source_sha=None, ci=None):
    """Read only fresh UUID-owned safe receipts, not Delivery's raw result/log."""
    if runner not in SAFE_FAILURE_RUNNERS:
        return []
    job, folder, pattern, prefix = SAFE_FAILURE_RUNNERS[runner]
    if (not isinstance(source_sha, str) or not SHA.fullmatch(source_sha) or not isinstance(ci, dict)
            or set(ci) != {'run_id', 'run_attempt', 'job_id'} or ci.get('job_id') != job
            or not all(isinstance(ci[key], str) and ci[key].isdigit() and int(ci[key]) > 0 for key in ('run_id', 'run_attempt'))):
        return []
    sources = trusted_sources() if sources is None else sources
    base = root / '.codex-local' / folder
    if not base.is_dir() or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)() for part in (base, *base.parents)):
        return []
    observations = []
    for directory in sorted(base.iterdir()):
        if (not re.fullmatch(pattern, directory.name) or not directory.is_dir()
                or directory.is_symlink() or getattr(directory, 'is_junction', lambda: False)()):
            continue
        paths = [directory / name for name in (prefix.lower() + '-diagnostic-context.json', prefix.lower() + '-failure-safe.json', 'private-compose.json', 'private.empty.env')]
        if not all(fresh_catalog_file(path, before) for path in paths):
            continue
        try:
            context, receipt, profile = (json.loads(path.read_text(encoding='utf-8')) for path in paths[:3])
            if (not isinstance(context, dict) or set(context) != {'schema_version', 'kind', 'project', 'source_sha', 'ci', 'diagnostic_id', 'isolation'}
                    or type(context['schema_version']) is not int or context['schema_version'] != 1 or context['kind'] != prefix + '_DIAGNOSTIC_CONTEXT'
                    or context['project'] != directory.name or context['source_sha'] != source_sha or context['ci'] != ci
                    or not isinstance(context['diagnostic_id'], str) or not re.fullmatch(r'[a-f0-9]{32}', context['diagnostic_id'])
                    or not isinstance(context['isolation'], dict) or set(context['isolation']) != {'private-compose.json', 'private.empty.env'}
                    or not isinstance(receipt, dict) or set(receipt) != {'schema_version', 'kind', 'status', 'context_sha256', 'error_type', 'frames'} | ({'phase'} if runner == 'identity-sso' else set())
                    or type(receipt['schema_version']) is not int or receipt['schema_version'] != 1 or receipt['kind'] != prefix + '_FAILURE_SAFE'
                    or runner == 'identity-sso' and not allowed(receipt['phase'], IDENTITY_FAILURE_PHASES)
                    or receipt['status'] != 'FAIL' or receipt['context_sha256'] != hashlib.sha256(paths[0].read_bytes()).hexdigest()
                    or not allowed(receipt['error_type'], ERROR_TYPES) or not isinstance(receipt['frames'], list) or len(receipt['frames']) > 20
                    or not isinstance(profile, dict) or not isinstance(profile.get('services'), dict)):
                continue
            if any(context['isolation'][path.name] != {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'bytes': path.stat().st_size} for path in paths[2:]) or paths[3].stat().st_size != 0:
                continue
            if any(not isinstance(profile['services'].get(role), dict)
                   or not isinstance(profile['services'][role].get('environment'), dict)
                   or profile['services'][role]['environment'].get('TRACKVANCE_CERTIFICATION_PROJECT') != directory.name
                   for role in ('api', 'worker', 'delivery-worker')):
                continue
            frames = []
            for frame in receipt['frames']:
                if (not isinstance(frame, dict) or set(frame) != {'file', 'line', 'function', 'source_sha256'}
                        or not isinstance(frame['file'], str) or frame['file'] not in sources
                        or '\\' in frame['file'] or '..' in frame['file'].split('/') or Path(frame['file']).is_absolute()
                        or type(frame['line']) is not int or not isinstance(frame['function'], str)
                        or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*|<module>', frame['function'])):
                    raise ValueError('UNTRUSTED_DELIVERY_FRAME')
                source = root / frame['file']
                if (not source.is_file() or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)() for part in (source, *source.parents))):
                    raise ValueError('UNTRUSTED_DELIVERY_SOURCE')
                raw = source.read_bytes()
                if (hashlib.sha256(raw).hexdigest() != frame['source_sha256'] or not 1 <= frame['line'] <= len(raw.decode('utf-8').splitlines())
                        or frame['function'] != '<module>' and not re.search(r'\bdef\s+' + re.escape(frame['function']) + r'\b', raw.decode('utf-8'))):
                    raise ValueError('UNTRUSTED_DELIVERY_SOURCE_HASH_OR_LOCATION')
                frames.append(dict(frame))
            observation = {'kind': prefix + '_FAILURE_SAFE', 'error_type': receipt['error_type'], 'frames': frames,
                           **({'phase': receipt['phase']} if runner == 'identity-sso' else {})}
            if runner == 'delivery':
                doctor = collect_delivery_doctor(directory / 'delivery-doctor-safe.json', receipt['context_sha256'], before, root, sources)
                if doctor is not None:
                    observation['doctor'] = doctor
            browser = directory / 'browser-summary.json'
            if fresh_catalog_file(browser, before):
                value = json.loads(browser.read_text(encoding='utf-8'))
                if isinstance(value, dict) and (value.get('status') == 'FAIL' or type(value.get('unexpected')) is int and value['unexpected'] > 0):
                    observation['browser'] = sanitize_browser_failure(value, sources)
            observations.append(observation)
        except (OSError, ValueError, TypeError, RecursionError):
            continue
    return observations[:4]


def sanitize_browser_failure(value, sources):
    """Only counters and verified source locations survive; no test title or message."""
    result = {'status': 'FAIL'}
    for key in ('expected', 'unexpected', 'skipped', 'flaky'):
        if type(value.get(key)) is int and 0 <= value[key] <= 1000000:
            result[key] = value[key]
    if number(value.get('duration')) and value['duration'] <= 3600000:
        result['duration'] = value['duration']
    if allowed(value.get('error_type'), ERROR_TYPES):
        result['error_type'] = value['error_type']
    failures = []
    for failure in value.get('failures', [])[:30] if isinstance(value.get('failures'), list) else []:
        if not isinstance(failure, dict):
            continue
        item = {'location_present': False}
        if allowed(failure.get('status'), {'failed', 'timedOut', 'interrupted'}):
            item['status'] = failure['status']
        location = failure.get('location')
        if isinstance(location, dict) and isinstance(location.get('file'), str):
            raw = location['file'].replace('\\', '/')
            selected = [source for source in sources if source.startswith('frontend/tests-e2e/')
                and source.endswith('.spec.ts') and (raw == source or raw.endswith('/' + source)
                or raw == Path(source).name or raw == source.removeprefix('frontend/'))]
            if len(selected) == 1 and not any(mark in raw for mark in ('://', '\n', '\r', '\0', '?', '#')) and '..' not in raw.split('/'):
                source = ROOT / selected[0]
                if source.is_file() and not any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
                                               for part in (source, *source.parents)):
                    lines = source.read_text(encoding='utf-8').splitlines()
                    line, column = location.get('line'), location.get('column')
                    if type(line) is int and 1 <= line <= len(lines):
                        safe = {'file': selected[0], 'line': line}
                        if type(column) is int and 1 <= column <= min(4096, len(lines[line - 1]) + 1):
                            safe['column'] = column
                        item.update(location_present=True, location=safe)
        failures.append(item)
    result.update(failures=failures, failure_location_present=any(item['location_present'] for item in failures))
    return result


def collect_corrections_browser_failure(before, *, root=ROOT, sources=None, source_sha=None, ci=None):
    """Bind the root summary to the fresh group's one final failed population receipt."""
    if (not isinstance(source_sha, str) or not SHA.fullmatch(source_sha) or not isinstance(ci, dict)
            or set(ci) != {'run_id', 'run_attempt', 'job_id'} or ci.get('job_id') != 'suite-corrections-browser'):
        return []
    sources = trusted_sources() if sources is None else sources
    base = root / '.codex-local/v070'
    if not base.is_dir() or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
                               for part in (base, base.parent, base.parent.parent)):
        return []
    observations = []
    for context in sorted(base.iterdir()):
        if (not re.fullmatch(r'trackvance-v070-test-corrections-browser-[a-f0-9]{12}', context.name)
                or not context.is_dir() or context.is_symlink() or getattr(context, 'is_junction', lambda: False)()):
            continue
        paths = [context / 'isolation.json', context / 'corrections-evidence.json']
        if not all(fresh_catalog_file(path, before) for path in paths):
            continue
        try:
            identity, group = (json.loads(path.read_text(encoding='utf-8')) for path in paths)
            if (not isinstance(identity, dict) or identity.get('project') != context.name
                    or not isinstance(group, dict) or type(group.get('schema_version')) is not int or group['schema_version'] != 1 or group.get('kind') != 'XLSX_GROUP'
                    or group.get('group') != 'corrections-browser' or group.get('status') != 'FAIL'
                    or group.get('source_sha') != source_sha or group.get('ci') != ci
                    or group.get('required_scenarios') != ['browser-400000-shared', 'browser-1000000-shared']
                    or not isinstance(group.get('scenario_results'), list) or len(group['scenario_results']) > 2):
                continue
            failed = []
            for reference in group['scenario_results']:
                if not isinstance(reference, dict) or reference.get('kind') != 'XLSX_SCENARIO' or not isinstance(reference.get('path'), str):
                    continue
                match = re.fullmatch(r'xlsx-scenarios-corrections-browser-[a-f0-9]{12}/'
                    r'scenario-browser-(400000|1000000)-shared-[a-f0-9]{32}\.json', reference['path'])
                if not match:
                    continue
                path = context / reference['path']
                if (path.parent.is_symlink() or getattr(path.parent, 'is_junction', lambda: False)()
                        or not fresh_catalog_file(path, before) or hashlib.sha256(path.read_bytes()).hexdigest() != reference.get('sha256')):
                    continue
                receipt = json.loads(path.read_text(encoding='utf-8'))
                scenario, rows = 'browser-' + match[1] + '-shared', int(match[1])
                if (isinstance(receipt, dict) and type(receipt.get('schema_version')) is int and receipt['schema_version'] == 1 and receipt.get('kind') == 'XLSX_SCENARIO'
                        and receipt.get('status') == 'FAIL' and receipt.get('group') == 'corrections-browser'
                        and receipt.get('source_sha') == source_sha and receipt.get('ci') == ci
                        and receipt.get('scenario_id') == reference.get('scenario_id') == scenario
                        and type(receipt.get('rows')) is int and receipt['rows'] == rows and receipt.get('variant') == 'shared'):
                    failed.append((scenario, rows))
            if len(failed) != 1:
                continue
            path = context / 'browser-summary.json'
            if (path.exists() or path.is_symlink()) and not fresh_catalog_file(path, before):
                continue
            value = json.loads(path.read_text(encoding='utf-8')) if fresh_catalog_file(path, before) else None
            if value is not None and (not isinstance(value, dict) or value.get('status') != 'FAIL'):
                continue
            scenario, rows = failed[0]
            observations.append({'kind': 'CORRECTIONS_BROWSER_PARTIAL_SUMMARY', 'scenario_id': scenario, 'rows': rows,
                'summary_present': value is not None, 'result': sanitize_browser_failure(value, sources) if value is not None
                else {'status': 'FAIL', 'failure_location_present': False}})
        except (OSError, ValueError, RecursionError):
            continue
    return observations[:4]


def collect_catalog_recovery_failure(context, before, sources):
    """Read only the failed catalog child's fresh UUID directory, never its raw log."""
    observations = []
    for directory in sorted(context.glob('catalog-recovery-*')):
        if (not re.fullmatch(r'catalog-recovery-[a-f0-9]{12}', directory.name) or not directory.is_dir()
                or directory.is_symlink() or getattr(directory, 'is_junction', lambda: False)()):
            continue
        path = directory / 'result.json'
        if not fresh_catalog_file(path, before):
            continue
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, RecursionError):
            continue
        if (not isinstance(value, dict) or value.get('source_project') != context.name
                or not allowed(value.get('mode'), {'native', 'legacy', 'legacy080', 'both'})
                or not allowed(value.get('status'), {'PASS', 'FAIL'})):
            continue
        result = {**sanitize_recovery_summary(value), 'mode': value['mode']}
        for profile, versions in (('native', {'0.8.5', '0.9.0', '0.9.5', '0.9.7'}), ('legacy', {'0.7.0'}), ('legacy080', {'0.8.0'})):
            child = value.get(profile)
            if (isinstance(child, dict) and value['mode'] in {profile, 'both'}
                    and allowed(child.get('source_version'), versions)
                    and allowed(child.get('target_version'), {'0.8.5', '0.9.0', '0.9.5', '0.9.7'})):
                result[profile] = sanitize_recovery_summary(child)
        observations.append({'kind': 'CATALOG_RECOVERY_PARTIAL_SUMMARY', 'result': result})
        log = directory / 'diagnostic.private.log'
        facts = extract_log_facts(log, sources) if fresh_catalog_file(log, before, bounded=False) else {}
        if any(facts.values()):
            observations.append({'kind': 'CATALOG_RECOVERY_LOG_FACTS', 'facts': facts})
    return observations[:8]


def sanitize_catalog_http(value):
    result = sanitize_summary(value)
    for key, choices in (('stage', {'SYNTHETIC_AUTHORIZATION', 'SYNTHETIC_GOVERNANCE',
            'REAL_ACQUISITION_AND_STRICT_INTAKE', 'FREEZE_JOINT_CONTEXT', 'VERIFY_FIXTURE_JOBS_TERMINAL',
            'STOP_COMPLETED_FIXTURE_WORKERS', 'STORAGE_AND_METADATA_BASELINE',
            'VERIFY_STORAGE_METADATA_AND_SYSCALLS', 'EXECUTE_HTTP_CASE', 'COMPLETE'}),
            ('current_case', {'PREVIEW_SUCCESS', 'PREVIEW_LARGE_CONTEXT_REQUEST', 'PREVIEW_RESOURCE_FAILURE',
            'PREVIEW_CONSUMER_DISCONNECT', *(f'{fmt}_{suffix}' for fmt in ('CSV', 'XLSX')
                for suffix in ('SUCCESS', 'RESOURCE_FAILURE', 'CONSUMER_DISCONNECT'))}),
            ('api_error_code', ERROR_CODES)):
        if allowed(value.get(key), choices):
            result[key] = value[key]
    return result


def sanitize_recovery_summary(value):
    """Closed runner diagnostics and public route metadata; no bodies or rows."""
    if not isinstance(value, dict):
        return {}
    result = {'status': value['status']} if allowed(value.get('status'), {'PASS', 'FAIL'}) else {}
    for key, choices in (('failed_stage', RECOVERY_PHASES), ('functional_stage', RECOVERY_FUNCTIONAL_STAGES), ('error_type', ERROR_TYPES),
                         ('error_code', RECOVERY_CODES),
                         ('error_category', {'CONNECTION', 'SQL', 'UNHEALTHY', 'BUILD', 'UNKNOWN'}),
                         ('source_version', {'0.5.1', '0.6.0', '0.6.1', '0.7.0', '0.8.0', '0.8.5', '0.9.0', '0.9.5', '0.9.7'}),
                         ('target_version', {'0.8.5', '0.9.0', '0.9.5', '0.9.7'}), ('cleanup', {'PASS', 'FAIL'}),
                         ('main_inventory', {'UNCHANGED', 'CHANGED_OR_UNVERIFIABLE', 'UNVERIFIABLE'}),
                         ('cleanup_error_type', ERROR_TYPES), ('inventory_error_type', ERROR_TYPES)):
        if allowed(value.get(key), choices):
            result[key] = value[key]
    if type(value.get('exit_code')) is int and -255 <= value['exit_code'] <= 255:
        result['exit_code'] = value['exit_code']
    if number(value.get('duration_seconds')):
        result['duration_seconds'] = value['duration_seconds']
    request = sanitize_public_request_metadata(value.get('target_last_request'))
    if request:
        result['target_last_request'] = request
    return result


def sanitize_public_request_metadata(value):
    """Match a method and normalized route against the committed API contract."""
    if not isinstance(value, dict) or value.get('method') not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}:
        return {}
    path = value.get('path')
    if not isinstance(path, str) or any(token in path for token in ('?', '#', '\n', '\r', '\0', '://')):
        return {}
    try:
        contract = json.loads((ROOT / 'backend/openapi.json').read_text(encoding='utf-8'))
        routes = {(method.upper(), re.sub(r'\{[^{}]+\}', '{id}', route.removeprefix('/api/v1')))
            for route, operations in contract['paths'].items() for method in operations
            if method in {'get', 'post', 'put', 'patch', 'delete', 'head', 'options'}}
    except (OSError, ValueError, KeyError, TypeError):
        return {}
    if (value['method'], path) not in routes:
        return {}
    status = value.get('http_status')
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        return {}
    return {'method': value['method'], 'path': path, 'http_status': status}


def collect_async_failure(group: str, before: set[Path], *, root: Path = ROOT):
    """Preserve the failed cycle's closed native receipt without guessing a phase."""
    tier = {'async-volume-100': 100, 'async-volume-500': 500, 'async-volume-1024': 1024}[group]
    base = root / '.codex-local/v070'
    if (not base.is_dir() or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
                               for part in (base, base.parent, base.parent.parent))):
        return []
    observations = []
    for context in sorted(base.iterdir()):
        if (not re.fullmatch(r'trackvance-v070-test-volume-' + str(tier) + r'-[a-f0-9]{12}', context.name)
                or not context.is_dir() or context.is_symlink()
                or getattr(context, 'is_junction', lambda: False)()):
            continue
        isolation = context / 'isolation.json'
        native_directory = context / 'native-recovery'
        paths = {'isolation': isolation, 'native': native_directory / 'result.json'}
        if (native_directory.is_symlink() or getattr(native_directory, 'is_junction', lambda: False)()
                or (native_directory.exists() and not native_directory.is_dir())
                or not isolation.is_file()):
            continue
        documents = {}
        invalid = False
        for key, path in paths.items():
            if (path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
                    or path.resolve() in before or (path.exists() and (not path.is_file() or path.stat().st_size > MAX_BYTES))):
                invalid = True
                break
            if not path.exists():
                documents[key] = None
                continue
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError, RecursionError):
                invalid = True
                break
            if not isinstance(value, dict):
                invalid = True
                break
            documents[key] = value
        if invalid or not isinstance(documents.get('isolation'), dict) or documents['isolation'].get('project') != context.name:
            continue
        native = documents['native']
        if native is not None and (native.get('source_project') != context.name
                or not isinstance(native.get('target_project'), str)
                or not re.fullmatch(r'trackvance-v070-test-recovery-[a-f0-9]{12}', native['target_project'])
                or not allowed(native.get('status'), {'PASS', 'FAIL'})):
            continue
        observations.append({'kind': 'RECOVERY_PARTIAL_SUMMARY', 'profile': 'native', 'summary_present': native is not None,
                             'result': sanitize_recovery_summary(native) if native is not None else {}})
    return observations[:8]


def collect_corrections_recovery_failure(before: set[Path], *, root: Path = ROOT):
    """Read the parent's closed native sidecar from its fresh exact UUID context."""
    base = root / '.codex-local/v070'
    if (not base.is_dir() or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
                               for part in (base, base.parent, base.parent.parent))):
        return []
    observations = []
    for context in sorted(base.iterdir()):
        if (not re.fullmatch(r'trackvance-v070-test-corrections-recovery-[a-f0-9]{12}', context.name)
                or not context.is_dir() or context.is_symlink()
                or getattr(context, 'is_junction', lambda: False)()):
            continue
        isolation = context / 'isolation.json'
        path = context / 'native-recovery-diagnostic.json'
        if any(not file.is_file() or file.resolve() in before or file.is_symlink()
               or getattr(file, 'is_junction', lambda: False)() or file.stat().st_size > MAX_BYTES
               for file in (isolation, path)):
            continue
        try:
            identity = json.loads(isolation.read_text(encoding='utf-8'))
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, RecursionError):
            continue
        if (not isinstance(identity, dict) or identity.get('project') != context.name
                or not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or value.get('kind') != 'RECOVERY_PARTIAL_SUMMARY'
                or value.get('profile') != 'native' or type(value.get('summary_present')) is not bool):
            continue
        result = sanitize_recovery_summary(value.get('result'))
        if result.get('status') not in {'PASS', 'FAIL'}:
            continue
        observations.append({'kind': 'RECOVERY_PARTIAL_SUMMARY', 'profile': 'native',
                             'summary_present': value['summary_present'], 'result': result})
    return observations[:4]


def collect_recovery_failure(before: set[Path], *, root: Path = ROOT):
    """Read only new UUID-owned native/legacy result documents before wrapper cleanup."""
    observations = []
    bases = [(root / '.codex-local/recovery', False), (root / '.codex-local/v070', True)]
    for base, legacy061 in bases:
        if not base.is_dir() or base.is_symlink() or getattr(base, 'is_junction', lambda: False)():
            continue
        if any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
               for part in (base.parent, base.parent.parent)):
            continue
        for context in sorted(base.iterdir()):
            if not context.is_dir() or context.is_symlink() or getattr(context, 'is_junction', lambda: False)():
                continue
            native = re.fullmatch(r'trackvance-v070-test-recovery-src-([a-f0-9]{12})-to-'
                                  r'trackvance-v070-test-recovery-dst-\1', context.name) if not legacy061 else None
            legacy = re.fullmatch(r'legacy061-([a-f0-9]{12})' if legacy061 else
                                  r'identity-legacy-([a-f0-9]{12})', context.name)
            if not native and not legacy:
                continue
            path = context / 'result.json'
            if (not path.is_file() or path.resolve() in before or path.is_symlink()
                    or getattr(path, 'is_junction', lambda: False)() or path.stat().st_size > MAX_BYTES):
                continue
            try:
                value = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError, RecursionError):
                continue
            if not isinstance(value, dict) or value.get('status') != 'FAIL':
                continue
            if native:
                suffix, prefix, profile = native[1], 'recovery', 'native'
            else:
                version = value.get('source_version')
                if not allowed(version, {'0.6.1'} if legacy061 else {'0.5.1', '0.6.0'}):
                    continue
                suffix = legacy[1]
                profile = prefix = 'legacy' + version.replace('.', '')
            source = f'trackvance-v070-test-{prefix}-src-{suffix}'
            target = f'trackvance-v070-test-{prefix}-dst-{suffix}'
            if value.get('source_project') != source or value.get('target_project') != target:
                continue
            observations.append({'kind': 'RECOVERY_PARTIAL_SUMMARY', 'profile': profile,
                                 'result': sanitize_recovery_summary(value)})
    return observations[:8]


def sanitize_runtime_diagnostics(value):
    """Retain health/exit/OOM counters, never IDs, env, health output or logs."""
    if not isinstance(value, list):
        return []
    results = []
    for row in value[:20]:
        if not isinstance(row, dict) or not allowed(row.get('service'), RUNTIME_SERVICES):
            continue
        result = {'service': row['service']}
        for key, choices in (('state', {'created', 'restarting', 'running', 'removing', 'paused', 'exited', 'dead', 'UNKNOWN'}),
                             ('health', {'starting', 'healthy', 'unhealthy', 'UNKNOWN'})):
            if allowed(row.get(key), choices):
                result[key] = row[key]
        if type(row.get('oom_killed')) is bool:
            result['oom_killed'] = row['oom_killed']
        if type(row.get('exit_code')) is int and -255 <= row['exit_code'] <= 255:
            result['exit_code'] = row['exit_code']
        for key in ('pids_limit', 'memory_limit_bytes', 'nano_cpus'):
            if type(row.get(key)) is int and 0 <= row[key] <= 128 * 1024**3:
                result[key] = row[key]
        results.append(result)
    return results


def collect_compose_failure(group, before, *, root=ROOT, sources=None):
    """Read only the exact fresh disposable child namespace for this group."""
    folder, pattern = {
        'compose-critical': ('v070', r'trackvance-v070-test-e2e-[a-f0-9]{12}'),
        'identity-sso': ('v070', r'trackvance-v070-test-identity-[a-f0-9]{12}'),
        'connections': ('connections-e2e', r'trackvance-connections-e2e-(?:[0-9]+-)?[a-f0-9]{12}'),
    }[group]
    base = root / '.codex-local' / folder
    if (not base.is_dir() or any(part.is_symlink() or getattr(part, 'is_junction', lambda: False)()
                               for part in (base, base.parent, base.parent.parent))):
        return []
    results = []
    for context in sorted(base.iterdir()):
        if (not re.fullmatch(pattern, context.name) or not context.is_dir() or context.is_symlink()
                or getattr(context, 'is_junction', lambda: False)()):
            continue
        path = context / 'result.json'
        if (not path.is_file() or path.resolve() in before or path.is_symlink()
                or path.stat().st_size > MAX_BYTES):
            continue
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError, RecursionError):
            continue
        if not isinstance(value, dict) or value.get('status') != 'FAIL' or value.get('project') != context.name:
            continue
        summary = {'status': 'FAIL'}
        for key, choices in (('error_type', ERROR_TYPES), ('failed_stage', COMPOSE_PHASES),
                             ('compose_failure', COMPOSE_FAILURES), ('cleanup', {'PASS', 'FAIL', 'NOT_STARTED'}),
                             ('main_inventory', {'UNCHANGED', 'CHANGED_OR_UNVERIFIABLE'})):
            if allowed(value.get(key), choices):
                summary[key] = value[key]
        if type(value.get('failed_exit_code')) is int and -255 <= value['failed_exit_code'] <= 255:
            summary['failed_exit_code'] = value['failed_exit_code']
        if number(value.get('duration_seconds')):
            summary['duration_seconds'] = value['duration_seconds']
        if type(value.get('demo_seed_enabled')) is bool:
            summary['demo_seed_enabled'] = value['demo_seed_enabled']
        if group == 'connections' and value.get('failed_stage') == 'post_restart_source_check':
            http_failure = sanitize_compose_http_failure(value.get('http_failure'))
            if http_failure:
                summary['http_failure'] = http_failure
        frames = sanitize_runner_frames(value.get('failure_frames'), trusted_sources() if sources is None else sources, root)
        if frames:
            summary['failure_frames'] = frames
        runtime = sanitize_runtime_diagnostics(value.get('runtime_diagnostics'))
        runtime_path = context / 'runtime-diagnostics.json'
        if (not runtime and runtime_path.is_file() and runtime_path.resolve() not in before
                and not runtime_path.is_symlink() and runtime_path.stat().st_size <= MAX_BYTES):
            try:
                runtime = sanitize_runtime_diagnostics(json.loads(runtime_path.read_text(encoding='utf-8')))
            except (OSError, ValueError, RecursionError):
                pass
        if runtime:
            summary['runtime'] = runtime
        # The reporter already emits a safe summary; filter it again and bind
        # it to this invocation's newly created disposable project. Raw browser
        # output, test titles, messages and credential probes never survive.
        browser_path, isolation_path = context / 'browser-summary.json', context / 'isolation.json'
        if fresh_catalog_file(browser_path, before) and fresh_catalog_file(isolation_path, before):
            try:
                isolation = json.loads(isolation_path.read_text(encoding='utf-8'))
                browser = json.loads(browser_path.read_text(encoding='utf-8'))
                if (isinstance(isolation, dict) and isolation.get('project') == context.name
                        and isinstance(browser, dict) and browser.get('status') == 'FAIL'):
                    summary['browser_failure'] = sanitize_browser_failure(
                        browser, trusted_sources() if sources is None else sources)
            except (OSError, ValueError, RecursionError):
                pass
        results.append({'kind': 'COMPOSE_PARTIAL_SUMMARY', 'profile': group, 'result': summary})
    return results[:4]


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
    children = collect_child_failure(group, before, root=root, sources=sources, source_sha=source_sha, ci=ci)
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
