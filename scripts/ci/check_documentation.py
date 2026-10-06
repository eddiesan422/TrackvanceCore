"""Validate changed documentation and local link targets without product setup."""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

try:
    from .common import ROOT, EvidenceError, load_json, require, sha256
    from .select_suites import impact
except ImportError:
    from common import ROOT, EvidenceError, load_json, require, sha256
    from select_suites import impact

LINK = re.compile(r"!?\[[^\]\n]*\]\((<[^>\n]+>|[^\s)]+)(?:\s+[\"'][^\n]*?[\"'])?\)")
REFERENCE = re.compile(r"^\s{0,3}\[[^\]\n]+\]:\s*(<[^>\n]+>|\S+)", re.MULTILINE)


def prose(text: str) -> str:
    # Code examples may deliberately mention old paths or pseudo URLs.
    text = re.sub(r"^\s*(`{3,}|~{3,})[^\n]*\n.*?^\s*\1\s*$", "", text, flags=re.MULTILINE | re.DOTALL)
    return re.sub(r"`[^`\n]*`", "", text)


def verify_link(root: Path, document: Path, value: str) -> None:
    value = value.removeprefix("<").removesuffix(">")
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc:
        require(parsed.scheme in {"https", "http", "mailto", "codex", "app", "plugin"}
                or (not parsed.scheme and bool(parsed.netloc)), "UNSUPPORTED_DOCUMENT_LINK")
        return
    if not parsed.path:
        return  # Fragment identifiers are interpreted by each Markdown renderer.
    path = PurePosixPath(unquote(parsed.path))
    require("\\" not in str(path) and "\0" not in str(path), "INVALID_DOCUMENT_LINK")
    target = root / str(path).lstrip("/") if path.is_absolute() else document.parent / path
    require(target.resolve().is_relative_to(root.resolve()), "DOCUMENT_LINK_OUTSIDE_REPOSITORY")
    require(target.exists(), "MISSING_LOCAL_DOCUMENT_LINK")


def validate_documentation(root: Path, changed_files: list[str]) -> list[dict]:
    require(changed_files and len(set(changed_files)) == len(changed_files), "EMPTY_OR_DUPLICATE_DOCUMENT_SELECTION")
    records = []
    for name in sorted(changed_files):
        require(impact(name) == "documentation", "NON_DOCUMENT_IN_DOCUMENTATION_SELECTION")
        path = root / PurePosixPath(name)
        require(path.resolve().is_relative_to(root.resolve()) and not path.is_symlink(), "UNSAFE_DOCUMENT_PATH")
        if not path.exists():
            records.append({"path": name, "status": "DELETED"})
            continue
        require(path.is_file() and path.stat().st_size > 0, "EMPTY_OR_NONFILE_DOCUMENT")
        suffix = path.suffix.lower()
        if suffix in {".md", ".txt", ".rst", ".svg"} or path.name == "LICENSE":
            content = path.read_text(encoding="utf-8-sig")
            require("\0" not in content, "NUL_IN_DOCUMENT")
            if suffix == ".md":
                text = prose(content)
                for expression in (LINK, REFERENCE):
                    for match in expression.finditer(text):
                        verify_link(root, path, match[1])
        elif suffix == ".pdf":
            with path.open("rb") as stream:
                require(stream.read(8).startswith(b"%PDF-"), "INVALID_DOCUMENT_PDF")
                stream.seek(max(0, path.stat().st_size - 2048))
                require(b"%%EOF" in stream.read(), "INCOMPLETE_DOCUMENT_PDF")
        elif suffix == ".docx":
            with zipfile.ZipFile(path) as document:
                require({"[Content_Types].xml", "word/document.xml"} <= set(document.namelist()), "INVALID_DOCUMENT_DOCX")
                require(sum(info.file_size for info in document.infolist()) <= 64 * 1024**2, "EXCESSIVE_DOCUMENT_DOCX")
                require(document.testzip() is None, "CORRUPT_DOCUMENT_DOCX")
        records.append({"path": name, "status": "VALID", "sha256": sha256(path), "bytes": path.stat().st_size})
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--run-attempt", required=True)
    args = parser.parse_args()
    selection = load_json(args.selection)
    result = {"schema_version": 1, "kind": "DOCUMENTATION_EVIDENCE", "status": "FAIL",
              "source_sha": selection.get("source_sha"), "selection_sha256": sha256(args.selection),
              "ci": {"run_id": args.run_id, "run_attempt": args.run_attempt, "job_id": "documentation"}}
    try:
        require(selection.get("mode") == "docs" and selection.get("groups") == [], "NOT_DOCUMENTATION_SELECTION")
        actual = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                                text=True, check=True).stdout.strip()
        require(actual == selection.get("source_sha"), "DOCUMENTATION_SOURCE_SHA_MISMATCH")
        result["documents"] = validate_documentation(ROOT, [item["path"] for item in selection["changed_files"]])
        result["status"] = "PASS"
    except (EvidenceError, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        result["error_code"] = str(error) if isinstance(error, EvidenceError) else type(error).__name__
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "source_sha": result["source_sha"]}))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
