"""Recovery adapters reject ambient configuration and unrelated Docker scopes."""

import json
import os
import zipfile
from copy import deepcopy

import docker_backup_cycle
import pytest
import v070_recovery as recovery
import yaml


@pytest.mark.parametrize('failed_stage', ['authentic_source_build', 'authentic_source_start'])
def test_authentic_failure_retains_safe_diagnostics_and_builds_shared_images_once(tmp_path, monkeypatch, failed_stage):
    import identity_legacy_restore_cycle
    import isolation_profile

    monkeypatch.setattr(recovery, 'ROOT', tmp_path)
    monkeypatch.setattr(recovery.os, 'environ', dict(os.environ))
    monkeypatch.setattr(recovery.docker_state, 'ensure_fresh_project', lambda _: None)
    monkeypatch.setattr(recovery.certification_v070, 'inventory', lambda _: [])
    monkeypatch.setattr(docker_backup_cycle, 'available_port', iter([32001, 32002]).__next__)
    calls, cleaned = [], []
    monkeypatch.setattr(docker_backup_cycle, 'cleanup', lambda project, _: cleaned.append(project))
    monkeypatch.setattr(isolation_profile, 'runtime_diagnostics', lambda project, _: [{'service': 'api', 'oom_killed': False}])
    monkeypatch.setattr(identity_legacy_restore_cycle, 'health_version', lambda _: pytest.fail('Failure must precede readiness'))

    def run(arguments, environment, **_kwargs):
        calls.append(arguments)
        if arguments[:2] == ['git', 'archive']:
            with zipfile.ZipFile(arguments[arguments.index('--output') + 1], 'w') as bundle:
                bundle.writestr('compose.yml', 'services: {}')
        elif 'build' in arguments and failed_stage == 'authentic_source_build':
            raise docker_backup_cycle.RecoveryCommandError(2, 'BUILD')
        elif 'up' in arguments:
            raise docker_backup_cycle.RecoveryCommandError(1, 'UNHEALTHY')
        return ''

    monkeypatch.setattr(docker_backup_cycle, 'execute', run)
    evidence = tmp_path / '.codex-local/v070/authentic-test'
    assert recovery.authentic_061_cycle('6fac26b3648cb4a4b50c094ef12c1e103bc97ddd', evidence) == 1
    result = json.loads((evidence / 'result.json').read_text())
    assert result['failed_stage'] == failed_stage
    assert result['error_category'] == ('BUILD' if failed_stage.endswith('build') else 'UNHEALTHY')
    assert len(result['runtime_diagnostics']) == 2 and cleaned == [result['source_project']]
    build = next(arguments for arguments in calls if 'build' in arguments)
    assert build[-3:] == ['build', 'api', 'web']
    if failed_stage.endswith('start'):
        start = next(arguments for arguments in calls if 'up' in arguments)
        assert '--no-build' in start and '--build' not in start


@pytest.mark.parametrize('project', ['trackvance-core', 'trackvance-certification', 'trackvance-v070-test-core', 'trackvance-v070-test-core-nothex'])
def test_recovery_requires_full_disposable_identity(project):
    with pytest.raises(ValueError, match='exclusivo'):
        recovery.guarded_project(project)


def test_private_env_refuses_production_database_identity(tmp_path):
    path = tmp_path / 'test.env'
    path.write_text('POSTGRES_USER=trackvance\nPOSTGRES_DB=trackvance\nPOSTGRES_PASSWORD=private\n')
    with pytest.raises(ValueError, match='desechable'):
        recovery.load_private_environment(path)


def test_restored_services_use_only_private_images_and_read_only_working_source(tmp_path):
    path = recovery.target_override(tmp_path, 'trackvance-v070-test-recovery-0123456789ab')
    source = path.read_text()
    assert 'build: !reset null' in source
    services = yaml.safe_load(source.replace('!reset null', 'null'))['services']
    assert set(services) == set(recovery.RESTORED_SERVICES)
    assert len(services) == 10
    assert all(service.get('build') is None for service in services.values())
    for name, service in services.items():
        if name != 'postgres':
            assert service['image'].startswith('trackvance-v070-isolated:')
        assert all(mount['read_only'] for mount in service.get('volumes', []))
    assert services['report-worker']['mem_limit'] == '256m'
    assert services['report-worker']['cpus'] == 0.5
    assert services['report-worker']['pids_limit'] == 128


def current_snapshot():
    return {'schema_version': recovery.docker_state.VERIFY_SCHEMA_VERSION,
            'migration': recovery.docker_state.CURRENT_MIGRATION,
            'tables': {name: {} for name in recovery.docker_state.CURRENT_STATE_TABLES}}


@pytest.mark.parametrize('damage', ['old_schema', 'old_migration', 'missing', 'unknown'])
def test_native_snapshot_rejects_stale_or_incomplete_current_inventory(damage):
    snapshot = current_snapshot()
    if damage == 'old_schema':
        snapshot['schema_version'] = 7
    elif damage == 'old_migration':
        snapshot['migration'] = '0016_acquisition_diagnostics'
    elif damage == 'missing':
        snapshot['tables'].pop('report_definitions')
    else:
        snapshot['tables']['unknown_table'] = {}
    with pytest.raises(ValueError, match='estado completo'):
        recovery.require_current_snapshot(snapshot)


def test_restored_061_keeps_exact_history_and_rejects_new_activity_or_classification():
    before = {'schema_version': 5, 'tables': {'users': {'legacy-user': 'hash'}}}
    after = current_snapshot()
    recovery.require_preserved_061_history(before, after, deepcopy(before))
    assert len(after['tables']) == 55
    changed = deepcopy(before)
    changed['tables']['users']['legacy-user'] = 'different-hash'
    with pytest.raises(ValueError, match='exactamente'):
        recovery.require_preserved_061_history(before, after, changed)
    for table in ('acquisition_runs', 'strict_approvals', 'macro_domains', 'report_contexts'):
        damaged = deepcopy(after)
        damaged['tables'][table]['invented-row'] = 'hash'
        with pytest.raises(ValueError, match='actividad nueva'):
            recovery.require_preserved_061_history(before, damaged, before)


def test_native_restore_reuses_source_uuid_images_and_rejects_main_alias(tmp_path):
    prefix = 'trackvance-v070-test-corrections-0123456789ab'
    path = recovery.target_override(tmp_path, 'trackvance-v070-test-recovery-abcdef012345',
        backend_image=prefix + ':backend', web_image=prefix + ':web')
    services = yaml.safe_load(path.read_text().replace('!reset null', 'null'))['services']
    assert services['api']['image'] == prefix + ':backend'
    assert services['web']['image'] == prefix + ':web'
    with pytest.raises(ValueError, match='imágenes privadas'):
        recovery.target_override(tmp_path, 'trackvance-v070-test-recovery-abcdef012345',
                                backend_image='trackvance-core:backend')


def test_compose_adapter_uses_explicit_env_file_and_rejects_other_project(tmp_path, monkeypatch):
    project = 'trackvance-v070-test-recovery-0123456789ab'
    env_file = tmp_path / 'test.env'
    env_file.write_text('POSTGRES_USER=tv_v070_test\nPOSTGRES_DB=tv_v070_test\nPOSTGRES_PASSWORD=private-test-secret\n')
    override = recovery.target_override(tmp_path, project)
    calls = []
    monkeypatch.setattr(docker_backup_cycle, 'execute', lambda arguments, environment, **_kwargs: calls.append((arguments, environment)) or '')
    scoped = recovery.compose_adapter(project, env_file, override, recovery.load_private_environment(env_file))
    with pytest.raises(ValueError, match='otro proyecto'):
        scoped('trackvance-certification', 'stop')
    assert not calls
    scoped(project, 'up', 'api', environment={'DEMO_ACCESS_ENABLED': 'false'})
    arguments, environment = calls[0]
    assert arguments[:6] == ['docker', 'compose', '--env-file', str(env_file), '-p', project]
    assert str(override) in arguments and 'private-test-secret' not in ' '.join(arguments)
    assert environment['POSTGRES_PASSWORD'] == 'private-test-secret'
    assert environment['DEMO_ACCESS_ENABLED'] == 'false'
