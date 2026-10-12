"""The existing Doctor command executes once and publishes only closed facts."""
import hashlib
import json
import sys
from types import SimpleNamespace

import delivery_cycle as delivery
import pytest
from ci import failure_diagnostics as diagnostics
from test_ci_delivery_failure_diagnostics import CI, SHA, SOURCE
from test_ci_delivery_failure_diagnostics import delivery_failure as _delivery_failure_fixture


def result(failed=False, reason='TIMEOUT'):
    checks = {label: True for label in diagnostics.DOCTOR_HEALTH_LABELS | diagnostics.DOCTOR_COMMAND_LABELS}
    commands = {label: {'reason': 'OK', 'duration_seconds': 0.1, 'timeout_seconds': 15, 'exit_code': 0}
                for label in diagnostics.DOCTOR_COMMAND_LABELS}
    if failed:
        label = 'Componente Compose scheduler'
        checks[label] = False
        commands[label] = {'reason': reason, 'duration_seconds': 15.001 if reason == 'TIMEOUT' else 0.5,
                           'timeout_seconds': 15, 'exit_code': None if reason == 'TIMEOUT' else 1}
    return {'checks': checks, 'ok': not failed, 'command_diagnostics': commands}


@pytest.fixture
def doctor_context(tmp_path, monkeypatch):
    root, directory, context, receipt = _delivery_failure_fixture.__wrapped__(tmp_path, monkeypatch)
    source = root / 'scripts/doctor.py'
    source.write_text('def main():\n    return 0\n')
    return root, directory, context, receipt


@pytest.mark.parametrize('failed', [False, True])
def test_doctor_single_original_invocation_retains_exit_and_safe_counters(doctor_context, monkeypatch, failed):
    root, directory, context, _receipt = doctor_context
    calls = []
    output = json.dumps(result(failed))
    def run(arguments, **kwargs):
        calls.append(arguments)
        return SimpleNamespace(returncode=int(failed), stdout=output, stderr='SECRET_TOKEN' if failed else '')
    monkeypatch.setattr(delivery.subprocess, 'run', run)
    environment = {'TV_E2E_URL': 'http://127.0.0.1:32071'}
    argv = [sys.executable, 'scripts/doctor.py', '--base-url', environment['TV_E2E_URL'], '--docker', '--project', directory.name, '--json']
    if failed:
        with pytest.raises(RuntimeError):
            delivery.execute(argv, environment=environment, credentials=['SECRET_TOKEN'], capture=True, doctor_context=context)
    else:
        assert delivery.execute(argv, environment=environment, credentials=['SECRET_TOKEN'], capture=True, doctor_context=context) == output
    assert calls == [argv]
    path = directory / 'delivery-doctor-safe.json'
    saved = json.loads(path.read_text())
    assert saved['exit_code'] == int(failed) and saved['result'] == result(failed)
    assert 'SECRET_TOKEN' not in path.read_text()
    observed = diagnostics.collect_child_failure('delivery', set(), root=root, sources={SOURCE, 'scripts/doctor.py'}, source_sha=SHA, ci=CI)
    assert observed[0]['doctor']['exit_code'] == int(failed) and observed[0]['doctor']['ok'] is (not failed)


@pytest.mark.parametrize('damage', ['secret', 'path', 'hash', 'context-hash', 'stale-doctor', 'invalid-json', 'reason', 'exit', 'timeout', 'huge-duration', 'run', 'attempt'])
def test_invalid_doctor_details_are_blocked_without_suppressing_original_frames(doctor_context, damage):
    root, directory, context, receipt = doctor_context
    diagnostics.write_delivery_doctor_result(json.dumps(result(True)), 1, context)
    path = directory / 'delivery-doctor-safe.json';value = json.loads(path.read_text());before = set()
    if damage == 'secret':value['result']['message'] = 'SECRET_TOKEN'
    elif damage == 'path':value['source_file'] = '../SECRET_TOKEN.py'
    elif damage == 'hash':value['source_sha256'] = '0' * 64
    elif damage == 'context-hash':value['context_sha256'] = '0' * 64
    elif damage == 'stale-doctor':before.add(path.resolve())
    elif damage == 'reason':value['result']['command_diagnostics']['Componente Compose scheduler']['reason'] = 'SECRET_TOKEN'
    elif damage == 'exit':value['exit_code'] = 0
    elif damage == 'timeout':value['result']['command_diagnostics']['Componente Compose scheduler']['timeout_seconds'] = 300
    elif damage == 'huge-duration':value['result']['command_diagnostics']['Componente Compose scheduler']['duration_seconds'] = 10**400
    elif damage in {'run', 'attempt'}:
        identity = json.loads(context.read_text());identity['ci']['run_id' if damage == 'run' else 'run_attempt'] = '999'
        context.write_text(json.dumps(identity))
        new_hash = hashlib.sha256(context.read_bytes()).hexdigest()
        original = json.loads(receipt.read_text());original['context_sha256'] = new_hash;receipt.write_text(json.dumps(original));value['context_sha256'] = new_hash
    path.write_text('{invalid' if damage == 'invalid-json' else json.dumps(value))
    observed = diagnostics.collect_child_failure('delivery', before, root=root, sources={SOURCE, 'scripts/doctor.py'}, source_sha=SHA, ci=CI)
    if damage in {'run', 'attempt'}:
        assert observed == []
    else:
        assert len(observed) == 1 and observed[0]['frames'] and 'doctor' not in observed[0]
        assert 'SECRET_TOKEN' not in json.dumps(observed)
