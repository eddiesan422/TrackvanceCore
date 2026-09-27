"""Persistence probes must work when Compose replaces the API container."""
import json

import identity_sso_cycle


def test_storage_verifier_survives_an_api_container_recreation():
    snapshots = {"migration": "0012_delivery_target_audit", "tables": {"users": {"user": "digest"}}}
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

    compose = ["docker", "compose", "-p", "trackvance-identity-e2e-test"]
    before = identity_sso_cycle.storage_snapshot(run, compose)
    after = identity_sso_cycle.storage_snapshot(run, compose)
    assert before == after == snapshots
    assert len(submissions) == 2
