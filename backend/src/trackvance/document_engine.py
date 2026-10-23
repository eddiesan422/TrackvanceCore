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
import importlib.util
import io
import json
import math
import os
import re
import struct
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, Inexact, InvalidOperation, localcontext
from pathlib import Path
from typing import Any

MAX_PAGES = 50
MAX_PIXELS = 20_000_000
MAX_TEXT = 2_000_000
ARITHMETIC_PRECISION_LIMIT = 4096

# -I intentionally excludes the script directory from sys.path. Load only this
# fixed sibling before confinement; the definition cannot choose a module/path.
_rules_spec = importlib.util.spec_from_file_location("document_visual_rules", Path(__file__).with_name("document_template_builder_rules.py"))
assert _rules_spec and _rules_spec.loader
_visual_rules = importlib.util.module_from_spec(_rules_spec)
_rules_spec.loader.exec_module(_visual_rules)
_invoice_spec = importlib.util.spec_from_file_location("document_invoice_controls", Path(__file__).with_name("document_invoice_controls.py"))
assert _invoice_spec and _invoice_spec.loader
_invoice_rules = importlib.util.module_from_spec(_invoice_spec)
sys.modules[_invoice_spec.name] = _invoice_rules
_invoice_spec.loader.exec_module(_invoice_rules)
PROCESSOR_HASHES = {name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                   for name in ("document_engine.py", "document_template_builder_rules.py", "document_invoice_controls.py")}


def confine(folder: Path):
    import resource
    if sys.platform != "linux":
        raise ValueError("DOCUMENT_SANDBOX_UNAVAILABLE")
    # strptime imports its standard-library dependencies lazily. Hosted Python
    # can keep those files outside the shared-library roots allowed below.
    # Load them before confinement without granting access to another directory.
    datetime.strptime("2000-01-01", "%Y-%m-%d").replace(tzinfo=UTC)
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


def merge_lines(primary, supplemental):
    """Add spatially new words, preserving the first source and its values.

    Different segmentation can split one physical text line into many runs.
    Comparing line strings would repeat its words and corrupt table columns.
    Equal text at distinct positions or on distinct pages remains distinct.
    """
    result = list(primary)
    indexed: dict[tuple[int, int, int], list[dict]] = {}

    def cells(word, page):
        left, top, right, bottom = word["region"]
        return ((page, x, y) for x in range(int(left * 50), int(right * 50) + 1)
                for y in range(int(top * 50), int(bottom * 50) + 1))

    def remember(word, page):
        for key in cells(word, page):
            indexed.setdefault(key, []).append(word)

    def overlaps(first, second):
        a, b = first["region"], second["region"]
        area = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
        smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
        return smaller > 0 and area / smaller >= .45

    for line in primary:
        for word in _visual_rules.words(line):
            remember(word, line["page"])
    for line in supplemental:
        selected = []
        for word in _visual_rules.words(line):
            if any(overlaps(word, previous) for key in cells(word, line["page"]) for previous in indexed.get(key, [])):
                continue
            selected.append(word)
            remember(word, line["page"])
        if selected:
            confidence = [word["confidence"] for word in selected if word.get("confidence") is not None]
            result.append({**line, "text": " ".join(word["text"] for word in selected), "words": selected,
                           "region": _visual_rules.box(selected), "confidence": min(confidence) if confidence else line.get("confidence")})
    return result


def ocr_pass(path, page, width, height, mode):
    raw = command(["/usr/bin/tesseract", str(path), "stdout", "-l", "eng+spa", "--psm", str(mode), "tsv"])
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
                      "confidence": min(float(w["conf"]) for w in words), "method": "OCR",
                      "words": [{"text": w["text"], "region": [int(w["left"]) / width, int(w["top"]) / height,
                          (int(w["left"]) + int(w["width"])) / width, (int(w["top"]) + int(w["height"])) / height],
                          "confidence": float(w["conf"])} for w in words]})
    return lines


def ocr(path, page):
    width, height = image_size(path)
    if width < 1 or height < 1 or width * height > MAX_PIXELS:
        raise ValueError("DOCUMENT_RESOLUTION_LIMIT")
    # The block pass retains short numeric cells. The sparse pass recovers
    # contrasting titles and table headings that the block pass can omit.
    # Neither pass is an oracle: retain original confidence and review policy.
    return merge_lines(ocr_pass(path, page, width, height, 6), ocr_pass(path, page, width, height, 11))


def lines_from_file(source, folder, progress, page_geometry=None, *, native_only=False, native_gaps=None):
    if source.suffix != ".pdf":
        lines = [] if native_only else ocr(source, 1)
        if native_only and native_gaps is not None:
            native_gaps.append(1)
        if page_geometry is not None:
            width, height = image_size(source)
            page_geometry["1"] = {"width": width, "height": height, "unit": "PIXEL"}
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
            if page_geometry is not None:
                page_geometry[str(index + 1)] = {"width": width, "height": height, "unit": "PDF_POINT"}
            for line in page.findall(".//{*}line"):
                words = line.findall("{*}word")
                if words:
                    page_lines.append({"text": " ".join(w.text or "" for w in words), "page": index + 1,
                        "region": [float(line.attrib["xMin"]) / width, float(line.attrib["yMin"]) / height,
                                   float(line.attrib["xMax"]) / width, float(line.attrib["yMax"]) / height],
                        "confidence": None, "method": "DIGITAL",
                        "words": [{"text": w.text or "", "region": [float(w.attrib["xMin"]) / width,
                            float(w.attrib["yMin"]) / height, float(w.attrib["xMax"]) / width,
                            float(w.attrib["yMax"]) / height], "confidence": None} for w in words]})
        # Mixed pages retain digital text and additionally recognize embedded scans.
        images = command(["/usr/bin/pdfimages", "-f", str(index + 1), "-l", str(index + 1), "-list", str(source)])
        if not page_lines or re.search(r"^\s*\d+\s+\d+\s+image\s", images, re.MULTILINE):
            if native_only:
                if native_gaps is not None:
                    native_gaps.append(index + 1)
                output.extend(page_lines)
                if sum(len(line["text"]) for line in output) > MAX_TEXT:
                    raise ValueError("DOCUMENT_TEXT_LIMIT")
                progress.write_text(str(int((index + 1) / count * 100)))
                continue
            prefix = folder / f"page-{index + 1}"
            command(["/usr/bin/pdftoppm", "-f", str(index + 1), "-l", str(index + 1), "-singlefile",
                     "-scale-to", "4000", "-png", str(source), str(prefix)])
            if page_geometry is not None and str(index + 1) not in page_geometry:
                width, height = image_size(prefix.with_suffix(".png"))
                page_geometry[str(index + 1)] = {"width": width, "height": height, "unit": "PIXEL"}
            page_lines = merge_lines(page_lines, ocr(prefix.with_suffix(".png"), index + 1))
            prefix.with_suffix(".png").unlink(missing_ok=True)
        output.extend(page_lines)
        if sum(len(line["text"]) for line in output) > MAX_TEXT:
            raise ValueError("DOCUMENT_TEXT_LIMIT")
        progress.write_text(str(int((index + 1) / count * 100)))
    return output, count


def validated_candidate_lines(lines, pages):
    """Accept bounded observations, never execution instructions or confidence claims."""
    if not isinstance(lines, list) or len(lines) > 100000:
        raise ValueError("DOCUMENT_CANDIDATE_INVALID")
    total = 0
    output = []
    for line in lines:
        if not isinstance(line, dict) or not isinstance(line.get("text"), str):
            raise ValueError("DOCUMENT_CANDIDATE_INVALID")  # noqa: TRY004 - stable child protocol
        page, region = line.get("page"), line.get("region")
        if (type(page) is not int or not 1 <= page <= pages or not isinstance(region, list) or len(region) != 4
            or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in region)
            or region[0] >= region[2] or region[1] >= region[3]):
            raise ValueError("DOCUMENT_CANDIDATE_INVALID")
        total += len(line["text"])
        if total > MAX_TEXT:
            raise ValueError("DOCUMENT_TEXT_LIMIT")
        words = line.get("words")
        if words is None:
            words = [{"text": line["text"], "region": region}]
        if not isinstance(words, list) or not words or len(words) > 10000:
            raise ValueError("DOCUMENT_CANDIDATE_INVALID")
        clean_words = []
        for word in words:
            box = word.get("region") if isinstance(word, dict) else None
            if (not isinstance(word, dict) or not isinstance(word.get("text"), str) or word["text"] not in line["text"]
                or not isinstance(box, list) or len(box) != 4
                or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in box)
                or box[0] >= box[2] or box[1] >= box[3]
                or box[0] < region[0] or box[1] < region[1] or box[2] > region[2] or box[3] > region[3]):
                raise ValueError("DOCUMENT_CANDIDATE_INVALID")
            clean_words.append({"text": word["text"], "region": box, "confidence": None})
        total += sum(len(word["text"]) for word in clean_words)
        if total > MAX_TEXT:
            raise ValueError("DOCUMENT_TEXT_LIMIT")
        # Only real provider-supplied word boxes or the honest whole block box.
        output.append({"text": line["text"], "page": page, "region": region, "confidence": None,
                       "method": "OCR", "words": clean_words})
    return output


def validate_value(raw, field):
    if raw is None:
        return None, "MISSING" if field.get("required", True) else "VALID"
    value = raw.strip()
    try:
        kind = field["logical_type"]
        if kind == "DECIMAL":
            separator = field.get("decimal_separator", ".")
            sign = ""
            if field.get("currency_symbols") and value.startswith(("-", "+")):
                sign, value = value[0], value[1:].strip()
            for symbol in sorted(field.get("currency_symbols", []), key=len, reverse=True):
                if value.startswith(symbol):
                    value = value[len(symbol):].strip()
                    break
            value = sign + value
            if not field.get("decimal_format_explicit", True) and re.fullmatch(r"[+-]?[0-9]{1,3}[.,][0-9]{3}", value):
                return raw, "AMBIGUOUS"
            thousands = field.get("thousands_separator")
            if field.get("numeric_fragments") and thousands:
                value = re.sub(r"(?<=[0-9])\s+(?=[0-9])", "", value)
            if thousands and thousands in value:
                integer, *fraction = value.split(separator)
                if len(fraction) > 1 or not re.fullmatch(r"[+-]?[0-9]{1,3}(?:" + re.escape(thousands) + r"[0-9]{3})+", integer):
                    raise ValueError()
                value = integer.replace(thousands, "") + (separator + fraction[0] if fraction else "")
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
            if parsed.strftime(field.get("date_format", "%Y-%m-%d")) != value:
                raise ValueError()
            value = parsed.date().isoformat()
        elif kind == "BOOLEAN" and value not in {"true", "false"}:
            raise ValueError()
        return value, "VALID"
    except (ValueError, InvalidOperation, OverflowError):
        return raw, "INVALID"  # Never erase an invalid observation or replace it with zero.


def arithmetic_checks(payload, definition):
    def require_precision(values, scale):
        # Exact alignment must fit before an addition, cancellation or comparison.
        # A scalar can be valid yet too widely scaled for bounded arithmetic.
        high = max(value.adjusted() for value in values)
        low = min(-scale, *(value.as_tuple().exponent for value in values))
        if high - low + len(str(len(values))) + 3 > ARITHMETIC_PRECISION_LIMIT:
            raise Inexact()

    checks = []
    for rule in definition.get("arithmetic", []):
        rows = payload["tables"].get(rule.get("table"), []) if rule.get("table") else [payload["fields"]]
        for index, row in enumerate(rows):
            result = {"name": rule["name"], "table": rule.get("table"), "row": index, "rounding": "ROUND_HALF_UP", "scale": rule["scale"]}
            try:
                required = [rule["result"], *rule["terms"], *rule.get("subtract", [])]
                if any(row.get(key, {}).get("validity") != "VALID" for key in required):
                    raise ValueError()
                numbers = [Decimal(str(row[key]["value"])) for key in rule["terms"]]
                deductions = [Decimal(str(row[key]["value"])) for key in rule.get("subtract", [])]
                observed = Decimal(str(row[rule["result"]]["value"]))
                tolerance = Decimal(rule["tolerance"])
                if any(not number.is_finite() for number in [*numbers, *deductions, observed, tolerance]):
                    raise ValueError()
                with localcontext() as context:
                    context.prec = ARITHMETIC_PRECISION_LIMIT
                    context.traps[Inexact] = True
                    require_precision([*numbers, *deductions, observed, tolerance], rule["scale"])
                    if sum(len(number.as_tuple().digits) for number in numbers) > ARITHMETIC_PRECISION_LIMIT:
                        raise Inexact()
                    expected = sum(numbers, Decimal(0)) if rule["operation"] == "SUM" else Decimal(1)
                    if rule["operation"] == "PRODUCT":
                        for number in numbers:
                            expected *= number
                    require_precision([expected, *deductions, observed, tolerance], rule["scale"])
                    expected -= sum(deductions, Decimal(0))
                    context.traps[Inexact] = False  # Only the declared final rounding is permitted.
                    expected = expected.quantize(Decimal(1).scaleb(-rule["scale"]), rounding=ROUND_HALF_UP)
                    context.traps[Inexact] = True
                    result.update(status="VALID" if abs(observed - expected) <= tolerance else "DISCREPANCY", expected=str(expected), observed=str(observed))
            except Inexact:
                result.update(status="NOT_EVALUATED", reason="ARITHMETIC_PRECISION_LIMIT")
            except (KeyError, ValueError, InvalidOperation):
                result["status"] = "NOT_EVALUATED"
            checks.append(result)
    checks.extend(_invoice_rules.invoice_checks(payload, definition))
    return checks


def cross_table_checks(payload, definition):
    """Exact declared detail-to-header totals, never a model-authored verdict."""
    checks = []
    for rule in definition.get("cross_totals", []):
        check = {"name": rule["name"], "table": rule["table"], "column": rule["column"],
            "header": rule["header"], "scale": rule["scale"], "rounding": "ROUND_HALF_UP"}
        try:
            rows = payload["tables"].get(rule["table"], [])
            header = payload["fields"][rule["header"]]
            if not rows or header["validity"] != "VALID" or any(row.get(rule["column"], {}).get("validity") != "VALID" for row in rows):
                raise ValueError()
            values = [Decimal(str(row[rule["column"]]["value"])) for row in rows]
            observed, tolerance = Decimal(str(header["value"])), Decimal(rule["tolerance"])
            if not all(value.is_finite() for value in [*values, observed, tolerance]):
                raise ValueError()
            high = max(value.adjusted() for value in [*values, observed, tolerance])
            low = min(-rule["scale"], *(value.as_tuple().exponent for value in [*values, observed, tolerance]))
            if high - low + len(str(len(values))) + 3 > ARITHMETIC_PRECISION_LIMIT:
                raise Inexact()
            with localcontext() as context:
                context.prec = ARITHMETIC_PRECISION_LIMIT
                context.traps[Inexact] = True
                expected = sum(values, Decimal(0))
                context.traps[Inexact] = False
                expected = expected.quantize(Decimal(1).scaleb(-rule["scale"]), rounding=ROUND_HALF_UP)
                context.traps[Inexact] = True
                check.update(status="VALID" if abs(expected - observed) <= tolerance else "DISCREPANCY",
                    expected=str(expected), observed=str(observed), rows=len(rows))
        except Inexact:
            check.update(status="NOT_EVALUATED", reason="ARITHMETIC_PRECISION_LIMIT")
        except (KeyError, ValueError, InvalidOperation):
            check["status"] = "NOT_EVALUATED"
        checks.append(check)
    return checks


def extract(lines, definition, page_geometry=None):
    if page_geometry is not None and not _visual_rules.valid_page_geometry(page_geometry, {line["page"] for line in lines}):
        raise ValueError("DOCUMENT_PAGE_GEOMETRY")
    fields = {}
    for field in definition["fields"]:
        matches = []
        if field.get("visual"):
            matches = _visual_rules.field_candidates(lines, field["visual"])
            for alternative in field.get("visual_alternatives", []):
                matches += _visual_rules.field_candidates(lines, alternative)
            unique = {}
            for raw_match, source in matches:
                unique[(raw_match, source["page"], tuple(source["region"]))] = (raw_match, source)
            matches = list(unique.values())
        for line in ([] if field.get("visual") else lines):
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
        ambiguous = len(distinct) > 1 or len(matches) > 1 and (field.get("visual") or {}).get("require_unique_origin", False)
        fields[field["name"]] = {"value": value, "observed": raw,
            "validity": "AMBIGUOUS" if ambiguous else validity,
            "recognition_status": "AMBIGUOUS" if ambiguous else "RECOGNIZED" if raw is not None else "EXPECTED_NOT_LOCATED" if field.get("required", True) else "NOT_APPLICABLE",
            "confidence": origin["confidence"] if origin else None,
            "origin": {k: origin[k] for k in ("page", "region", "method")} if origin else None,
            "reviewed": False, "alternatives": sorted(distinct)[:20] if ambiguous else []}
    tables = {}
    table_unparsed = {}
    for table in definition["tables"]:
        rows = []
        rejected = []
        if table.get("visual"):
            header_values = [fields[field["name"]]["value"] for field in definition["fields"]
                             if field.get("logical_type", "STRING") == "STRING" and fields[field["name"]]["validity"] == "VALID"
                             and fields[field["name"]]["origin"] is not None]
            visual_rows, rejected = _visual_rules.table_candidates(lines, table["visual"], table["fields"], header_values,
                validate_value, page_geometry, table_name=table["name"], definition=definition)
            for values in visual_rows:
                row = {}
                for field, (raw, source) in zip(table["fields"], values, strict=True):
                    value, validity = validate_value(raw, field)
                    if source is None:
                        value, validity = None, "MISSING"
                    row[field["name"]] = {"value": value, "observed": raw, "validity": validity,
                        "confidence": source["confidence"] if source else None,
                        "origin": {k: source[k] for k in ("page", "region", "method", "continuation_origins") if k in source} if source else None,
                        "reviewed": False}
                rows.append(row)
            tables[table["name"]], table_unparsed[table["name"]] = rows, rejected
            continue
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
    payload = {"fields": fields, "tables": tables, "pages": sorted({line["page"] for line in lines}),
            "lines": lines, "table_unparsed": table_unparsed,
            "business_period": {"field": period_field, **fields[period_field]} if period_field else None}
    if page_geometry is not None:
        payload["page_geometry"] = {page: dict(size) for page, size in page_geometry.items()}
    if definition.get("arithmetic") or definition.get("invoice_controls"):
        payload["arithmetic"] = arithmetic_checks(payload, definition)
    if definition.get("cross_totals"):
        payload["cross_totals"] = cross_table_checks(payload, definition)
    return payload


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
    page_geometry: dict[str, Any] = {}
    native_gaps: list[int] = []
    native_only = request.get("native_only") is True
    lines, pages = lines_from_file(source, folder, folder / "progress", page_geometry,
                                  native_only=native_only, native_gaps=native_gaps)
    if request.get("candidate_lines") is not None:
        if not native_only:
            raise ValueError("DOCUMENT_CANDIDATE_INVALID")
        # A layout block can contain several real table rows sharing its rectangle.
        # Spatial deduplication would discard those distinct observations. Retain
        # every candidate, deduplicating only identical native observations.
        native_observations = {(line["page"], line["text"], tuple(line["region"])) for line in lines}
        lines.extend(line for line in validated_candidate_lines(request["candidate_lines"], pages)
                     if (line["page"], line["text"], tuple(line["region"])) not in native_observations)
    if len(lines) > 100000:
        raise ValueError("DOCUMENT_TEXT_LIMIT")
    payload = extract(lines, request["definition"], page_geometry)
    if native_only:
        payload["native_incomplete_pages"] = native_gaps
    models = {}
    for language in ("eng", "spa"):
        for path in Path("/usr/share/tesseract-ocr").glob(f"*/tessdata/{language}.traineddata"):
            models[language] = hashlib.sha256(path.read_bytes()).hexdigest()
    result = {"payload": payload, "engine": {"key": "POPPLER_TESSERACT_CPU_V1", "pages": pages, "processing_sha256": PROCESSOR_HASHES,
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
