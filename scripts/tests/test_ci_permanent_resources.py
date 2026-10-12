import json

import pytest
from ci import local_resources
from test_ci_local_image_registry import registered


@pytest.mark.parametrize("project", ["trackvance-certification", "trackvance-codex", "bikerwash"])
def test_permanent_project_cannot_be_registered_even_without_runner_environment(project):
    with pytest.raises(ValueError, match="Permanent"):
        local_resources.register_project(project, {})


def test_permanent_image_labels_prevent_disposable_registry_cleanup(tmp_path):
    docker, path = registered(tmp_path)
    value = json.loads(path.read_text())
    identity = value["builds"][0]["image"]["image_id"]
    docker.images[identity]["Config"]["Labels"][local_resources.PERMANENT_LABEL] = "false"
    result = local_resources.cleanup_registered_images(path, value["execution_id"], command=docker)
    assert not result["removed"] and result["skipped"][0]["reason"] == "IMAGE_IDENTITY_OR_REFERENCES_CHANGED"
    assert identity in docker.images and not any(args[:2] == ("image", "rm") for args in docker.calls)
