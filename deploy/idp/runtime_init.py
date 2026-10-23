"""Offline, nonroot extraction of locally built and hash verified Python layers."""
import copy
import hashlib
import json
import os
import resource
import tarfile
import time
import venv
from pathlib import Path, PurePosixPath

PREFIX = "usr/local/lib/python3.12/site-packages/"


def main():
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024**3,) * 2)
    if os.getuid() != 65532:
        raise RuntimeError("NONROOT_REQUIRED")
    manifest = json.loads(Path("/app/runtime-layers.lock.json").read_text())
    layers = manifest["layers"]
    if (not isinstance(layers, list) or len(layers) != 2
            or any(not isinstance(value, str) or len(value) != 64
                   or any(char not in "0123456789abcdef" for char in value) for value in layers)):
        raise RuntimeError("SOFTWARE_MANIFEST_INVALID")
    started = time.monotonic()
    root = Path("/opt/venv")
    receipt = root / "runtime-receipt.json"
    if receipt.exists():
        saved = json.loads(receipt.read_text())
        if saved.get("verified_layers") != layers:
            raise RuntimeError("RUNTIME_RECEIPT_MISMATCH")
        print(json.dumps({"cached": True, **saved}))
        return
    if (root / "lib").exists():
        raise RuntimeError("RUNTIME_PARTIAL_VOLUME")
    venv.EnvBuilder(with_pip=False, symlinks=False).create(root)
    site = root / "lib/python3.12/site-packages"
    count = 0
    extracted_size = 0
    with tarfile.open("/software/runtime.tar", "r:") as archive:
        for digest in layers:
            entry = "blobs/sha256/" + digest
            with archive.extractfile(entry) as compressed:
                if hashlib.file_digest(compressed, "sha256").hexdigest() != digest:
                    raise RuntimeError("SOFTWARE_LAYER_INTEGRITY")
                compressed.seek(0)
                with tarfile.open(fileobj=compressed, mode="r|gz") as layer:
                    for original in layer:
                        name = original.name.removeprefix("./")
                        if not name.startswith(PREFIX):
                            continue
                        relative = name[len(PREFIX):]
                        if not relative:
                            continue
                        parts = PurePosixPath(relative).parts
                        if ".." in parts or any(part.startswith(".wh.") for part in parts):
                            raise RuntimeError("UNEXPECTED_LAYER_PATH")
                        if not (original.isfile() or original.isdir() or original.issym()):
                            raise RuntimeError("UNEXPECTED_LAYER_ENTRY")
                        if original.issym() and PurePosixPath(original.linkname).is_absolute():
                            raise RuntimeError("UNEXPECTED_LAYER_SYMLINK")
                        extracted_size += original.size
                        count += 1
                        if extracted_size > 18 * 1024**3 or count > 200000:
                            raise RuntimeError("SOFTWARE_LAYER_LIMIT")
                        member = copy.copy(original)
                        member.name = relative
                        layer.extract(member, path=site, filter="data")
    result = {"verified_layers": layers, "files": count,
        "uncompressed_bytes": extracted_size,
        "elapsed_seconds": round(time.monotonic() - started, 4),
        "source_image_config_sha256": manifest["source_image_config_sha256"],
        "requirements_sha256": manifest["requirements_sha256"],
        "paddle_wheel_sha256": manifest["paddle_wheel_sha256"]}
    receipt.write_text(json.dumps(result, indent=2))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
