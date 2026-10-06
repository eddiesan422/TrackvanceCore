"""Opt-in CI image pinning; standalone disposable runners retain their defaults."""
from __future__ import annotations

import os
from pathlib import Path


def verified_images(environment: dict[str, str] | None = None) -> dict[str, str] | None:
    values = os.environ if environment is None else environment
    local_path = values.get("TRACKVANCE_LOCAL_IMAGE_MANIFEST")
    if local_path:
        import json

        from ci.image_bundle import inspect_image
        document = json.loads(Path(local_path).read_text(encoding="utf-8"))
        sha = values.get("TRACKVANCE_SOURCE_SHA", "")
        if document.get("kind") != "LOCAL_IMAGE_PROOF" or document.get("source_sha") != sha or document.get("status") != "PASS":
            raise ValueError("Missing same-commit local image verification")
        images = document.get("images", {})
        if set(images) != {"backend", "web"}:
            raise ValueError("Local image proof requires both immutable roles")
        for image in images.values():
            inspect_image(image, sha)
        return images
    path = values.get("TRACKVANCE_CI_IMAGE_MANIFEST")
    if not path:
        return None
    from ci.image_bundle import inspect_image, validate_manifest
    sha = values.get("CI_SOURCE_SHA", values.get("GITHUB_SHA", ""))
    manifest = validate_manifest(Path(path), sha, archives=False)
    images = {}
    for role, descriptor in manifest["images"].items():
        digest = descriptor["image_id"]
        inspect_image(digest, sha)
        images[role] = digest
    return images
