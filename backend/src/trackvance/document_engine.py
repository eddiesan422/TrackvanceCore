"""Standalone local OCR process. No application imports, credentials or DB access.

Inputs/outputs are bounded files in one private work directory. Landlock denies
all other data paths and seccomp denies network and namespace/process inspection.
"""
from __future__ import annotations

import base64
import csv
import ctypes
import ctypes.util
import hashlib
import io
import json
import os
import re
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

MAX_PAGES = 50
MAX_PIXELS = 20_000_000
MAX_TEXT = 2_000_000


def confine(folder: Path):
    import resource
    if sys.platform != "linux":
        raise ValueError("DOCUMENT_SANDBOX_UNAVAILABLE")
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (120, 121))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024**2,) * 2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, 9, 0, 0, 0):
        raise ValueError("DOCUMENT_SANDBOX_UNAVAILABLE")
    library = ctypes.util.find_library("seccomp")
    if not library or libc.syscall(444, 0, 0, 1) < 3:
        raise ValueError("DOCUMENT_SANDBOX_UNAVAILABLE")
    rules = ctypes.c_uint64((1 << 15) - 1)
    descriptor = libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0)
    if descriptor < 0:
        raise ValueError("DOCUMENT_SANDBOX_UNAVAILABLE")

    class Rule(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("mask", ctypes.c_uint64), ("fd", ctypes.c_int)]

    def allow(path, mask):
        if not Path(path).exists():
            return
        fd = os.open(str(path), 0x200000 | 0x80000)
        try:
            item = Rule(mask, fd)
            if libc.syscall(445, descriptor, 1, ctypes.byref(item), 0):
                raise ValueError("DOCUMENT_SANDBOX_UNAVAILABLE")
        finally:
            os.close(fd)
    try:
        for path in ("/usr/lib", "/usr/local/lib", "/lib", "/lib64", "/usr/share/tesseract-ocr",
                     "/usr/share/fonts", "/usr/share/fontconfig", "/etc/fonts", "/var/cache/fontconfig"):
            allow(path, 1 | 4 | 8)
        for path in ("/etc/ld.so.cache", "/dev/null", "/dev/urandom"):
            allow(path, 4)
        for executable in ("pdfinfo", "pdftotext", "pdftoppm", "pdfimages", "tesseract"):
            allow("/usr/bin/" + executable, 1 | 4)
        allow(folder, ((1 << 15) - 1) & ~1)
        if libc.prctl(38, 1, 0, 0, 0) or libc.syscall(446, descriptor, 0):
            raise ValueError("DOCUMENT_SANDBOX_UNAVAILABLE")
    finally:
        os.close(descriptor)
    lib = ctypes.CDLL(library)
    lib.seccomp_init.argtypes, lib.seccomp_init.restype = [ctypes.c_uint32], ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = lib.seccomp_release.argtypes = [ctypes.c_void_p]
    context = lib.seccomp_init(0x7FFF0000)
    if not context:
        raise ValueError("DOCUMENT_SANDBOX_UNAVAILABLE")
    try:
        for name in ("socket", "socketpair", "connect", "bind", "listen", "accept", "accept4",
                     "ptrace", "process_vm_readv", "process_vm_writev", "mount", "umount2",
                     "unshare", "setns", "bpf", "keyctl", "open_by_handle_at", "io_uring_setup"):
            number = lib.seccomp_syscall_resolve_name(name.encode())
            if number >= 0 and lib.seccomp_rule_add(context, 0x00050001, number, 0):
                raise ValueError("DOCUMENT_SANDBOX_UNAVAILABLE")
        if lib.seccomp_load(context):
            raise ValueError("DOCUMENT_SANDBOX_UNAVAILABLE")
    finally:
        lib.seccomp_release(context)


def command(args, timeout=30):
    result = subprocess.run(args, capture_output=True, timeout=timeout, check=False)
    if result.returncode or len(result.stdout) > MAX_TEXT * 8:
        raise ValueError("DOCUMENT_ENGINE_FAILURE")
    return result.stdout.decode("utf-8", errors="strict")


def image_size(path):
    with path.open("rb") as stream:
        header = stream.read(24)
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return struct.unpack(">II", header[16:24])
        if header[:2] != b"\xff\xd8":
            raise ValueError("DOCUMENT_FORMAT_INVALID")
        stream.seek(2)
        while stream.tell() < 1024 * 1024:
            byte = stream.read(1)
            if byte != b"\xff":
                raise ValueError("DOCUMENT_FORMAT_INVALID")
            marker = stream.read(1)
            while marker == b"\xff":
                marker = stream.read(1)
            if not marker:
                break
            size = struct.unpack(">H", stream.read(2))[0]
            if marker in [bytes([n]) for n in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)]:
                _, height, width = struct.unpack(">BHH", stream.read(5))
                return width, height
            if size < 2:
                break
            stream.seek(size - 2, 1)
    raise ValueError("DOCUMENT_FORMAT_INVALID")


def ocr(path, page):
    width, height = image_size(path)
    if width < 1 or height < 1 or width * height > MAX_PIXELS:
        raise ValueError("DOCUMENT_RESOLUTION_LIMIT")
    raw = command(["/usr/bin/tesseract", str(path), "stdout", "-l", "eng+spa", "--psm", "6", "tsv"])
    groups: dict[tuple[str, ...], list[dict[str, str]]] = {}
    for item in csv.DictReader(io.StringIO(raw), delimiter="\t"):
        if item.get("level") != "5" or not item.get("text", "").strip():
            continue
        key = tuple(item[k] for k in ("block_num", "par_num", "line_num"))
        groups.setdefault(key, []).append(item)
    lines = []
    for words in groups.values():
        x1, y1 = min(int(w["left"]) for w in words), min(int(w["top"]) for w in words)
        x2 = max(int(w["left"]) + int(w["width"]) for w in words)
        y2 = max(int(w["top"]) + int(w["height"]) for w in words)
        lines.append({"text": " ".join(w["text"] for w in words), "page": page,
                      "region": [x1 / width, y1 / height, x2 / width, y2 / height],
                      "confidence": min(float(w["conf"]) for w in words), "method": "OCR"})
    return lines


def lines_from_file(source, folder, progress):
    if source.suffix != ".pdf":
        lines = ocr(source, 1)
        progress.write_text("100")
        return lines, 1
    info = command(["/usr/bin/pdfinfo", str(source)])
    match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
    if not match or not 1 <= int(match[1]) <= MAX_PAGES:
        raise ValueError("DOCUMENT_PAGE_LIMIT")
    count = int(match[1])
    if re.search(r"^Encrypted:\s+yes", info, re.MULTILINE):
        raise ValueError("DOCUMENT_ENCRYPTED")
    xml = command(["/usr/bin/pdftotext", "-bbox-layout", str(source), "-"])
    pages = ET.fromstring(xml).findall(".//{*}page")
    output: list[dict[str, Any]] = []
    for index in range(count):
        page_lines = []
        if index < len(pages):
            page = pages[index]
            width, height = float(page.attrib["width"]), float(page.attrib["height"])
            if width <= 0 or height <= 0 or max(width, height) > 14400:
                raise ValueError("DOCUMENT_RESOLUTION_LIMIT")
            for line in page.findall(".//{*}line"):
                words = line.findall("{*}word")
                if words:
                    page_lines.append({"text": " ".join(w.text or "" for w in words), "page": index + 1,
                        "region": [float(line.attrib["xMin"]) / width, float(line.attrib["yMin"]) / height,
                                   float(line.attrib["xMax"]) / width, float(line.attrib["yMax"]) / height],
                        "confidence": None, "method": "DIGITAL"})
        # Mixed pages retain digital text and additionally recognize embedded scans.
        images = command(["/usr/bin/pdfimages", "-f", str(index + 1), "-l", str(index + 1), "-list", str(source)])
        if not page_lines or re.search(r"^\s*\d+\s+\d+\s+image\s", images, re.MULTILINE):
            prefix = folder / f"page-{index + 1}"
            command(["/usr/bin/pdftoppm", "-f", str(index + 1), "-l", str(index + 1), "-singlefile",
                     "-scale-to", "4000", "-png", str(source), str(prefix)])
            known = {line["text"] for line in page_lines}
            page_lines += [line for line in ocr(prefix.with_suffix(".png"), index + 1) if line["text"] not in known]
            prefix.with_suffix(".png").unlink(missing_ok=True)
        output.extend(page_lines)
        if sum(len(line["text"]) for line in output) > MAX_TEXT:
            raise ValueError("DOCUMENT_TEXT_LIMIT")
        progress.write_text(str(int((index + 1) / count * 100)))
    return output, count


def validate_value(raw, field):
    if raw is None:
        return None, "MISSING" if field.get("required", True) else "VALID"
    value = raw.strip()
    try:
        kind = field["logical_type"]
        if kind == "DECIMAL":
            separator = field.get("decimal_separator", ".")
            if not re.fullmatch(r"[+-]?[0-9]+(?:" + re.escape(separator) + r"[0-9]+)?", value):
                raise ValueError()
            value = value.replace(separator, ".")
            if not Decimal(value).is_finite() or len(Decimal(value).as_tuple().digits) > 38:
                raise ValueError()
        elif kind == "INT64":
            if not re.fullmatch(r"[+-]?[0-9]+", value) or not -(2**63) <= int(value) < 2**63:
                raise ValueError()
        elif kind == "DATE":
            parsed = datetime.strptime(value, field.get("date_format", "%Y-%m-%d")).replace(tzinfo=UTC)
            value = parsed.date().isoformat()
        elif kind == "BOOLEAN" and value not in {"true", "false"}:
            raise ValueError()
        return value, "VALID"
    except (ValueError, InvalidOperation, OverflowError):
        return raw, "INVALID"  # Never erase an invalid observation or replace it with zero.


def extract(lines, definition):
    fields = {}
    for field in definition["fields"]:
        matches = []
        for line in lines:
            text = line["text"]
            label = field.get("label", "")
            if label:
                found = re.search(re.escape(label) + r"\s*[:=]?\s*", text, re.IGNORECASE)
                if not found:
                    continue
                text = text[found.end():]
            match = re.search(field["pattern"], text)
            if match:
                matches.append((match[1], line))
        distinct = {m[0] for m in matches}
        raw, origin = matches[0] if matches else (None, None)
        value, validity = validate_value(raw, field)
        fields[field["name"]] = {"value": value, "observed": raw,
            "validity": "AMBIGUOUS" if len(distinct) > 1 else validity,
            "confidence": origin["confidence"] if origin else None,
            "origin": {k: origin[k] for k in ("page", "region", "method")} if origin else None,
            "reviewed": False, "alternatives": sorted(distinct)[:20] if len(distinct) > 1 else []}
    tables = {}
    table_unparsed = {}
    for table in definition["tables"]:
        rows = []
        rejected = []
        prefix = table.get("row_label", "")
        if not prefix:
            literal = re.match(r"\^?([A-Za-z][A-Za-z0-9 ]+)\(", table["row_pattern"])
            prefix = literal[1].strip() if literal else ""
        for line in lines:
            match = re.fullmatch(table["row_pattern"], line["text"])
            if not match:
                if prefix and line["text"].startswith(prefix):
                    rejected.append({k: line[k] for k in ("text", "page", "region", "method")})
                continue
            row = {}
            for field in table["fields"]:
                raw = match[field["name"]]
                value, validity = validate_value(raw, field)
                row[field["name"]] = {"value": value, "observed": raw, "validity": validity,
                    "confidence": line["confidence"], "origin": {k: line[k] for k in ("page", "region", "method")},
                    "reviewed": False}
            rows.append(row)
            if len(rows) > 10000:
                raise ValueError("DOCUMENT_ROW_LIMIT")
        tables[table["name"]] = rows
        table_unparsed[table["name"]] = rejected
    period_field = definition.get("business_period_field")
    return {"fields": fields, "tables": tables, "pages": sorted({line["page"] for line in lines}),
            "lines": lines, "table_unparsed": table_unparsed,
            "business_period": {"field": period_field, **fields[period_field]} if period_field else None}


def main():
    import resource
    started = time.monotonic()
    folder = Path(sys.argv[1]).resolve()
    request = json.loads((folder / "request.json").read_text())
    source = folder / request["filename"]
    confine(folder)
    if request.get("preview_page") is not None:
        page = request["preview_page"]
        info = command(["/usr/bin/pdfinfo", str(source)])
        match = re.search(r"^Pages:\s+(\d+)", info, re.MULTILINE)
        if not match or not 1 <= page <= min(int(match[1]), MAX_PAGES):
            raise ValueError("DOCUMENT_PAGE_LIMIT")
        target = folder / "preview"
        command(["/usr/bin/pdftoppm", "-f", str(page), "-l", str(page), "-singlefile", "-scale-to", "1800", "-jpeg", str(source), str(target)])
        raw = target.with_suffix(".jpg").read_bytes()
        if len(raw) > 8 * 1024**2:
            raise ValueError("DOCUMENT_PREVIEW_LIMIT")
        (folder / "result.json").write_text(json.dumps({"preview": base64.b64encode(raw).decode(), "page": page}))
        return
    lines, pages = lines_from_file(source, folder, folder / "progress")
    if len(lines) > 100000:
        raise ValueError("DOCUMENT_TEXT_LIMIT")
    payload = extract(lines, request["definition"])
    models = {}
    for language in ("eng", "spa"):
        for path in Path("/usr/share/tesseract-ocr").glob(f"*/tessdata/{language}.traineddata"):
            models[language] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {"payload": payload, "engine": {"key": "POPPLER_TESSERACT_CPU_V1", "pages": pages,
        "poppler": subprocess.run(["/usr/bin/pdftotext", "-v"], capture_output=True, timeout=5, check=True).stderr.decode("utf-8").splitlines()[0],
        "tesseract": command(["/usr/bin/tesseract", "--version"]).splitlines()[0], "model_sha256": models,
        "resources": {"elapsed_seconds": round(time.monotonic() - started, 4),
                      "child_max_rss_kib": resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss,
                      "coordinator_max_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss}}}
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, subprocess.TimeoutExpired, ET.ParseError) as error:
        code = str(error) if isinstance(error, ValueError) and re.fullmatch(r"DOCUMENT_[A-Z_]+", str(error)) else "DOCUMENT_ENGINE_FAILURE"
        (Path(sys.argv[1]) / "result.json").write_text(json.dumps({"error": code}))
        sys.exit(1)
