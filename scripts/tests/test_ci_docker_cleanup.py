"""Exact-ID cleanup guards; every Docker call is simulated."""
import copy
import json

import pytest
from ci import docker_cleanup as cleanup
from ci.common import EvidenceError

PROJECT = "trackvance-v080-test-origininspection-aaaaaaaaaaaa"


def image(number, tags):
    return {"id": "sha256:" + str(number) * 64, "tags": tags, "digests": [], "created": "2026-01-01",
            "size_bytes": 100, "labels": {"com.docker.compose.project": "trackvance-certification"}, "rootfs_layers": []}


def container(identity, project, image_id, volume):
    return {"id": identity * 64, "name": project + "-api-1", "image_id": image_id,
            "image_reference": image_id, "status": "exited", "labels": {"com.docker.compose.project": project},
            "config_sha256": "a" * 64, "host_config_sha256": "b" * 64,
            "mounts": [{"Type": "volume", "Name": volume, "Source": "/vol/" + volume, "Destination": "/data", "RW": True}],
            "networks": [project + "_default"]}


@pytest.fixture
def case():
    volumes = [{"name": name, "labels": {"com.docker.compose.project": project}, "driver": "local",
                "mountpoint": "/vol/" + name, "created": "2026-01-01"}
               for project, name in (("trackvance-certification", "trackvance-certification_data"), (PROJECT, PROJECT + "_data"))]
    cache = [{"ID": char * 26, "Parents": parents, "description_sha256": char * 64,
              "description_markers": markers, "Reclaimable": True, "Size": "100", "Mutable": False,
              "Type": "regular", "UsageCount": 1, "LastUsedAt": "2026-01-01"}
             for char, parents, markers in (("a", ["b" * 26], ["trackvance"]), ("b", [], []))]
    images = [image(1, ["trackvance-ci:backend"]), image(2, [PROJECT + ":backend"]), image(3, ["trackvance-certification-api:latest"])]
    containers = [container("a", "trackvance-certification", images[0]["id"], volumes[0]["name"]),
                  container("b", PROJECT, images[1]["id"], volumes[1]["name"])]
    state = {"kind": "SANITIZED_CLI_DOCKER_INVENTORY", "containers": containers, "images": images,
             "volumes": volumes, "cache": cache, "builder_list": "default\ndesktop-linux",
             "networks": [{"id": c["id"], "name": c["networks"][0], "labels": c["labels"],
                           "driver": "bridge", "endpoints": [c["id"]]} for c in containers]}
    proof = {"schema_version": 1, "projects": {PROJECT: {"approved": True, "not_sole_copy": True,
        "unshared_storage": True, "read_only_clone_pass": True, "promotion_legacy_pass": True, "evidence_sha256": ["a" * 64]}}}
    cache_proof = {"schema_version": 1, "builder": "desktop-linux", "inventory_sha256": cleanup.fingerprint(state),
        "records": [{"id": c["ID"], "description_sha256": c["description_sha256"], "parents": c["Parents"], "owned_root_id": "a" * 26} for c in cache]}
    return state, proof, cache_proof


def planned(case):
    state, proof, cache = case
    cache["inventory_sha256"] = cleanup.fingerprint(state)
    return cleanup.plan(state, proof, cache)


def simulated(case, monkeypatch, *, fail=None, drop_repository_digests=False):
    state = copy.deepcopy(case[0])
    calls = []
    monkeypatch.setattr(cleanup, "inventory", lambda **kwargs: copy.deepcopy(state))
    monkeypatch.setattr(cleanup, "cache_inventory", lambda: copy.deepcopy(state["cache"]))
    def docker(*args):
        calls.append(args)
        if fail and fail(args):
            raise ValueError("Controlled Docker operation failure")
        if args[0] == "rm":
            state["containers"] = [c for c in state["containers"] if c["id"] != args[1]]
            for n in state["networks"]:
                n["endpoints"] = [i for i in n["endpoints"] if i != args[1]]
        elif args[0] == "stop":
            next(c for c in state["containers"] if c["id"] == args[-1])["status"] = "exited"
        elif args[:2] == ("network", "rm"):
            state["networks"] = [n for n in state["networks"] if n["id"] != args[2]]
        elif args[:2] == ("volume", "rm"):
            state["volumes"] = [v for v in state["volumes"] if v["name"] != args[2]]
        elif args[:2] == ("image", "rm"):
            for i in list(state["images"]):
                if args[2] == i["id"]:
                    state["images"].remove(i)
                elif args[2] in i["tags"]:
                    repository = args[2].split(":")[0]
                    i["tags"].remove(args[2])
                    if drop_repository_digests and not any(tag.split(":")[0] == repository for tag in i["tags"]):
                        i["digests"] = [digest for digest in i["digests"] if not digest.startswith(repository + "@")]
                    if not i["tags"]:
                        state["images"].remove(i)
        elif args[:2] == ("buildx", "prune"):
            identity = args[args.index("--filter") + 1].removeprefix("id=")
            state["cache"] = [c for c in state["cache"] if c["ID"] != identity]
        return "measured Docker result"
    monkeypatch.setattr(cleanup, "docker", docker)
    return state, calls


def test_stopped_current_installation_is_protected_but_unused_old_certification_tag_is_retired(case):
    document = planned(case)
    assert document["containers"][0]["id"] == "b" * 64
    assert {i["id"] for i in document["images"]} == {"sha256:" + n * 64 for n in ("2", "3")}
    assert document["protected"]["containers"][0]["status"] == "exited"
    assert document["protected"]["images"][0]["id"] == "sha256:" + "1" * 64


def test_foreign_alias_and_explicit_restart_reference_protect_images(case):
    state = case[0]
    state["images"][2]["tags"].append("bikerwash:legitimate")
    assert "sha256:" + "3" * 64 not in {i["id"] for i in planned(case)["images"]}
    state["images"][2]["tags"].pop()
    state["containers"][0]["image_reference"] = "trackvance-certification-api:latest"
    assert "sha256:" + "3" * 64 not in {i["id"] for i in planned(case)["images"]}


def test_shared_volume_and_network_remain_with_protected_consumers(case):
    state = case[0]
    state["containers"][0]["mounts"].append(copy.deepcopy(state["containers"][1]["mounts"][0]))
    state["containers"][0]["networks"].append(state["containers"][1]["networks"][0])
    document = planned(case)
    assert document["volumes"] == document["networks"] == []


@pytest.mark.parametrize("flag", ["not_sole_copy", "unshared_storage", "read_only_clone_pass", "promotion_legacy_pass"])
def test_origin_inspection_requires_preservation_and_retirement_evidence(case, flag):
    case[1]["projects"][PROJECT][flag] = False
    with pytest.raises(EvidenceError):
        planned(case)


def test_cleanup_removes_exact_reviewed_resources_and_hashes_protected_before_after(case, monkeypatch, tmp_path):
    document = planned(case)
    state, calls = simulated(case, monkeypatch)
    result = cleanup.apply(document, tmp_path / "checkpoint.json")
    assert result["status"] == "PASS" and len(state["containers"]) == 1 and len(state["images"]) == 1
    assert result["protected_before_sha256"] == result["protected_after_sha256"]
    assert state["containers"][0]["status"] == "exited"
    prune = [c for c in calls if c[:2] == ("buildx", "prune")]
    assert len(prune) == 2 and all(c == ("buildx", "prune", "--builder", "desktop-linux", "--filter", "id=" + i, "--force")
                                 for c, i in zip(prune, ("a" * 26, "b" * 26), strict=True))
    assert not any("--force" in c for c in calls if c[:2] != ("buildx", "prune"))


@pytest.mark.parametrize("mutation", [
    lambda s: s["containers"][0].update(status="running"),
    lambda s: s["containers"][0].update(config_sha256="f" * 64),
    lambda s: s["containers"].append(container("c", "bikerwash-backend", s["images"][2]["id"], "foreign-volume")),
    lambda s: s["images"][2]["tags"].append("foreign:alias"),
])
def test_changed_installation_state_config_new_consumer_or_foreign_alias_aborts_before_removal(case, monkeypatch, tmp_path, mutation):
    document = planned(case)
    state, calls = simulated(case, monkeypatch)
    mutation(state)
    with pytest.raises(EvidenceError):
        cleanup.apply(document, tmp_path / "checkpoint.json")
    assert calls == [] and json.loads((tmp_path / "checkpoint.json").read_text())["status"] == "FAIL"


def test_failure_after_partial_removal_never_leaves_running_or_pass_checkpoint(case, monkeypatch, tmp_path):
    simulated(case, monkeypatch, fail=lambda c: c[:2] == ("volume", "rm"))
    checkpoint = tmp_path / "checkpoint.json"
    with pytest.raises(ValueError, match="Controlled"):
        cleanup.apply(planned(case), checkpoint)
    assert json.loads(checkpoint.read_text())["status"] == "PARTIAL"


def test_gc_absent_cache_is_verified_separately_and_still_present_returncode_is_failure(case, monkeypatch, tmp_path):
    document = planned(case)
    state, _ = simulated(case, monkeypatch)
    state["cache"] = state["cache"][:1]
    result = cleanup.apply(document, tmp_path / "absent.json")
    assert result["already_absent_cache"] == ["b" * 26]
    assert result["removed"]["cache"] == ["a" * 26]
    for key in ("containers", "images", "networks", "volumes"):
        case[0][key] = case[0][key][:1]
    case[1]["projects"] = {}
    document = planned(case)
    simulated(case, monkeypatch)
    monkeypatch.setattr(cleanup, "docker", lambda *args: "success without actual mutation")
    with pytest.raises(EvidenceError, match="CACHE_RECORD_STILL_PRESENT"):
        cleanup.apply(document, tmp_path / "still-present.json")
    assert json.loads((tmp_path / "still-present.json").read_text())["status"] == "FAIL"


def test_cache_must_be_in_reviewed_parent_closure_with_exact_description_hash(case):
    case[2]["records"][1]["description_sha256"] = "f" * 64
    with pytest.raises(EvidenceError, match="CACHE_IDENTITY_CHANGED"):
        planned(case)


def test_docker_null_parent_array_is_normalized_and_absent_unselected_parent_is_not_a_target(case):
    case[0]["cache"][1]["Parents"] = None
    case[2]["records"][1]["parents"] = []
    document = planned(case)
    assert document["cache"][1]["Parents"] == []


def test_cache_new_usage_and_image_identity_changes_cannot_be_removed(case, monkeypatch, tmp_path):
    document = planned(case)
    state, calls = simulated(case, monkeypatch)
    state["images"][2]["rootfs_layers"].append("sha256:" + "f" * 64)
    with pytest.raises(EvidenceError, match="RETIRING_IMAGE_IDENTITY_OR_CONSUMER_CHANGED"):
        cleanup.apply(document, tmp_path / "image-changed.json")
    assert not any(c[:3] == ("image", "rm", "trackvance-certification-api:latest") for c in calls)
    state, _ = simulated(case, monkeypatch)
    state["cache"][0]["UsageCount"] += 1
    with pytest.raises(EvidenceError, match="CACHE_IDENTITY_OR_USE_CHANGED"):
        cleanup.apply(document, tmp_path / "cache-used.json")
    assert json.loads((tmp_path / "cache-used.json").read_text())["status"] == "PARTIAL"


def test_reviewed_hash_mismatch_overwrites_a_stale_green_checkpoint_without_docker(case, monkeypatch, tmp_path):
    import sys
    reviewed = tmp_path / "plan.json"
    reviewed.write_text(json.dumps(planned(case)), encoding="utf-8")
    output = tmp_path / "checkpoint.json"
    output.write_text('{"status":"PASS"}', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["cleanup", "--apply-plan", str(reviewed), "--plan-sha256", "0" * 64,
                                     "--output", str(output)])
    monkeypatch.setattr(cleanup, "docker", lambda *args: pytest.fail("Hash mismatch must not call Docker"))
    assert cleanup.main() == 1
    assert json.loads(output.read_text())["error_code"] == "REVIEWED_PLAN_SHA256_MISMATCH"


def test_trackvance_owned_recovery_digest_alias_is_exact_and_foreign_alias_remains_protected(case):
    resource = case[0]["images"][2]
    resource["tags"] = ["sha256:" + "f" * 64]
    resource["digests"] = ["sha256@" + resource["id"]]
    assert resource["id"] in {i["id"] for i in planned(case)["images"]}
    resource["digests"] = ["sha256@sha256:" + "f" * 64]
    assert resource["id"] not in {i["id"] for i in planned(case)["images"]}
    resource["digests"] = ["sha256@" + resource["id"]]
    resource["tags"].append("foreign:alias")
    assert resource["id"] not in {i["id"] for i in planned(case)["images"]}


def test_relative_cache_age_display_can_change_while_actual_identity_and_usage_stay_fixed(case, monkeypatch, tmp_path):
    document = planned(case)
    state, _ = simulated(case, monkeypatch)
    state["cache"][0]["LastUsedAt"] = "one day ago"
    assert cleanup.apply(document, tmp_path / "age-display.json")["status"] == "PASS"


def reviewed_two_repository_image(case):
    resource = case[0]["images"][2]
    resource["tags"] = ["trackvance-v080-isolated:backend", "trackvance-v080-test-linux-typecheck-02503a0fe906:base"]
    resource["digests"] = [tag.split(":")[0] + "@" + resource["id"] for tag in resource["tags"]]
    return resource


@pytest.mark.parametrize("drop_repository_digests", [False, True])
def test_removing_last_reviewed_tag_of_repository_can_remove_only_its_digest(case, monkeypatch, tmp_path, drop_repository_digests):
    resource = reviewed_two_repository_image(case)
    document = planned(case)
    state, calls = simulated(case, monkeypatch, drop_repository_digests=drop_repository_digests)
    result = cleanup.apply(document, tmp_path / "repository-digest.json")
    assert result["status"] == "PASS" and not any(i["id"] == resource["id"] for i in state["images"])
    assert [call[2] for call in calls if call[:2] == ("image", "rm")][-2:] == resource["tags"]


def test_digest_stays_when_another_reviewed_tag_of_same_repository_remains(case, monkeypatch, tmp_path):
    resource = reviewed_two_repository_image(case)
    resource["tags"][1] = "trackvance-v080-isolated:web"
    resource["digests"] = resource["digests"][:1]
    state, _ = simulated(case, monkeypatch, drop_repository_digests=True)
    assert cleanup.apply(planned(case), tmp_path / "same-repository.json")["status"] == "PASS"
    assert not any(i["id"] == resource["id"] for i in state["images"])


@pytest.mark.parametrize("mutation", [
    "other_repository_digest_disappears", "digest_added", "foreign_alias", "new_consumer",
    "image_layer_changed", "image_size_changed", "image_labels_changed", "same_repository_digest_disappears_early",
])
def test_unexpected_image_change_after_untag_aborts_before_next_removal(case, monkeypatch, tmp_path, mutation):
    resource = reviewed_two_repository_image(case)
    if mutation == "same_repository_digest_disappears_early":
        resource["tags"][1] = "trackvance-v080-isolated:web"
        resource["digests"] = resource["digests"][:1]
    first_tag, second_tag = resource["tags"]
    document = planned(case)
    state, calls = simulated(case, monkeypatch, drop_repository_digests=True)
    safe_simulation = cleanup.docker
    def changed(*args):
        output = safe_simulation(*args)
        if args == ("image", "rm", first_tag):
            current = next(i for i in state["images"] if i["id"] == resource["id"])
            if mutation in {"other_repository_digest_disappears", "same_repository_digest_disappears_early"}:
                current["digests"] = []
            elif mutation == "digest_added":
                current["digests"].append("trackvance-v080-unreviewed@sha256:" + "f" * 64)
            elif mutation == "foreign_alias":
                current["tags"].append("foreign:alias")
            elif mutation == "new_consumer":
                state["containers"].append(container("c", "bikerwash-backend", resource["id"], "foreign-volume"))
            elif mutation == "image_layer_changed":
                current["rootfs_layers"].append("sha256:" + "f" * 64)
            elif mutation == "image_size_changed":
                current["size_bytes"] += 1
            elif mutation == "image_labels_changed":
                current["labels"]["io.trackvance.changed"] = "true"
        return output
    monkeypatch.setattr(cleanup, "docker", changed)
    checkpoint = tmp_path / "changed-after-untag.json"
    with pytest.raises(EvidenceError):
        cleanup.apply(document, checkpoint)
    assert ("image", "rm", first_tag) in calls and ("image", "rm", second_tag) not in calls
    assert json.loads(checkpoint.read_text())["status"] == "PARTIAL"
