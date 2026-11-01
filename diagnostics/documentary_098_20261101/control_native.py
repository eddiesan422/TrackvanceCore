"""Run the existing native PDF reader only; no OCR, models, DB or publication.

On Windows an explicit Poppler directory changes executable paths, not reader
logic. Linux without --poppler-bin uses the original /usr/bin paths unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def load_engine(worktree):
    root = Path(worktree) / "backend/src/trackvance"
    path = root / "document_engine.py"
    spec = importlib.util.spec_from_file_location("documentary_native_existing_engine", path)
    if spec is None or spec.loader is None:
        raise ValueError("EXISTING_NATIVE_READER_MISSING")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                    for name in ("document_engine.py", "document_template_builder_rules.py", "document_invoice_controls.py")}


def control(source, document_id, modality, worktree, *, poppler_bin=None):
    source = Path(source)
    result = {"document_id": document_id, "route": "CONTROL_DIGITAL_NATIVE",
              "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "semantic": None, "semantic_reason": "NATIVE_READER_RETURNS_LINES_WORDS_NOT_SEMANTIC_FIELDS",
              "operational_approval_measured": False, "models": 0, "OCR_calls": 0,
              "recognized_text": None, "recognized_text_scope": "FULL_DOCUMENT"}
    if str(modality).casefold() not in {"digital", "digitals", "d"}:
        result.update(status="UNSUPPORTED", reason="NOT_APPLICABLE_SCANNED_DOCUMENT_NATIVE_CONTROL",
                      metrics={"work_seconds": 0, "call_count": 0, "timing_kind": "NO_EXECUTION"})
        return result
    module, hashes = load_engine(worktree)
    original_command = module.command
    tools = {}
    if poppler_bin:
        directory = Path(poppler_bin)
        for name in ("pdfinfo", "pdftotext", "pdfimages"):
            path = directory / (name + (".exe" if sys.platform == "win32" else ""))
            if not path.is_file():
                raise ValueError("DECLARED_POPPLER_BINARY_MISSING:" + name)
            version = subprocess.run([str(path), "-v"], capture_output=True, timeout=10, check=False)
            tools[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                           "version": (version.stdout + version.stderr).decode("utf-8", errors="replace").splitlines()[0]}

        def command(args, timeout=30):
            if args[0] not in {"/usr/bin/pdfinfo", "/usr/bin/pdftotext", "/usr/bin/pdfimages"}:
                raise RuntimeError("NATIVE_CONTROL_UNEXPECTED_EXECUTABLE")
            args = [tools[Path(args[0]).name]["path"], *args[1:]]
            return original_command(args, timeout=timeout)
        module.command = command
    started = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="trackvance-doc-native-") as folder:
            page_geometry, gaps = {}, []
            lines, pages = module.lines_from_file(source, Path(folder), Path(folder) / "progress.json", page_geometry,
                                                 native_only=True, native_gaps=gaps)
        result.update(status="PARTIAL" if gaps else "COMPLETE", pages=pages, lines=lines,
                      recognized_text="\n".join(line["text"] for line in lines),
                      native_gaps=gaps, page_geometry=page_geometry,
                      recognition_inventory={"lines": len(lines), "words": sum(len(line.get("words", [])) for line in lines),
                                             "physical_tables": None, "table_support": "NOT_RETURNED_BY_EXISTING_NATIVE_READER"})
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        result.update(status="TIMEOUT" if isinstance(error, subprocess.TimeoutExpired) else "ERROR",
                      reason=type(error).__name__ + ":" + str(error), attribution=[{"layer": "FILE_RENDER_OR_NATIVE_READER", "cause": type(error).__name__}])
    result.update(metrics={"work_seconds": time.monotonic() - started, "call_count": 0, "timing_kind": "NEW_CPU_NATIVE_READ",
                           "RAM_sampled": None, "VRAM_sampled": None},
                  existing_native_reader_source_hashes=hashes, executable_transport_override=bool(poppler_bin), actual_tools=tools)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--document-id", required=True)
    parser.add_argument("--modality", required=True)
    parser.add_argument("--worktree", type=Path, required=True)
    parser.add_argument("--poppler-bin", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = control(args.source, args.document_id, args.modality, args.worktree, poppler_bin=args.poppler_bin)
    with args.output.open("xb") as stream:
        stream.write(canonical(result) + b"\n")
    print(json.dumps({"status": result["status"], "document_id": args.document_id, "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
