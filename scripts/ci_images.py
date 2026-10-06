"""Opt-in CI image pinning; standalone disposable runners retain their defaults."""
from __future__ import annotations

import os
from pathlib import Path


def verified_images(environment: dict[str, str] | None = None) -> dict[str, str] | None:
    values = os.environ if environment is None else environment
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
