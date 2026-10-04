"""Private Compose adapters and native recovery for the 0.7.0 certification.

The existing source is a previously authorized certification context. Restoration
uses a new project with the same generated database identity, explicit env-file,
private images and read-only source mounts. Automatic consumers remain stopped.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import certification_v070
import docker_state


def guarded_project(project):
    if not re.fullmatch(r'trackvance-v070-test-[a-z0-9-]+-[a-f0-9]{12}', project):
        raise ValueError('La recuperación requiere un proyecto exclusivo de certificación 0.7.0.')
    return docker_state.validate_project(project)


def load_private_environment(path):
    values = {}
    for line in path.read_text(encoding='utf-8').splitlines():
        if line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values[key] = value
    if values.get('POSTGRES_USER') != 'tv_v070_test' or values.get('POSTGRES_DB') != 'tv_v070_test':
        raise ValueError('La identidad PostgreSQL no corresponde al entorno desechable.')
    return values


def target_override(directory, project, *, backend_image='trackvance-v070-isolated:backend', web_image='trackvance-v070-isolated:web'):
    guarded_project(project)
    for image, role in ((backend_image, 'backend'), (web_image, 'web')):
        if image != 'trackvance-v070-isolated:' + role and not re.fullmatch(
                r'trackvance-v070-test-[a-z0-9-]+-[a-f0-9]{12}:' + role, image):
            raise ValueError('La restauración sólo acepta imágenes privadas de certificación.')
    services = {}
    for name in certification_v070.SERVICES:
        if name in {'postgres', 'web'}:
            continue
        services[name] = {'build': None, 'image': backend_image, 'pids_limit': 256,
            'environment': {'PYTHONPATH': '/app/backend/src', 'TRACKVANCE_CERTIFICATION_PROJECT': project},
            'volumes': [{'type': 'bind', 'source': str(ROOT / 'backend/src'), 'target': '/app/backend/src', 'read_only': True},
                        {'type': 'bind', 'source': str(ROOT / 'backend/migrations'), 'target': '/app/backend/migrations', 'read_only': True},
                        {'type': 'bind', 'source': str(ROOT / 'scripts'), 'target': '/app/scripts', 'read_only': True}]}
    services['web'] = {'build': None, 'image': web_image, 'pids_limit': 128}
    services['postgres'] = {'pids_limit': 256}
    # Compose ignores a plain JSON null when merging build. The reset tag
    # removes it so restore --build cannot rebuild or retag private images.
    lines = ['services:']
    for name, service in services.items():
        lines.append(f'  {name}:')
        for key, value in service.items():
            rendered = '!reset null' if key == 'build' else json.dumps(value)
            lines.append(f'    {key}: {rendered}')
    path = directory / 'restore-compose.yml'
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return path


def compose_adapter(project, env_file, override, environment):
    """Run real Compose with an explicit private scope; never infer .env."""
    from docker_backup_cycle import execute

    project = guarded_project(project)
    credentials = (environment['POSTGRES_PASSWORD'],)
    def scoped(requested_project, *arguments, environment=None):
        if requested_project != project:
            raise ValueError('El adaptador de restauración recibió otro proyecto.')
        child_environment = {**os.environ, **load_private_environment(env_file), **(environment or {})}
        return execute(['docker', 'compose', '--env-file', str(env_file), '-p', project,
            '-f', str(ROOT / 'compose.yml'), '-f', str(override), *arguments], child_environment,
            credentials=credentials)
    return scoped


def container_script(container, script, environment):
    from docker_backup_cycle import execute

    return execute(['docker', 'exec', '-i', container, 'python', '-'],
        {**os.environ, **environment}, input_text=script, credentials=(environment['POSTGRES_PASSWORD'],))


MULTIPART_FIXTURE = '''
import json
import polars as pl
from sqlalchemy import select
from trackvance.artifactstore import storage_provider
from trackvance.config import DEMO_USER_ID
from trackvance.db import SessionLocal
from trackvance.models import Artifact, Dataset, User
from trackvance.services import create_version
with SessionLocal() as db:
    existing = db.scalar(select(Artifact).where(Artifact.media_type == 'application/vnd.trackvance.parquet-set+json'))
    if existing is None:
        user = db.get(User, DEMO_USER_ID)
        dataset = Dataset(organization_id=user.organization_id, name='Recovery partition fixture')
        db.add(dataset)
        db.flush()
        source = storage_provider.temporary_path('.csv')
        source.write_text('id,amount\\nA,12.25\\nB,20.50\\n', encoding='utf-8')
        version = create_version(db, dataset, source, 'recovery-parts.csv', actor=user.name)
        parts = [storage_provider.temporary_path('.parquet') for _ in range(2)]
        for ordinal, part in enumerate(parts):
            pl.DataFrame({'id': ['A' if ordinal == 0 else 'B'], 'amount': ['12.25' if ordinal == 0 else '20.50']}).write_parquet(part)
        canonical = storage_provider.put_dataset(db, parts, 'CANONICAL_PARQUET', user.organization_id, name='recovery.parquet-set.json')
        version.canonical_artifact_id, version.canonical_path = canonical.id, canonical.path
        db.commit()
    print(json.dumps({'multipart_fixture': 'PASS'}))
'''


def native_cycle(context_path, evidence_path=None):
    from docker_backup_cycle import (
        assert_no_secrets,
        available_port,
        cleanup,
        scan_backup_plaintext,
    )

    directory, context = certification_v070.load_context(context_path)
    source = guarded_project(context['project'])
    certification_v070.assert_main_unchanged(context)
    source_environment = load_private_environment(directory / 'test.env')
    state = docker_state.inventory(source)
    docker_state.require_backup_inventory(state)
    if {item['service'] for item in state['containers']} != set(certification_v070.SERVICES):
        raise ValueError('El origen no contiene exactamente los nueve servicios aislados.')
    if state['project'] != source:
        raise ValueError('El inventario fuente contiene un recurso ajeno.')
    suffix = uuid4().hex[:12]
    target = guarded_project(f'trackvance-v070-test-recovery-{suffix}')
    docker_state.ensure_fresh_project(target)
    evidence = (evidence_path or directory / 'evidence' / f'native-recovery-{suffix}').resolve()
    if not evidence.is_relative_to((ROOT / '.codex-local/v070').resolve()):
        raise ValueError('La evidencia debe permanecer dentro del contexto privado de certificación.')
    evidence.mkdir(parents=True, exist_ok=False)
    backup = evidence / 'backup'
    source_compose = json.loads((directory / 'compose.json').read_text(encoding='utf-8'))
    override = target_override(evidence, target, backend_image=context['image'],
                              web_image=source_compose['services']['web']['image'])
    target_environment = {**source_environment, 'WEB_PORT': str(available_port()),
        'DEMO_ACCESS_ENABLED': 'false', 'DEMO_SEED_ENABLED': 'false'}
    env_file = evidence / 'restore.env'
    env_file.write_text('\n'.join(f'{key}={value}' for key, value in target_environment.items()) + '\n', encoding='utf-8')
    result = {'status': 'FAIL', 'source_project': source, 'target_project': target,
              'automatic_processes_started': False}
    old_compose = docker_state.compose
    claimed = False
    stage = 'multipart_fixture'
    try:
        api = next(item for item in state['containers'] if item['service'] == 'api')
        container_script(api['id'], MULTIPART_FIXTURE, source_environment)
        stage = 'native_backup'
        docker_state.backup(source, backup)
        docker_state.verify_backup(backup)
        before = json.loads((backup / 'state.json').read_text(encoding='utf-8'))
        if before['schema_version'] != docker_state.VERIFY_SCHEMA_VERSION or before['migration'] != docker_state.CURRENT_MIGRATION or len(before['tables']) != 42:
            raise ValueError('La huella nativa no contiene el estado completo 0.7.0.')
        source_compose = ['docker', 'compose', '--env-file', str(directory / 'test.env'), '-p', source,
            '-f', str(ROOT / 'compose.yml'), '-f', str(directory / 'compose.json')]
        result['backup_privacy'] = scan_backup_plaintext(backup, source_compose,
            {**os.environ, **source_environment}, (source_environment['POSTGRES_PASSWORD'],))
        stage = 'fresh_restore'
        docker_state.compose = compose_adapter(target, env_file, override, target_environment)
        claimed = True
        receipt = docker_state.restore(backup, target, start=False, web_port=int(target_environment['WEB_PORT']))
        restored = docker_state.inventory(target)
        if any(item['running'] for item in restored['containers']):
            raise ValueError('La restauración activó un componente antes de concluir la revisión.')
        if {item['service'] for item in restored['containers']} != set(certification_v070.SERVICES):
            raise ValueError('La restauración no preservó el inventario de nueve servicios.')
        stage = 'restored_multipart'
        docker_state.compose(target, 'up', '-d', '--wait', 'api', environment=target_environment)
        api = next(item for item in docker_state.inventory(target)['containers'] if item['service'] == 'api')
        after = docker_state._copy_snapshot(api['id'], evidence / 'restored-state.json')
        if after != before:
            raise ValueError('La segunda huella restaurada no coincide exactamente.')
        result.update(status='PASS', revision=before['migration'], state_schema_version=before['schema_version'], tables=42,
            table_counts={name: len(rows) for name, rows in before['tables'].items()},
            verified_artifacts=before['verified_artifacts'], verified_source_secrets=before['verified_source_secrets'],
            verified_delivery_secrets=before['verified_delivery_secrets'],
            source_state_sha256=docker_state.canonical_hash(before), restored_state_sha256=docker_state.canonical_hash(after),
            manifest_sha256=docker_state.digest(backup / 'backup-manifest.json'),
            multipart_integrity='PASS', historical_bytes_and_hashes='PASS', exact_state_comparison='PASS',
            restore=receipt['status'], main_inventory='UNCHANGED')
        docker_state.compose(target, 'stop', environment=target_environment)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        result.update(failed_stage=stage, error_type=type(error).__name__)
    finally:
        docker_state.compose = old_compose
        if claimed:
            try:
                cleanup(target, evidence)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                result.update(status='FAIL', cleanup_error_type=type(error).__name__)
        try:
            certification_v070.assert_main_unchanged(context)
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result.update(status='FAIL', main_inventory='CHANGED_OR_UNVERIFIABLE', inventory_error_type=type(error).__name__)
        serialized = json.dumps(result, indent=2)
        assert_no_secrets(serialized, (source_environment['POSTGRES_PASSWORD'],))
        (evidence / 'result.json').write_text(serialized, encoding='utf-8')
        print(serialized)
    return 0 if result['status'] == 'PASS' else 1


def authentic_061_cycle(commit, evidence_path=None):
    """Archive an immutable 0.6.1 source and destroy it before restoring 0.7.0."""
    from docker_backup_cycle import (
        RecoveryApi,
        RecoveryCommandError,
        assert_no_secrets,
        available_port,
        cleanup,
        execute,
        scan_backup_plaintext,
    )
    from identity_legacy_restore_cycle import certify_061_credentials, health_version
    from isolation_profile import runtime_diagnostics

    if commit != '6fac26b3648cb4a4b50c094ef12c1e103bc97ddd':
        raise ValueError('La fuente debe ser el commit auténtico 0.6.1 aprobado.')
    suffix = uuid4().hex[:12]
    source = guarded_project(f'trackvance-v070-test-legacy061-src-{suffix}')
    target = guarded_project(f'trackvance-v070-test-legacy061-dst-{suffix}')
    for project in (source, target):
        docker_state.ensure_fresh_project(project)
    evidence = (evidence_path or ROOT / '.codex-local/v070' / f'legacy061-{suffix}').resolve()
    if not evidence.is_relative_to((ROOT / '.codex-local/v070').resolve()):
        raise ValueError('La evidencia debe permanecer en el directorio privado 0.7.0.')
    evidence.mkdir(parents=True, exist_ok=False)
    baseline = evidence / 'baseline'
    baseline.mkdir()
    archive, backup = evidence / 'baseline.zip', evidence / 'backup'
    source_port, target_port = available_port(), available_port()
    while target_port == source_port:
        target_port = available_port()
    environment = {'POSTGRES_USER': 'tv_v070_test', 'POSTGRES_DB': 'tv_v070_test',
        'POSTGRES_PASSWORD': secrets.token_urlsafe(36), 'DEMO_ACCESS_ENABLED': 'true', 'DEMO_SEED_ENABLED': 'true',
        'WEB_PORT': str(source_port), 'TRACKVANCE_WEB_ORIGIN': f'http://localhost:{source_port}',
        'TRACKVANCE_SSO_MICROSOFT_ENABLED': 'false', 'TRACKVANCE_SSO_GOOGLE_ENABLED': 'false',
        'TRACKVANCE_SMTP_ENABLED': 'false', 'PYTHONIOENCODING': 'utf-8'}
    env_file = evidence / 'source.env'
    env_file.write_text('\n'.join(f'{key}={value}' for key, value in environment.items()) + '\n', encoding='utf-8')
    credentials = (environment['POSTGRES_PASSWORD'],)
    source_compose = ['docker', 'compose', '--env-file', str(env_file), '-p', source, '-f', str(baseline / 'compose.yml')]
    source_profile = evidence / 'source-compose.json'
    source_services = {name: {'cpus': 1, 'mem_limit': '1g', 'pids_limit': 256}
                       for name in ('postgres', 'api', 'worker', 'delivery-worker')}
    source_services['web'] = {'cpus': 0.25, 'mem_limit': '128m', 'pids_limit': 64}
    for name in ('api', 'worker', 'delivery-worker', 'web'):
        source_services[name]['image'] = f'{source}:{"web" if name == "web" else "backend"}'
    source_profile.write_text(json.dumps({'services': source_services}, indent=2), encoding='utf-8')
    source_compose.extend(['-f', str(source_profile)])
    override = target_override(evidence, target)
    target_environment = {**environment, 'WEB_PORT': str(target_port), 'DEMO_ACCESS_ENABLED': 'false',
                          'DEMO_SEED_ENABLED': 'false', 'TRACKVANCE_WEB_ORIGIN': f'http://localhost:{target_port}'}
    target_env_file = evidence / 'restore.env'
    target_env_file.write_text('\n'.join(f'{key}={value}' for key, value in target_environment.items()) + '\n', encoding='utf-8')
    target_compose = ['docker', 'compose', '--env-file', str(target_env_file), '-p', target,
        '-f', str(ROOT / 'compose.yml'), '-f', str(override)]
    original_environment = dict(os.environ)
    os.environ.update(environment)
    old_compose = docker_state.compose
    source_claimed = target_claimed = False
    main_project = 'trackvance-certification'
    main_before = certification_v070.inventory(main_project)
    result = {'status': 'FAIL', 'source_version': '0.6.1', 'target_version': '0.7.0', 'baseline_commit': commit,
              'source_project': source, 'target_project': target, 'automatic_processes_started': False}
    stage, started = 'authentic_archive', time.monotonic()
    def run(arguments, *, input_text=None):
        return execute(arguments, {**os.environ, **environment}, input_text=input_text, credentials=credentials)
    try:
        run(['git', 'archive', '--format=zip', '--output', str(archive), commit])
        with zipfile.ZipFile(archive) as bundle:
            for entry in bundle.infolist():
                if not (baseline / entry.filename).resolve().is_relative_to(baseline.resolve()):
                    raise ValueError('La fuente archivada contiene una ruta ajena.')
            bundle.extractall(baseline)
        stage = 'authentic_source_build'
        source_claimed = True
        # Build each distinct image once before starting its services. The three
        # backend services use the same private image produced by the api build.
        run([*source_compose, 'build', 'api', 'web'])
        stage = 'authentic_source_start'
        run([*source_compose, 'up', '--no-build', '-d', '--wait', '--wait-timeout', '300'])
        if health_version(source_port) != '0.6.1':
            raise ValueError('El runtime fuente no corresponde a la revisión auténtica.')
        stage = 'historical_fixtures'
        api = RecoveryApi(source_port, credentials)
        destination = api.json('POST', '/delivery/destinations', {'name': 'Legacy encrypted destination',
            'sink_type': 'POSTGRESQL', 'host': 'postgres', 'port': 5432, 'database': 'tv_v070_test',
            'username': 'tv_v070_test', 'password': environment['POSTGRES_PASSWORD'],
            'options': {'sslmode': 'disable', 'connect_timeout': 5, 'query_timeout': 30}}, expected=201)
        connection = api.json('POST', '/connections', {'name': 'Legacy encrypted source',
            'source_type': 'POSTGRESQL', 'host': 'postgres', 'port': 5432, 'database': 'tv_v070_test',
            'username': 'tv_v070_test', 'password': environment['POSTGRES_PASSWORD'],
            'options': {'sslmode': 'disable', 'connect_timeout': 5, 'query_timeout': 30}}, expected=201)
        fixture = '''
import hashlib
import json
from datetime import timedelta
from sqlalchemy import select
from trackvance.db import SessionLocal, utcnow
from trackvance.delivery_audit import target_identity
from trackvance.models import (Configuration, Dataset, DeliveryTargetPolicy, ExternalIdentity, MonitorSchedule,
    MonitorScheduleVersion, NotificationDeliveryRecord, OIDCLoginAttempt, User)
identity = json.loads(INPUT)
with SessionLocal() as db:
    user = db.get(User, identity['user_id'])
    now = utcnow()
    db.add(NotificationDeliveryRecord(organization_id=user.organization_id, event_type='USER_CREATED', template_key='USER_CREATED',
        recipient_user_id=user.id, recipient_email_snapshot=user.email, status='FAILED', error_code='SMTP_NOT_CONFIGURED', failed_at=now))
    db.add(ExternalIdentity(organization_id=user.organization_id, user_id=user.id, provider='GOOGLE',
        issuer='https://accounts.google.com', subject='legacy061-recovery-subject', email_at_link=user.email, linked_at=now, last_login_at=now))
    db.add(OIDCLoginAttempt(state_hash=hashlib.sha256(b'legacy061-consumed-state').hexdigest(),
        browser_hash=hashlib.sha256(b'legacy061-browser').hexdigest(), provider='GOOGLE', nonce='', code_verifier='',
        expires_at=now + timedelta(minutes=10), consumed_at=now))
    fingerprint, locator = target_identity(user.organization_id, 'POSTGRESQL',
        {'host': 'postgres', 'port': 5432, 'database': 'tv_v070_test'}, {'schema_name': 'legacy_fixture', 'table_name': 'not_materialized'})
    db.add(DeliveryTargetPolicy(organization_id=user.organization_id, destination_id=identity['destination_id'],
        target_fingerprint=fingerprint, sink_type='POSTGRESQL', host_snapshot=locator['host'], port=locator['port'], database=locator['database'],
        schema_name=locator['schema_name'], table_name=locator['table_name'], audit_columns_required=True, enabled_at=now,
        enabled_by_user_id=user.id, enabled_by_username=user.username))
    dataset = db.scalar(select(Dataset).where(Dataset.organization_id == user.organization_id))
    for label, actor in [('verified', user.id), ('unresolved', 'SYSTEM')]:
        monitor = Configuration(organization_id=user.organization_id, module='sentinel', name='Legacy recovery ' + label,
            dataset_id=dataset.id, config={})
        db.add(monitor)
        db.flush()
        schedule = MonitorSchedule(organization_id=user.organization_id, monitor_id=monitor.id, version=1, enabled=True,
            next_run_at=now + timedelta(days=30))
        db.add(schedule)
        db.flush()
        db.add(MonitorScheduleVersion(organization_id=user.organization_id, schedule_id=schedule.id, version=1, interval_seconds=3600,
            enabled=True, starts_at=now + timedelta(days=30), actor_id=actor))
    db.commit()
'''.replace('INPUT', repr(json.dumps({'user_id': api.user_id, 'destination_id': destination['id']})))
        run([*source_compose, 'exec', '-T', 'api', 'python', '-'], input_text=fixture)
        stage = 'authentic_backup'
        docker_state.backup(source, backup)
        docker_state.verify_backup(backup)
        before = json.loads((backup / 'state.json').read_text(encoding='utf-8'))
        if before['schema_version'] != 5 or before['migration'] != docker_state.IDENTITY_MIGRATION or len(before['tables']) != 31:
            raise ValueError('La huella de origen no es una instalación auténtica 0.6.1.')
        result['backup_privacy'] = scan_backup_plaintext(backup, source_compose, {**os.environ, **environment}, credentials)
        stage = 'destroy_authentic_source'
        run([*source_compose, 'down', '--volumes', '--remove-orphans'])
        docker_state.ensure_fresh_project(source)
        source_claimed = False
        result['source_destroyed_before_restore'] = True
        stage = 'restore_070_stopped'
        docker_state.compose = compose_adapter(target, target_env_file, override, target_environment)
        target_claimed = True
        receipt = docker_state.restore(backup, target, start=False, web_port=target_port)
        if any(item['running'] for item in docker_state.inventory(target)['containers']):
            raise ValueError('El destino activó procesos antes de verificar la preservación.')
        docker_state.compose(target, 'up', '-d', '--wait', 'api', 'web', environment=target_environment)
        if health_version(target_port) != '0.7.0':
            raise ValueError('El destino no ejecuta 0.7.0.')
        api_container = next(item for item in docker_state.inventory(target)['containers'] if item['service'] == 'api')
        after = docker_state._copy_snapshot(api_container['id'], evidence / 'restored-state.json')
        normalized = docker_state._copy_snapshot(api_container['id'], evidence / 'legacy-state.json', command='snapshot-legacy-v5')
        if normalized != before or after['schema_version'] != docker_state.VERIFY_SCHEMA_VERSION or len(after['tables']) != 42:
            raise ValueError('La proyección restaurada no conserva exactamente la historia 0.6.1.')
        if any(after['tables'][name] for name in docker_state.ASYNC_STATE_TABLES):
            raise ValueError('La actualización produjo actividad nueva o notificaciones retroactivas.')
        result.update(exact_historical_state='PASS', schema_upgrade='0012→0016', native_tables=42, historical_tables=31,
            source_state_sha256=docker_state.canonical_hash(before), restored_legacy_sha256=docker_state.canonical_hash(normalized),
            artifacts=after['verified_artifacts'], source_secrets=after['verified_source_secrets'], delivery_secrets=after['verified_delivery_secrets'],
            historical_notifications=len(after['tables']['notification_deliveries']), no_history_replay='PASS', restore=receipt['status'])
        stage = 'current_credentials'
        active = {**target_environment, 'DEMO_ACCESS_ENABLED': 'true'}
        docker_state.compose(target, 'up', '-d', '--wait', 'api', 'web', environment=active)
        api = RecoveryApi(target_port, credentials)
        report, credentials = certify_061_credentials(api, target_compose, {**os.environ, **active}, credentials)
        result['current_credentials'] = report
        api.json('POST', f"/connections/{connection['id']}/test", {})
        api.json('POST', f"/delivery/destinations/{destination['id']}/test", {})
        result.update(status='PASS', restored_source_and_destination_credentials='PASS',
            main_inventory='UNCHANGED', historical_fixture='SYNTHETIC_METADATA_CREATED_BY_AUTHENTIC_061_ORM_NO_SMTP_OR_PROVIDER')
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        result.update(failed_stage=stage, error_type=type(error).__name__)
        if isinstance(error, RecoveryCommandError):
            result.update(error_category=error.category, command_exit_code=error.exit_code)
        try:
            result['runtime_diagnostics'] = {
                project: runtime_diagnostics(project, run) for project in (source, target)
            }
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as diagnostic_error:
            result['runtime_diagnostic_error_type'] = type(diagnostic_error).__name__
    finally:
        docker_state.compose = old_compose
        for claimed, project in ((target_claimed, target), (source_claimed, source)):
            if claimed:
                try:
                    cleanup(project, evidence)
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                    result.update(status='FAIL', cleanup_error_type=type(error).__name__)
        os.environ.clear()
        os.environ.update(original_environment)
        try:
            if certification_v070.inventory(main_project) != main_before:
                result.update(status='FAIL', main_inventory='CHANGED')
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            result.update(status='FAIL', main_inventory='UNVERIFIABLE', inventory_error_type=type(error).__name__)
        result['duration_seconds'] = round(time.monotonic() - started, 3)
        serialized = json.dumps(result, indent=2)
        assert_no_secrets(serialized, credentials)
        (evidence / 'result.json').write_text(serialized, encoding='utf-8')
        print(serialized)
    return 0 if result['status'] == 'PASS' else 1
