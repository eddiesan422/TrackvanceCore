"""Recovery adapters reject ambient configuration and unrelated Docker scopes."""

import json
import os
import subprocess
import sys
import zipfile
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import docker_backup_cycle
import v070_recovery as recovery


@pytest.fixture
def private_adapter(tmp_path, monkeypatch):
    from ci import compose_preflight

    project = 'trackvance-v070-test-recovery-0123456789ab'
    directory = tmp_path / '.codex-local/v070' / project
    directory.mkdir(parents=True)
    password, protected = 'synthetic-recovery-password', 'protected-main-password'
    sha = 'a' * 40
    images = {'backend': 'sha256:' + 'b' * 64, 'web': 'sha256:' + 'c' * 64}
    env_file, override = directory / 'restore.env', directory / 'restore-compose.yml'
    env_file.write_text('POSTGRES_USER=tv_v070_test\nPOSTGRES_DB=tv_v070_test\nPOSTGRES_PASSWORD=' + password + '\n')
    override.write_text('services: {}\n')
    manifest = directory / 'images.json'
    manifest.write_text(json.dumps({'schema_version': 1, 'status': 'PASS', 'source_sha': sha,
        'digest_kind': 'DOCKER_CONFIGURATION_SHA256', 'images': {
            role: {'image_id': image, 'archive': role + '.tar', 'archive_bytes': 1,
                   'archive_sha256': 'd' * 64} for role, image in images.items()}}))
    monkeypatch.setenv('TRACKVANCE_CI_IMAGE_MANIFEST', str(manifest))
    monkeypatch.setenv('CI_SOURCE_SHA', sha)
    monkeypatch.setattr(recovery, 'ROOT', tmp_path)
    monkeypatch.setattr(recovery.docker_state, 'ROOT', tmp_path)
    monkeypatch.setattr(compose_preflight, 'ROOT', tmp_path)
    services = {}
    for name in recovery.RESTORED_SERVICES:
        environment = {'TRACKVANCE_CERTIFICATION_PROJECT': project}
        if name == 'postgres':
            environment = {'POSTGRES_USER': 'tv_v070_test', 'POSTGRES_DB': 'tv_v070_test', 'POSTGRES_PASSWORD': password}
        services[name] = {'cpus': 1, 'mem_limit': 268435456, 'pids_limit': 128, 'restart': 'no',
            'environment': environment, 'networks': {'default': {}},
            'image': 'postgres:16-alpine' if name == 'postgres' else images['web' if name == 'web' else 'backend']}
    config = {'name': project, 'services': services, 'volumes': {'data': {'name': project + '_data'}},
              'networks': {'default': {'name': project + '_default'}}}
    fixture = {'project': project, 'directory': directory, 'password': password, 'protected': protected,
               'config': config, 'calls': [], 'config_exit': 0, 'config_stderr': password, 'ordinary_output': 'started',
               'env_file': env_file, 'override': override}

    def run(arguments, **kwargs):
        fixture['calls'].append(arguments)
        assert kwargs['capture_output'] is True
        if arguments[:2] == ['docker', 'compose']:
            if arguments[-3:] == ['config', '--format', 'json']:
                return subprocess.CompletedProcess(arguments, fixture['config_exit'],
                    json.dumps(config), fixture['config_stderr'])
            return subprocess.CompletedProcess(arguments, 0, fixture['ordinary_output'], '')
        if arguments == ['git', 'rev-parse', 'HEAD']:
            assert kwargs['cwd'] == tmp_path
            return subprocess.CompletedProcess(arguments, 0, sha, '')
        if arguments[:3] == ['docker', 'image', 'inspect']:
            assert arguments[3] in images.values()
            return subprocess.CompletedProcess(arguments, 0, json.dumps([{'Id': arguments[3], 'Os': 'linux',
                'Architecture': 'amd64', 'Config': {'Labels': {'org.opencontainers.image.revision': sha,
                    'org.opencontainers.image.version': '0.8.0'}}}]), '')
        if arguments[:2] == ['docker', 'ps']:
            output = 'e' * 12 if arguments[-1] == 'label=com.docker.compose.project=trackvance-certification' else ''
            return subprocess.CompletedProcess(arguments, 0, output, '')
        if arguments[:2] == ['docker', 'inspect']:
            assert arguments[2:] == ['e' * 12]
            return subprocess.CompletedProcess(arguments, 0, json.dumps([{'Config': {
                'Env': ['POSTGRES_PASSWORD=' + protected]}}]), protected)
        assert arguments[:2] in (['docker', 'volume'], ['docker', 'network']) and arguments[2] == 'ls'
        return subprocess.CompletedProcess(arguments, 0, '', '')

    monkeypatch.setattr(recovery.subprocess, 'run', run)
    fixture['scoped'] = recovery.compose_adapter(project, env_file, override, recovery.load_private_environment(env_file))
    return fixture


def test_resolved_config_and_default_preflight_keep_sensitive_outputs_private(private_adapter, monkeypatch, capsys):
    fixture = private_adapter
    scoped, project = fixture['scoped'], fixture['project']
    resolved = json.loads(scoped(project, 'config', '--format', 'json'))
    assert resolved['services']['postgres']['environment']['POSTGRES_PASSWORD'] == fixture['password']
    monkeypatch.setattr(recovery.docker_state, 'compose', scoped)
    assert recovery.docker_state._restore_creation_options(
        project, fixture['directory'], list(resolved['services']), {}
    ) == ['--no-build', '--pull', 'never']
    assert scoped(project, 'up', '-d', '--wait', 'api') == 'started'
    assert any(command[:2] == ['docker', 'inspect'] for command in fixture['calls'])
    assert sum(command[-3:] == ['config', '--format', 'json'] for command in fixture['calls']) == 3
    output = capsys.readouterr()
    assert all(secret not in output.out + output.err for secret in (fixture['password'], fixture['protected']))
    assert '"services"' not in output.out + output.err


@pytest.mark.parametrize('damage', ['failed_config', 'timeout'])
def test_private_config_failure_uses_closed_error_without_sensitive_outputs(private_adapter, monkeypatch, capsys, damage):
    from ci.compose_preflight import ComposePreflightError

    fixture = private_adapter
    if damage == 'failed_config':
        fixture['config_exit'] = 1
    else:
        def timeout(arguments, **_kwargs):
            raise subprocess.TimeoutExpired(arguments, 60, output=fixture['password'], stderr=fixture['protected'])
        monkeypatch.setattr(recovery.subprocess, 'run', timeout)
    with pytest.raises(ComposePreflightError) as caught:
        fixture['scoped'](fixture['project'], 'config', '--format', 'json')
    assert caught.value.code == 'READ_ONLY_DOCKER_COMMAND_FAILED'
    output = capsys.readouterr()
    assert all(secret not in output.out + output.err + str(caught.value) for secret in (fixture['password'], fixture['protected']))


@pytest.mark.parametrize('damage', ['inherited_secret', 'host_network', 'failed_config'])
def test_default_preflight_rejects_unsafe_config_before_up_without_leaking(private_adapter, capsys, damage):
    from ci.compose_preflight import ComposePreflightError

    fixture = private_adapter
    if damage == 'inherited_secret':
        fixture['config']['services']['api']['environment']['RENAMED_CREDENTIAL'] = fixture['protected']
        code = 'LIVE_CREDENTIAL_INHERITANCE'
    elif damage == 'host_network':
        fixture['config']['services']['api']['network_mode'] = 'host'
        code = 'HOST_NAMESPACE'
    else:
        fixture['config_exit'] = 1
        code = 'READ_ONLY_DOCKER_COMMAND_FAILED'
    with pytest.raises(ComposePreflightError) as caught:
        fixture['scoped'](fixture['project'], 'up', 'api')
    assert caught.value.code == code
    assert not any('up' in command for command in fixture['calls'])
    output = capsys.readouterr()
    assert all(secret not in output.out + output.err + str(caught.value) for secret in (fixture['password'], fixture['protected']))


@pytest.mark.parametrize('arguments', [('logs',), ('config', '--format', 'yaml'), ('config', '--format', 'json', '--environment')])
def test_only_exact_json_config_uses_private_capture_other_commands_keep_scans(private_adapter, capsys, arguments):
    fixture = private_adapter
    fixture['ordinary_output'] = fixture['password']
    with pytest.raises(RuntimeError, match='credencial'):
        fixture['scoped'](fixture['project'], *arguments)
    output = capsys.readouterr()
    assert fixture['password'] not in output.out + output.err


def test_private_config_never_executes_argv_containing_a_credential(private_adapter, capsys):
    fixture = private_adapter
    with pytest.raises(RuntimeError, match='credencial'):
        fixture['scoped'](fixture['project'], 'logs', fixture['password'])
    assert not fixture['calls']
    output = capsys.readouterr()
    assert fixture['password'] not in output.out + output.err


def test_exact_private_config_rejects_a_credential_in_its_env_file_path(private_adapter, capsys):
    fixture = private_adapter
    path = fixture['directory'] / (fixture['password'] + '.env')
    path.write_text(fixture['env_file'].read_text())
    scoped = recovery.compose_adapter(fixture['project'], path, fixture['override'], recovery.load_private_environment(path))
    with pytest.raises(RuntimeError, match='credencial'):
        scoped(fixture['project'], 'config', '--format', 'json')
    assert not fixture['calls']
    output = capsys.readouterr()
    assert fixture['password'] not in output.out + output.err


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


def test_ci_restore_accepts_only_verified_role_digest(tmp_path, monkeypatch):
    import ci_images

    images = {'backend': 'sha256:' + 'a' * 64, 'web': 'sha256:' + 'b' * 64}
    monkeypatch.setattr(ci_images, 'verified_images', lambda: images)
    project = 'trackvance-v070-test-recovery-0123456789ab'
    path = recovery.target_override(tmp_path, project,
        backend_image=images['backend'], web_image=images['web'])
    services = yaml.safe_load(path.read_text().replace('!reset null', 'null'))['services']
    assert services['api']['image'] == images['backend']
    assert services['web']['image'] == images['web']
    for rejected in (images['web'], 'sha256:' + 'c' * 64, 'trackvance-core:backend'):
        with pytest.raises(ValueError, match='imágenes privadas'):
            recovery.target_override(tmp_path, project, backend_image=rejected, web_image=images['web'])
