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
    from ci.image_bundle import inspect_image, validate_manifest, validated_host_mapping
    sha = values.get("CI_SOURCE_SHA", values.get("GITHUB_SHA", ""))
    manifest = validate_manifest(Path(path), sha, archives=False)
    mapping = values.get("TRACKVANCE_CI_HOST_IMAGE_MAPPING")
    mapping_path = Path(mapping) if mapping else Path(path).parent / "host-images.json"
    if mapping or mapping_path.exists():
        return validated_host_mapping(Path(path), sha, mapping_path)
    if manifest["digest_kind"] != "DOCKER_CONFIGURATION_SHA256":
        raise ValueError("New CI image manifests require a verified host identity mapping")
    images = {}
    for role, descriptor in manifest["images"].items():
        digest = descriptor["image_id"]
        row = inspect_image(digest, sha)
        # Compatibility only for untransferred classic manifests. A target
        # descriptor ID cannot masquerade as their declared configuration ID.
        if row["Id"] != digest or row.get("Descriptor"):
            raise ValueError("Cross-store CI images require a verified host identity mapping")
        images[role] = digest
    return images
