"""Linux test images retain authentic historical Git inputs without host secrets."""
from __future__ import annotations

import io
import json
import subprocess
import zipfile
from pathlib import Path

import pytest
from ci import local_backend, local_resources

ORIGIN = "https://github.com/eddiesan422/TrackvanceCore.git"
VERSIONS = ("0.5.1", "0.6.0", "0.6.1", "0.7.0", "0.8.0")


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(["git", "-C", str(repository), *arguments], check=True,
        capture_output=True, text=True, encoding="utf-8", timeout=30).stdout.strip()


@pytest.fixture
def source_repository(tmp_path, monkeypatch):
    repository = tmp_path / "source"
    repository.mkdir()
    git(repository, "init", "--quiet")
    git(repository, "config", "user.name", "Fixture")
    git(repository, "config", "user.email", "fixture@trackvance.test")
    git(repository, "config", "core.autocrlf", "false")
    git(repository, "remote", "add", "origin", ORIGIN)
    for name, payload in {
        ".gitignore": ".env\nbackend/credentials/\n",
        "compose.yml": "services: {}\n",
        "backend/Dockerfile": "FROM python:3.12-slim-bookworm\n",
        "deploy/docker/frontend.Dockerfile": "FROM scratch\n",
        "deploy/docker/backend-tests.Dockerfile": "FROM runtime AS local-backend-tests\nCOPY . /app/source/\n",
    }.items():
        path = repository / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
    historical = {}
    for version in (*VERSIONS, "0.8.5"):
        (repository / "backend/pyproject.toml").write_text('[project]\nversion="' + version + '"\n', encoding="utf-8")
        git(repository, "add", ".")
        git(repository, "commit", "--quiet", "-m", "Source fixture " + version)
        if version != "0.8.5":
            historical[version] = git(repository, "rev-parse", "HEAD")
    (repository / ".env").write_text("PRIVATE_FIXTURE=must-not-be-copied\n", encoding="utf-8")
    private = repository / "backend/credentials"
    private.mkdir()
    (private / "synthetic.secret").write_bytes(b"ignored private fixture")
    monkeypatch.setattr(local_resources, "HISTORICAL_SOURCES", historical)
    return repository, git(repository, "rev-parse", "HEAD"), historical


def real_execute(observed):
    def execute(arguments, directory, phase, _remaining, **options):
        assert arguments[0] == "git", "These tests must never invoke Docker"
        observed.append((phase, arguments))
        subprocess.run(arguments, cwd=options.get("cwd", directory), check=True,
            capture_output=True, timeout=30)
        return {"status": "PASS"}
    return execute


def test_shallow_linux_context_fetches_all_local_sources_and_passes_real_archive_provenance(source_repository, tmp_path):
    repository, candidate, historical = source_repository
    directory = tmp_path / "evidence"
    directory.mkdir()
    observed = []
    actual_execute = real_execute(observed)

    def execute(arguments, directory, phase, remaining, **options):
        result = actual_execute(arguments, directory, phase, remaining, **options)
        if phase == "backend-test-source-clone":
            context = Path(arguments[-1])
            for source in historical.values():
                missing = subprocess.run(["git", "-C", str(context), "cat-file", "-e", source + "^{commit}"],
                    check=False, capture_output=True, timeout=30)
                assert missing.returncode != 0, "The regression fixture must actually be shallow"
        return result

    context = local_backend.test_context(repository, directory, candidate, execute)
    assert not (context / ".git").exists() and (context / ".ci-source-git").is_dir()
    assert not (context / ".env").exists() and not (context / "backend/credentials").exists()
    assert (repository / ".env").read_text() == "PRIVATE_FIXTURE=must-not-be-copied\n"
    fetch = next(arguments for phase, arguments in observed if phase == "backend-test-historical-fetch")
    assert fetch == ["git", "fetch", "--no-tags", "--depth", "1", "origin",
        *[source + ":refs/trackvance/historical/" + version for version, source in historical.items()]]
    assert [phase for phase, _ in observed] == ["backend-test-source-clone", "backend-test-historical-fetch",
        "backend-test-source-checkout", "backend-test-source-origin"]
    # Simulate the image's existing COPY/mv step, then exercise the unchanged
    # archive/provenance checker against every fetched source commit.
    (context / ".ci-source-git").rename(context / ".git")
    assert git(context, "rev-parse", "HEAD") == candidate and git(context, "remote", "get-url", "origin") == ORIGIN
    proof = json.loads((directory / "backend-test-source-proof.json").read_text())
    assert proof["status"] == "PASS" and proof["source_sha"] == candidate
    assert set(proof["historical_sources"]) == set(VERSIONS)
    for version, source in historical.items():
        assert git(context, "rev-parse", "refs/trackvance/historical/" + version) == source
        archive = subprocess.run(["git", "-C", str(context), "archive", "--format=zip", source],
            check=True, capture_output=True, timeout=30).stdout
        extracted = tmp_path / ("archive-" + version)
        with zipfile.ZipFile(io.BytesIO(archive)) as package:
            package.extractall(extracted)
        actual = local_resources.historical_source_proof(context, extracted, source, version)
        assert actual["source_sha"] == source and actual["source_version"] == version
        assert actual["tree_sha"] == proof["historical_sources"][version]["tree_sha"]


@pytest.mark.parametrize("change", ["foreign_origin", "missing_commit", "wrong_version"])
def test_context_rejects_unverified_local_sources_before_cloning(source_repository, tmp_path, monkeypatch, change):
    repository, candidate, historical = source_repository
    if change == "foreign_origin":
        git(repository, "remote", "set-url", "origin", "https://github.com/foreign/TrackvanceCore.git")
    elif change == "missing_commit":
        monkeypatch.setitem(local_resources.HISTORICAL_SOURCES, "0.8.0", "a" * 40)
    else:
        monkeypatch.setitem(local_resources.HISTORICAL_SOURCES, "0.7.0", historical["0.8.0"])
    directory = tmp_path / "evidence"
    directory.mkdir()
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        local_backend.test_context(repository, directory, candidate,
            lambda *_args, **_options: pytest.fail("Unverified source must never be cloned"))
    assert not (directory / "backend-test-context").exists()
    assert not (directory / "backend-test-source-proof.json").exists()


def test_context_rejects_missing_fetch_objects_before_origin_change_or_copy(source_repository, tmp_path):
    repository, candidate, _historical = source_repository
    directory = tmp_path / "evidence"
    directory.mkdir()
    observed = []
    actual_execute = real_execute(observed)

    def execute(arguments, directory, phase, remaining, **options):
        if phase == "backend-test-historical-fetch":
            return {"status": "PASS"}  # A transport claim cannot substitute for actual Git objects.
        return actual_execute(arguments, directory, phase, remaining, **options)

    with pytest.raises(subprocess.CalledProcessError):
        local_backend.test_context(repository, directory, candidate, execute)
    context = directory / "backend-test-context"
    assert (context / ".git").is_dir() and not (context / ".ci-source-git").exists()
    assert not (context / "LocalBackend.Dockerfile").exists()
    assert not (directory / "backend-test-source-proof.json").exists()
    assert git(context, "remote", "get-url", "origin") != ORIGIN
