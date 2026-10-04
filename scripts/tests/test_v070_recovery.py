"""Recovery adapters reject ambient configuration and unrelated Docker scopes."""

import docker_backup_cycle
import pytest
import v070_recovery as recovery
import yaml


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
    assert len(services) == 9
    assert all(service.get('build') is None for service in services.values())
    for name, service in services.items():
        if name != 'postgres':
            assert service['image'].startswith('trackvance-v070-isolated:')
        assert all(mount['read_only'] for mount in service.get('volumes', []))


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
