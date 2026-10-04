"""Persistence probes must work when Compose replaces the API container."""
import json
from unittest.mock import Mock

import identity_sso_cycle
import pytest


def test_help_is_read_only_and_unknown_arguments_cannot_start_docker(monkeypatch, capsys):
    command = Mock(side_effect=AssertionError('Docker must not run while parsing arguments'))
    monkeypatch.setattr(identity_sso_cycle.subprocess, 'run', command)
    with pytest.raises(SystemExit) as help_exit:
        identity_sso_cycle.main(['--help'])
    assert help_exit.value.code == 0
    assert 'Certify identity' in capsys.readouterr().out
    with pytest.raises(SystemExit) as invalid_exit:
        identity_sso_cycle.main(['--unexpected-option'])
    assert invalid_exit.value.code == 2
    command.assert_not_called()


@pytest.mark.parametrize('project', ['trackvance-certification', 'trackvance-core', 'trackvance-v070-test-identity-nohex'])
def test_identity_runner_rejects_main_or_incomplete_disposable_names(project):
    with pytest.raises(ValueError, match='aislado'):
        identity_sso_cycle.validated_project(project)


def test_identity_runner_accepts_only_current_full_disposable_identity():
    project = 'trackvance-v070-test-identity-0123456789ab'
    assert identity_sso_cycle.validated_project(project) == project


def test_storage_verifier_survives_an_api_container_recreation():
    snapshots = {"migration": "0015_sentinel_execution_identity", "tables": {"users": {"user": "digest"}}}
    filesystem = set()
    submissions = []

    def run(arguments, *, input_text=None, capture=False, stage=None):
        # Every call models a newly created container with an empty temporary directory.
        filesystem.clear()
        if arguments[-2:] != ["-", "snapshot"]:
            assert arguments[-2] in filesystem, "An ephemeral helper path cannot survive recreation"
        assert capture and stage == "storage_snapshot"
        assert input_text and "physical_schema_guard.py" in input_text and "verify_storage.py" in input_text
        compile(input_text, "<storage-verifier>", "exec")
        submissions.append(input_text)
        return json.dumps(snapshots)

    compose = ["docker", "compose", "-p", "trackvance-v070-test-identity-0123456789ab"]
    before = identity_sso_cycle.storage_snapshot(run, compose)
    after = identity_sso_cycle.storage_snapshot(run, compose)
    assert before == after == snapshots
    assert len(submissions) == 2
