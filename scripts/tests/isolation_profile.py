"""Private Compose configuration and bounded resources for disposable runners."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SERVICES = ('postgres', 'api', 'worker', 'delivery-worker', 'acquisition-worker',
            'scheduler', 'events-notifications', 'events-chaining', 'web')
MAIN_PROJECT = 'trackvance-certification'
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))


def validate_project(project: str) -> str:
    current = r'trackvance-v070-test-[a-z0-9-]+-[a-f0-9]{12}'
    historical = r'trackvance-(?:e2e|identity-e2e|delivery-e2e|connections-e2e|bench|delivery-bench)-[a-z0-9-]+'
    if not re.fullmatch(f'(?:{current}|{historical})', project):
        raise ValueError('Se requiere un proyecto desechable de certificación explícito.')
    return project


def isolate_compose(compose: list[str], environment: dict[str, str], evidence: Path,
                    project: str) -> list[str]:
    """Suppress .env, pin private image tags and add limits; mutate child env only.

    The caller must prove project freshness before creating or deleting resources.
    Existing historical certification prefixes remain readable; new runs should
    use trackvance-v070-test-<suite>-<12hex>.
    """
    validate_project(project)
    if compose[:2] != ['docker', 'compose'] or '--env-file' in compose:
        raise ValueError('El perfil requiere un comando Compose sin env-file heredado.')
    positions = [index for index, value in enumerate(compose) if value in {'-p', '--project-name'}]
    if len(positions) != 1 or compose[positions[0] + 1] != project:
        raise ValueError('El proyecto Compose no coincide con el perfil privado.')
    if environment.get('WEB_PORT') == '3100':
        raise ValueError('El puerto de la instalación habitual no es desechable.')
    mock = any(Path(value).name == 'compose.identity-test.yml' for value in compose)
    for key in list(environment):
        if key.startswith('TRACKVANCE_SSO_'):
            environment.pop(key)
    environment.update(TRACKVANCE_SSO_MICROSOFT_ENABLED='false',
                       TRACKVANCE_SSO_GOOGLE_ENABLED='false', TRACKVANCE_SSO_TEST_MODE='false',
                       TRACKVANCE_SMTP_ENABLED='false',
                       POSTGRES_USER='tv_v070_test', POSTGRES_DB='tv_v070_test',
                       COMPOSE_PROJECT_NAME=project, COMPOSE_FILE='compose.yml',
                       TRACKVANCE_SPARK_MASTER='local[2]')
    if environment.get('TRACKVANCE_WEB_ORIGIN'):
        environment['TRACKVANCE_PUBLIC_URL'] = environment['TRACKVANCE_WEB_ORIGIN']
    image_prefix = ('trackvance-v070-isolated' if environment.get('TRACKVANCE_CERTIFICATION_USE_ISOLATED_IMAGES') == 'true'
                    else project)
    from ci_images import verified_images
    images = verified_images(environment)
    services = {}
    for name in (*SERVICES, *(('report-worker',) if images else ())):
        if name == 'report-worker':
            # CI reuses images, but keeps the REPORT runtime bounds from the
            # previously passing base Compose definition.
            limits = {'cpus': 2, 'mem_limit': '3g', 'pids_limit': 128}
        elif name in {'scheduler', 'events-notifications', 'events-chaining'}:
            limits = {'cpus': 0.25, 'mem_limit': '256m', 'pids_limit': 128}
        elif name == 'web':
            limits = {'cpus': 0.25, 'mem_limit': '128m', 'pids_limit': 64}
        elif name == 'worker':
            limits = {'cpus': 2, 'mem_limit': '3g', 'pids_limit': 512}
        else:
            limits = {'cpus': 1, 'mem_limit': '1g', 'pids_limit': 256}
        if name != 'postgres':
            role = "web" if name == "web" else "backend"
            limits['image'] = images[role] if images else f'{image_prefix}:{role}'
        if name not in {'postgres', 'web'}:
            limits['environment'] = {'PYTHONPATH': '/app/backend/src', 'TRACKVANCE_CERTIFICATION_PROJECT': project}
            limits['volumes'] = [{'type': 'bind', 'source': str(ROOT / relative),
                                  'target': '/app/' + relative, 'read_only': True}
                                 for relative in ('backend/src', 'backend/migrations', 'scripts')]
        services[name] = limits
    if mock:
        services['mock-oidc'] = {'cpus': 0.5, 'mem_limit': '512m', 'pids_limit': 128,
                                 'image': images['backend'] if images else f'{image_prefix}:backend'}
    evidence.mkdir(parents=True, exist_ok=True)
    empty = evidence / 'private.empty.env'
    overlay = evidence / 'private-compose.json'
    empty.write_text('', encoding='utf-8')
    overlay.write_text(json.dumps({'services': services}, indent=2), encoding='utf-8')
    scoped = [*compose[:2], '--env-file', str(empty), *compose[2:], '-f', str(overlay)]
    if environment.get('TRACKVANCE_LOCAL_EXECUTION_ID'):
        # Include every resolved connector/backup sidecar in the aggregate cap.
        # The private config can contain secrets, so publish only the limit map.
        resolved = subprocess.run([*scoped, 'config', '--format', 'json'], cwd=ROOT, env=environment,
            capture_output=True, text=True, encoding='utf-8', check=True, timeout=90)
        from ci.local_resources import apply_limits
        config = json.loads(resolved.stdout)
        for name, service in config['services'].items():
            if name not in services:
                services[name] = {key: service[key] for key in ('cpus', 'mem_limit', 'pids_limit') if key in service}
        profile = {'services': services}
        apply_limits(profile, project)
        overlay.write_text(json.dumps(profile, indent=2), encoding='utf-8')
    from ci.compose_preflight import preflight
    preflight(scoped, environment, project=project, directory=evidence, allow_mock_oidc=mock)
    return scoped


def main_inventory(run) -> list[dict]:
    identifiers = run(['docker', 'ps', '-aq', '--filter',
                       f'label=com.docker.compose.project={MAIN_PROJECT}']).split()
    if not identifiers:
        return []
    inspected = json.loads(run(['docker', 'inspect', *identifiers]))
    return sorted([{'id': row['Id'], 'image': row['Image'], 'status': row['State']['Status'],
                    'restart': row['HostConfig']['RestartPolicy']['Name'],
                    'mounts': sorted([{'type': mount['Type'], 'name': mount.get('Name'),
                                      'source': mount['Source'], 'destination': mount['Destination']}
                                     for mount in row['Mounts']], key=lambda mount: mount['destination'])}
                   for row in inspected], key=lambda row: row['id'])


def assert_main_unchanged(before: list[dict], run) -> None:
    if main_inventory(run) != before:
        raise RuntimeError('El inventario de la instalación habitual cambió durante la certificación.')


def runtime_diagnostics(project: str, run) -> list[dict]:
    """Allowlist disposable runtime facts; never retain env, logs or probe output."""
    validate_project(project)
    identifiers = run(['docker', 'ps', '-aq', '--filter',
                       f'label=com.docker.compose.project={project}']).split()
    if not identifiers:
        return []
    inspected = json.loads(run(['docker', 'inspect', *identifiers]))
    diagnostics = []
    for row in inspected:
        service = row.get('Config', {}).get('Labels', {}).get('com.docker.compose.service')
        if service not in {*SERVICES, 'mock-oidc', 'report-worker'}:
            continue
        state, limits = row.get('State', {}), row.get('HostConfig', {})
        health = state.get('Health', {})
        probes = []
        for probe in health.get('Log', [])[-3:]:
            try:
                duration = round((datetime.fromisoformat(probe['End']) -
                                  datetime.fromisoformat(probe['Start'])).total_seconds(), 3)
            except (ValueError, KeyError, TypeError):
                duration = None
            output = str(probe.get('Output', '')).lower()
            probes.append({'exit_code': probe.get('ExitCode'), 'duration_seconds': duration,
                           'timed_out': 'timed out' in output or 'timeout' in output})
        status = state.get('Status')
        health_status = health.get('Status')
        diagnostics.append({'service': service,
                            'state': status if status in {'created', 'restarting', 'running', 'removing',
                                                         'paused', 'exited', 'dead'} else 'UNKNOWN',
                            'exit_code': state.get('ExitCode'), 'oom_killed': state.get('OOMKilled') is True,
                            'pid': state.get('Pid'), 'pids_limit': limits.get('PidsLimit'),
                            'memory_limit_bytes': limits.get('Memory'), 'nano_cpus': limits.get('NanoCpus'),
                            'health': health_status if health_status in {'starting', 'healthy', 'unhealthy'} else 'UNKNOWN',
                            'probes': probes})
    return sorted(diagnostics, key=lambda row: row['service'])
