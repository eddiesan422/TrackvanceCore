"""Tiny real Docker-save and OCI trees; no Docker calls or invented digests."""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import tarfile
from pathlib import Path

from ci.image_content import CONFIG_MEDIA, INDEX_TYPES, MANIFEST_TYPES


def packed(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def write_members(path: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(path, "w") as archive:
        for name, payload in members.items():
            item = tarfile.TarInfo(name)
            item.size = len(payload)
            archive.addfile(item, io.BytesIO(payload))


def image_archive(path: Path, sha: str, role: str, *, kind: str = "configuration",
                  compressed: bool = False, config_change: dict | None = None) -> dict:
    layer_stream = io.BytesIO()
    with tarfile.open(fileobj=layer_stream, mode="w") as layer:
        item = tarfile.TarInfo("fixture.txt")
        payload = ("verified " + role).encode()
        item.size = len(payload)
        layer.addfile(item, io.BytesIO(payload))
    uncompressed = layer_stream.getvalue()
    layer_bytes = gzip.compress(uncompressed, mtime=0) if compressed else uncompressed
    diff_id, layer_digest = digest(uncompressed), digest(layer_bytes)
    config = {"architecture": "amd64", "os": "linux", "created": "2026-10-09T00:00:00Z",
              "config": {"Labels": {"org.opencontainers.image.revision": sha,
                                    "org.opencontainers.image.version": "0.8.5"},
                         "Env": ["FIXTURE_ROLE=" + role]},
              "rootfs": {"type": "layers", "diff_ids": [diff_id]},
              "history": [{"created_by": "deterministic fixture"}]}
    if config_change:
        config.update(config_change)
    config_bytes = packed(config)
    config_digest = digest(config_bytes)
    prefix = "blobs/sha256/"
    config_name = config_digest[7:] + ".json" if kind == "configuration" else prefix + config_digest[7:]
    layer_name = "layer/layer.tar" if kind == "configuration" else prefix + layer_digest[7:]
    tag = "trackvance-transport-fixture:" + role
    members = {config_name: config_bytes, layer_name: layer_bytes}
    engine_id = config_digest
    if kind != "configuration":
        media = "application/vnd.oci.image.manifest.v1+json"
        assert media in MANIFEST_TYPES
        manifest = {"schemaVersion": 2, "mediaType": media,
                    "config": {"mediaType": CONFIG_MEDIA, "digest": config_digest, "size": len(config_bytes)},
                    "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar" + ("+gzip" if compressed else ""),
                                "digest": layer_digest, "size": len(layer_bytes)}]}
        payload = packed(manifest)
        engine_id = digest(payload)
        members[prefix + engine_id[7:]] = payload
        target = {"mediaType": media, "digest": engine_id, "size": len(payload),
                  "platform": {"os": "linux", "architecture": "amd64"}}
        if kind == "index":
            media = "application/vnd.oci.image.index.v1+json"
            assert media in INDEX_TYPES
            payload = packed({"schemaVersion": 2, "mediaType": media, "manifests": [target]})
            engine_id = digest(payload)
            members[prefix + engine_id[7:]] = payload
            target = {"mediaType": media, "digest": engine_id, "size": len(payload)}
        members["index.json"] = packed({"schemaVersion": 2, "manifests": [target]})
        members["oci-layout"] = packed({"imageLayoutVersion": "1.0.0"})
    members["manifest.json"] = packed([{"Config": config_name, "RepoTags": [tag], "Layers": [layer_name]}])
    write_members(path, members)
    return {"engine_id": engine_id, "configuration_digest": config_digest, "rootfs_diff_ids": [diff_id],
            "config": config, "tag": tag, "members": members, "layer_name": layer_name}
