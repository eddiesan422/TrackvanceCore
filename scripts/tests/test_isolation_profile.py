"""Disposable profiles cannot inherit live SSO or image/runtime identities."""

import json
import re
from pathlib import Path

import pytest
from isolation_profile import (
    assert_main_unchanged,
    isolate_compose,
    main_inventory,
    runtime_diagnostics,
)


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


def test_ci_report_bounds_match_base_compose_and_original_nine_services_are_unchanged(tmp_path, monkeypatch):
    import ci_images
    from ci import compose_preflight
    images = {'backend': 'sha256:' + 'a' * 64, 'web': 'sha256:' + 'b' * 64}
    project = 'trackvance-v070-test-e2e-0123456789ab'
    environment = {'WEB_PORT': '32072', 'TRACKVANCE_CI_IMAGE_MANIFEST': 'synthetic-manifest.json'}
    calls = []
    monkeypatch.setattr(ci_images, 'verified_images', lambda values: images if values is environment else pytest.fail('Wrong env'))
    monkeypatch.setattr(compose_preflight, 'preflight', lambda *args, **kwargs: calls.append((args, kwargs)))
    scoped = isolate_compose(['docker', 'compose', '-p', project, '-f', 'compose.yml'], environment, tmp_path, project)
    services = json.loads((tmp_path / 'private-compose.json').read_text())['services']
    report = services['report-worker']
    base = (Path(__file__).resolve().parents[2] / 'compose.yml').read_text(encoding='utf-8')
    definition = re.search(r'^  report-worker:\n(.*?)(?=^  [a-z]|\Z)', base, re.MULTILINE | re.DOTALL)[1]
    assert report['cpus'] == int(re.search(r'^    cpus: (\d+)$', definition, re.MULTILINE)[1]) == 2
    assert report['mem_limit'] == re.search(r'^    mem_limit: (\S+)$', definition, re.MULTILINE)[1] == '3g'
    assert report['pids_limit'] == int(re.search(r'^    pids_limit: (\d+)$', definition, re.MULTILINE)[1]) == 128
    assert report['image'] == images['backend']
    assert report['environment']['TRACKVANCE_CERTIFICATION_PROJECT'] == project
    assert len(report['volumes']) == 3 and all(mount['read_only'] for mount in report['volumes'])
    expected = {'postgres': (1, '1g', 256), 'api': (1, '1g', 256), 'worker': (2, '3g', 512),
                'acquisition-worker': (1, '1g', 256), 'delivery-worker': (1, '1g', 256),
                'scheduler': (.25, '256m', 128), 'events-notifications': (.25, '256m', 128),
                'events-chaining': (.25, '256m', 128), 'web': (.25, '128m', 64)}
    assert {name: (value['cpus'], value['mem_limit'], value['pids_limit'])
            for name, value in services.items() if name != 'report-worker'} == expected
    assert len(calls) == 1 and calls[0][0] == (scoped, environment)
    assert calls[0][1] == {'project': project, 'directory': tmp_path, 'allow_mock_oidc': False}


def test_inventory_comparison_fails_when_main_changes():
    assert main_inventory(lambda _arguments: '') == []
    calls = iter(['main-id', json.dumps([{'Id': 'main-id', 'Image': 'immutable',
        'State': {'Status': 'exited'}, 'HostConfig': {'RestartPolicy': {'Name': 'no'}}, 'Mounts': []}])])
    with pytest.raises(RuntimeError, match='inventario'):
        assert_main_unchanged([], lambda _arguments: next(calls))


def test_runtime_diagnostics_retains_probe_facts_and_discards_credentials():
    project = 'trackvance-v070-test-identity-0123456789ab'
    secret = 'private-secret-must-never-be-emitted'
    inspected = [{'Config': {'Env': ['PASSWORD=' + secret], 'Labels': {
        'com.docker.compose.service': 'scheduler', 'untrusted': secret}},
        'State': {'Status': 'running', 'ExitCode': 0, 'OOMKilled': False, 'Pid': 321,
                  'Health': {'Status': 'unhealthy', 'Log': [{
                      'Start': '2026-10-03T00:00:00Z', 'End': '2026-10-03T00:00:05Z',
                      'ExitCode': -1, 'Output': 'Health check exceeded timeout: ' + secret}]}},
        'HostConfig': {'Memory': 268435456, 'PidsLimit': 128, 'NanoCpus': 250000000}}]
    calls = iter(['scheduler-id', json.dumps(inspected)])
    result = runtime_diagnostics(project, lambda _arguments: next(calls))
    assert secret not in json.dumps(result)
    assert result[0]['probes'] == [{'exit_code': -1, 'duration_seconds': 5.0, 'timed_out': True}]
    assert result[0]['pids_limit'] == 128
    assert result[0]['nano_cpus'] == 250000000
    assert result[0]['oom_killed'] is False


def test_runtime_report_health_is_observable_without_environment_or_probe_output():
    project = 'trackvance-v070-test-e2e-0123456789ab'
    row = {'Config': {'Labels': {'com.docker.compose.service': 'report-worker'}, 'Env': ['PASSWORD=SECRET_TOKEN']},
           'State': {'Status': 'running', 'ExitCode': 0, 'OOMKilled': False, 'Pid': 321,
               'Health': {'Status': 'healthy', 'Log': [{'Start': '2026-10-03T00:00:00Z',
                   'End': '2026-10-03T00:00:03Z', 'ExitCode': 0, 'Output': 'SECRET_TOKEN PRIVATE_ROW'}]}},
           'HostConfig': {'Memory': 3 * 1024**3, 'PidsLimit': 128, 'NanoCpus': 2000000000}}
    calls = iter(['own-report-id', json.dumps([row])])
    result = runtime_diagnostics(project, lambda _arguments: next(calls))
    assert len(result) == 1 and result[0]['service'] == 'report-worker'
    assert result[0]['health'] == 'healthy' and result[0]['memory_limit_bytes'] == 3 * 1024**3
    assert result[0]['probes'] == [{'exit_code': 0, 'duration_seconds': 3.0, 'timed_out': False}]
    assert all(secret not in json.dumps(result) for secret in ('SECRET_TOKEN', 'PRIVATE_ROW', 'Env', 'Output'))


def test_runtime_diagnostics_refuses_main_and_unknown_services():
    with pytest.raises(ValueError, match='desechable'):
        runtime_diagnostics('trackvance-certification', lambda _arguments: '')
    calls = iter(['unknown-id', json.dumps([{'Config': {'Labels': {'com.docker.compose.service': 'secret'}}}])])
    assert runtime_diagnostics('trackvance-v070-test-e2e-0123456789ab', lambda _arguments: next(calls)) == []
