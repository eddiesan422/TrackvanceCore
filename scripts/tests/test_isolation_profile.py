"""Disposable profiles cannot inherit live SSO or image/runtime identities."""

import json

import pytest
from isolation_profile import assert_main_unchanged, isolate_compose, main_inventory


@pytest.mark.parametrize('project', ['trackvance-core', 'trackvance-certification', 'trackvance-v070-test-e2e'])
def test_profile_refuses_uncontrolled_projects(project, tmp_path):
    with pytest.raises(ValueError, match='desechable'):
        isolate_compose(['docker', 'compose', '-p', project], {}, tmp_path, project)


def test_profile_masks_live_sso_and_namespaces_every_app_image(tmp_path):
    project = 'trackvance-v070-test-e2e-0123456789ab'
    environment = {'WEB_PORT': '32071', 'TRACKVANCE_SSO_GOOGLE_ENABLED': 'true',
                   'TRACKVANCE_SSO_GOOGLE_CLIENT_SECRET': 'unrelated-live-secret',
                   'TRACKVANCE_SPARK_MASTER': 'spark://live-cluster:7077'}
    scoped = isolate_compose(['docker', 'compose', '-p', project, '-f', 'compose.yml'], environment, tmp_path, project)
    assert scoped[2:4] == ['--env-file', str(tmp_path / 'private.empty.env')]
    assert (tmp_path / 'private.empty.env').read_text() == ''
    assert environment['TRACKVANCE_SSO_GOOGLE_ENABLED'] == 'false'
    assert 'TRACKVANCE_SSO_GOOGLE_CLIENT_SECRET' not in environment
    assert environment['POSTGRES_USER'] == environment['POSTGRES_DB'] == 'tv_v070_test'
    assert environment['TRACKVANCE_SPARK_MASTER'] == 'local[2]'
    services = json.loads((tmp_path / 'private-compose.json').read_text())['services']
    assert len(services) == 9
    for name, settings in services.items():
        assert settings['cpus'] > 0 and settings['mem_limit'] and settings['pids_limit'] > 0
        if name != 'postgres':
            assert settings['image'].startswith(project + ':')
        if name not in {'postgres', 'web'}:
            assert len(settings['volumes']) == 3 and all(item['read_only'] for item in settings['volumes'])
    assert 'mock-oidc' not in services and 'unrelated-live-secret' not in json.dumps(services)


def test_explicit_mock_is_bounded_and_preserves_only_the_test_overlay(tmp_path):
    project = 'trackvance-v070-test-identity-0123456789ab'
    environment = {'WEB_PORT': '32072'}
    compose = ['docker', 'compose', '-p', project, '-f', 'compose.yml', '-f', 'deploy/docker/compose.identity-test.yml']
    scoped = isolate_compose(compose, environment, tmp_path, project)
    services = json.loads((tmp_path / 'private-compose.json').read_text())['services']
    assert services['mock-oidc']['image'] == project + ':backend'
    assert services['mock-oidc']['mem_limit'] == '512m'
    assert 'TRACKVANCE_SSO_GOOGLE_ENABLED' not in services['api']['environment']
    assert scoped.index('deploy/docker/compose.identity-test.yml') < scoped.index(str(tmp_path / 'private-compose.json'))


def test_inventory_comparison_fails_when_main_changes():
    assert main_inventory(lambda _arguments: '') == []
    calls = iter(['main-id', json.dumps([{'Id': 'main-id', 'Image': 'immutable',
        'State': {'Status': 'exited'}, 'HostConfig': {'RestartPolicy': {'Name': 'no'}}, 'Mounts': []}])])
    with pytest.raises(RuntimeError, match='inventario'):
        assert_main_unchanged([], lambda _arguments: next(calls))
