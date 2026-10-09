"""Verify Docker-save content without extracting image filesystem paths."""
from __future__ import annotations

import base64
import gzip
import hashlib
import json
import re
import tarfile
from pathlib import Path, PurePosixPath

CHUNK = 1024**2
ARCHIVE_LIMIT = 8 * 1024**3
CONFIG_LIMIT = 4 * CHUNK
MAPPING_LIMIT = 96 * CHUNK
DIGEST = re.compile(r"sha256:[a-f0-9]{64}")
TAG = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:/-]{0,255}")
CONFIG_TYPES = {"application/vnd.oci.image.config.v1+json",
                "application/vnd.docker.container.image.v1+json"}
INDEX_TYPES = {"application/vnd.oci.image.index.v1+json",
               "application/vnd.docker.distribution.manifest.list.v2+json"}
MANIFEST_TYPES = {"application/vnd.oci.image.manifest.v1+json",
                  "application/vnd.docker.distribution.manifest.v2+json"}
CONFIG_MEDIA = "application/vnd.oci.image.config.v1+json"


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate image metadata key")
        result[key] = value
    return result


def _json(payload: bytes):
    try:
        return json.loads(payload, object_pairs_hook=_pairs,
                          parse_constant=lambda _: (_ for _ in ()).throw(
                              ValueError("Nonfinite image metadata")))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid image metadata") from error


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _config(payload: bytes, sha: str) -> dict:
    document = _json(payload)
    if not isinstance(document, dict) or not document:
        raise ValueError("Invalid image configuration")
    runtime, rootfs = document.get("config"), document.get("rootfs")
    if not isinstance(runtime, dict) or not isinstance(rootfs, dict) or not runtime or not rootfs:
        raise ValueError("Invalid image configuration structure")
    labels = runtime.get("Labels") or {}
    if not isinstance(labels, dict) or not labels:
        raise ValueError("Invalid image configuration labels")
    layers = rootfs.get("diff_ids")
    if (document.get("os") != "linux" or document.get("architecture") != "amd64"
            or labels.get("org.opencontainers.image.revision") != sha
            or labels.get("org.opencontainers.image.version") != "0.8.5"
            or rootfs.get("type") != "layers" or not isinstance(layers, list)
            or len(layers) > 256 or not all(isinstance(v, str) and DIGEST.fullmatch(v)
                                         for v in layers)):
        raise ValueError("Image configuration differs from certified source or runtime")
    return document


def _node(payload: bytes, media: str) -> dict:
    return {"digest": _digest(payload), "size": len(payload), "mediaType": media,
            "payload_base64": base64.b64encode(payload).decode("ascii")}


def validate_identity(proof: dict, engine_id: str, sha: str) -> dict:
    """Bind an Engine ID to exact configuration bytes through its hashed tree."""
    if not isinstance(proof, dict) or not proof:
        raise ValueError("Invalid image content proof")
    chain = proof.get("chain")
    if (proof.get("engine_id") != engine_id or not isinstance(engine_id, str) or not DIGEST.fullmatch(engine_id)
            or not isinstance(chain, list) or not 1 <= len(chain) <= 17
            or not all(isinstance(node, dict) for node in chain)
            or chain[0].get("digest") != engine_id):
        raise ValueError("Image content identity changed")
    payloads, documents = [], []
    for number, node in enumerate(chain):
        final = number == len(chain) - 1
        maximum = CONFIG_LIMIT if final else CHUNK
        encoded = node.get("payload_base64")
        if (not isinstance(encoded, str) or len(encoded) > (maximum + 2) // 3 * 4
                or type(node.get("size")) is not int or not 0 < node["size"] <= maximum):
            raise ValueError("Image identity metadata is excessive")
        try:
            payload = base64.b64decode(encoded, validate=True)
        except ValueError as error:
            raise ValueError("Invalid image identity encoding") from error
        if len(payload) != node["size"] or _digest(payload) != node.get("digest"):
            raise ValueError("Image identity bytes differ from their digest")
        payloads.append(payload)
        documents.append(_json(payload))
    for number, document in enumerate(documents[:-1]):
        node, following = chain[number], chain[number + 1]
        if (not isinstance(document, dict) or document.get("schemaVersion") != 2
                or document.get("mediaType") != node.get("mediaType")
                or node.get("mediaType") not in INDEX_TYPES | MANIFEST_TYPES):
            raise ValueError("Invalid image target tree")
        children = (document.get("manifests", []) if node["mediaType"] in INDEX_TYPES
                    else [document.get("config", {})])
        matches = [child for child in children if isinstance(child, dict) and all(
            child.get(key) == following.get(key) for key in ("digest", "size", "mediaType"))]
        if len(matches) != 1:
            raise ValueError("Image target tree link changed")
        platform = matches[0].get("platform")
        if platform is not None and (platform.get("os"), platform.get("architecture")) != ("linux", "amd64"):
            raise ValueError("Image target selected a different platform")
    if chain[-1].get("mediaType") not in CONFIG_TYPES:
        raise ValueError("Image target does not terminate in a configuration")
    config = _config(payloads[-1], sha)
    kind = "CONFIGURATION_SHA256" if len(chain) == 1 else "OCI_TARGET_SHA256"
    if (proof.get("image_id_kind") != kind
            or proof.get("configuration_digest") != chain[-1]["digest"]
            or proof.get("rootfs_diff_ids") != config["rootfs"]["diff_ids"]):
        raise ValueError("Image configuration identity or ordered layers changed")
    layers = proof.get("layers")
    if (not isinstance(layers, list) or len(layers) != len(proof["rootfs_diff_ids"])
            or any(not isinstance(layer, dict) or not isinstance(layer.get("digest"), str)
                   or not DIGEST.fullmatch(layer["digest"]) or type(layer.get("size")) is not int
                   or not 0 < layer["size"] <= ARCHIVE_LIMIT or layer.get("diff_id") != expected
                   for layer, expected in zip(layers, proof["rootfs_diff_ids"], strict=True))):
        raise ValueError("Image proof layer identities changed")
    if len(chain) > 1:
        descriptors = documents[-2].get("layers")
        if (not isinstance(descriptors, list) or len(descriptors) != len(layers)
                or any(not isinstance(descriptor, dict) or any(descriptor.get(key) != layer[key]
                    for key in ("digest", "size"))
                    for descriptor, layer in zip(descriptors, layers, strict=True))):
            raise ValueError("Image proof layers differ from target tree")
    return config


def archive_identity(path: Path, engine_id: str, sha: str) -> dict:
    """Hash config, target tree and ordered uncompressed layers of one image."""
    if (path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= ARCHIVE_LIMIT
            or not DIGEST.fullmatch(engine_id) or not re.fullmatch(r"[a-f0-9]{40}", sha)):
        raise ValueError("Invalid immutable image archive input")
    with tarfile.open(path, "r:") as saved:
        members = {}
        for member in saved:
            name = member.name.removeprefix("./").rstrip("/")
            relative = PurePosixPath(name)
            if (len(members) >= 4096 or name in members or not name
                    or relative.is_absolute() or ".." in relative.parts
                    or relative.as_posix() != name or "\\" in name or ":" in name
                    or not (member.isfile() or member.isdir()) or member.size < 0):
                raise ValueError("Ambiguous or unsafe image archive member")
            members[name] = member

        def read(name, maximum):
            member = members.get(name)
            if member is None or not member.isfile() or not 0 < member.size <= maximum:
                raise ValueError("Missing or excessive image metadata")
            source = saved.extractfile(member)
            if source is None:
                raise ValueError("Missing image metadata stream")
            with source:
                return source.read(maximum + 1)

        manifests = _json(read("manifest.json", CHUNK))
        if not isinstance(manifests, list) or len(manifests) != 1 or not isinstance(manifests[0], dict):
            raise ValueError("Image archive must contain exactly one image")
        manifest = manifests[0]
        payload = read(manifest.get("Config"), CONFIG_LIMIT)
        config_digest = _digest(payload)
        config = _config(payload, sha)
        config_node = _node(payload, CONFIG_MEDIA)
        found, visited = [], 0

        def walk(digest, expected, chain):
            nonlocal visited
            visited += 1
            if (visited > 256 or len(chain) >= 16 or not isinstance(digest, str)
                    or not DIGEST.fullmatch(digest) or digest in {n["digest"] for n in chain}):
                raise ValueError("Invalid or excessive image target graph")
            content = read("blobs/sha256/" + digest[7:], CHUNK)
            document = _json(content)
            if not isinstance(document, dict) or not document:
                raise ValueError("Invalid image target metadata")
            media = document.get("mediaType")
            node = _node(content, media)
            if (node["digest"] != digest or document.get("schemaVersion") != 2
                    or media not in INDEX_TYPES | MANIFEST_TYPES or expected is not None and any(
                        node[key] != expected.get(key) for key in ("digest", "mediaType", "size"))):
                raise ValueError("Image target descriptor differs from its bytes")
            if media in MANIFEST_TYPES:
                descriptor = document.get("config", {})
                if descriptor.get("digest") != config_digest:
                    return
                if descriptor.get("mediaType") not in CONFIG_TYPES or descriptor.get("size") != len(payload):
                    raise ValueError("Image target configuration descriptor changed")
                linked_config = {**config_node, "mediaType": descriptor["mediaType"]}
                found.append(([*chain, node, linked_config], document.get("layers")))
                return
            children = document.get("manifests")
            if not isinstance(children, list) or len(children) > 256:
                raise ValueError("Invalid image target index")
            for child in children:
                if not isinstance(child, dict) or not child:
                    raise ValueError("Invalid image target child")
                platform = child.get("platform")
                if platform is not None and (platform.get("os"), platform.get("architecture")) != ("linux", "amd64"):
                    continue
                walk(child.get("digest"), child, [*chain, node])

        if engine_id == config_digest:
            chain, descriptors = [config_node], None
        else:
            walk(engine_id, None, [])
            if len(found) != 1:
                raise ValueError("Image target does not bind one exact Linux configuration")
            chain, descriptors = found[0]
        names, diff_ids = manifest.get("Layers"), config["rootfs"]["diff_ids"]
        if (not isinstance(names, list) or len(names) != len(diff_ids)
                or not all(isinstance(name, str) for name in names)
                or descriptors is not None and (not isinstance(descriptors, list)
                                                or len(descriptors) != len(names))):
            raise ValueError("Image archive layer order changed")
        layers, total = [], len(payload)
        for number, (name, diff_id) in enumerate(zip(names, diff_ids, strict=True)):
            member = members.get(name)
            if member is None or not member.isfile() or not 0 < member.size <= ARCHIVE_LIMIT:
                raise ValueError("Missing or excessive image layer")
            source = saved.extractfile(member)
            if source is None:
                raise ValueError("Missing image layer stream")
            with source:
                raw_hash = hashlib.sha256()
                while chunk := source.read(CHUNK):
                    raw_hash.update(chunk)
                raw_digest = "sha256:" + raw_hash.hexdigest()
                if descriptors is not None:
                    descriptor = descriptors[number]
                    if (not isinstance(descriptor, dict) or descriptor.get("digest") != raw_digest
                            or descriptor.get("size") != member.size):
                        raise ValueError("Image layer differs from target descriptor")
                source.seek(0)
                prefix = source.read(4)
                source.seek(0)
                if prefix == b"\x28\xb5\x2f\xfd":
                    raise ValueError("Unsupported zstd image layer; no content substituted")
                decoded = gzip.GzipFile(fileobj=source) if prefix[:2] == b"\x1f\x8b" else source
                digest, size = hashlib.sha256(), 0
                try:
                    while chunk := decoded.read(CHUNK):
                        size += len(chunk)
                        if total + size > ARCHIVE_LIMIT:
                            raise ValueError("Uncompressed image exceeds finite archive budget")
                        digest.update(chunk)
                finally:
                    if decoded is not source:
                        decoded.close()
            if "sha256:" + digest.hexdigest() != diff_id:
                raise ValueError("Image layer differs from configuration diff_id")
            total += size
            layers.append({"digest": raw_digest, "size": member.size, "diff_id": diff_id})
        references = manifest.get("RepoTags") or []
        if (not isinstance(references, list) or len(references) > 16
                or len(set(references)) != len(references)
                or not all(isinstance(ref, str) and TAG.fullmatch(ref) for ref in references)):
            raise ValueError("Invalid image archive references")
    proof = {"engine_id": engine_id, "image_id_kind": "CONFIGURATION_SHA256" if len(chain) == 1 else "OCI_TARGET_SHA256",
             "configuration_digest": config_digest, "rootfs_diff_ids": diff_ids, "chain": chain,
             "layers": layers, "references": references}
    validate_identity(proof, engine_id, sha)
    return proof
