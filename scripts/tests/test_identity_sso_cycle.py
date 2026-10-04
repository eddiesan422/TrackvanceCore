"""Persistence probes must work when Compose replaces the API container."""
import json

import identity_sso_cycle
import pytest


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
        assert input_text and "def snapshot(" in input_text
        compile(input_text, "<storage-verifier>", "exec")
        submissions.append(input_text)
        return json.dumps(snapshots)

    compose = ["docker", "compose", "-p", "trackvance-v070-test-identity-0123456789ab"]
    before = identity_sso_cycle.storage_snapshot(run, compose)
    after = identity_sso_cycle.storage_snapshot(run, compose)
    assert before == after == snapshots
    assert len(submissions) == 2
