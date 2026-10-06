"""Docs checks run without dependencies and cannot silently excuse code changes."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from scripts.ci.check_documentation import validate_documentation
from scripts.ci.common import EvidenceError


def test_changed_document_links_target_existing_local_files_and_remote_official_docs(tmp_path):
    (tmp_path / "guide.md").write_text("# Guide\n", encoding="utf-8")
    (tmp_path / "README.md").write_text(
        "[Guide](guide.md#usage) [Official docs](https://github.com/eddiesan422/TrackvanceCore-docs)\n"
        "[Reference][guide]\n\n[guide]: guide.md\n"
        "```sh\n[Example](not-a-file.md)\n```\n", encoding="utf-8")
    results = validate_documentation(tmp_path, ["README.md", "docs/removed.md"])
    assert results[0]["status"] == "VALID" and results[0]["sha256"]
    assert results[1] == {"path": "docs/removed.md", "status": "DELETED"}


@pytest.mark.parametrize("content,code", [
    ("[Broken](missing.md)", "MISSING_LOCAL_DOCUMENT_LINK"),
    ("[Escape](../outside.md)", "DOCUMENT_LINK_OUTSIDE_REPOSITORY"),
    ("[Unsafe](file:///private/path)", "UNSUPPORTED_DOCUMENT_LINK"),
    ("\0binary", "NUL_IN_DOCUMENT")])
def test_invalid_doc_content_is_not_a_green_docs_run(tmp_path, content, code):
    (tmp_path / "README.md").write_text(content, encoding="utf-8")
    with pytest.raises(EvidenceError, match=code):
        validate_documentation(tmp_path, ["README.md"])


def test_documentation_selection_cannot_hide_product_or_generated_contract_changes(tmp_path):
    with pytest.raises(EvidenceError, match="NON_DOCUMENT_IN_DOCUMENTATION_SELECTION"):
        validate_documentation(tmp_path, ["scripts/ci/run_suite.py"])
    with pytest.raises(EvidenceError, match="NON_DOCUMENT_IN_DOCUMENTATION_SELECTION"):
        validate_documentation(tmp_path, ["docs/development/permission-matrix.md"])


def test_truncated_pdf_never_counts_as_valid_documentation(tmp_path):
    docs = tmp_path / "docs"; docs.mkdir()
    target = docs / "specification.pdf"; target.write_bytes(b"%PDF-1.7\ntruncated")
    with pytest.raises(EvidenceError, match="INCOMPLETE_DOCUMENT_PDF"):
        validate_documentation(tmp_path, ["docs/specification.pdf"])


def test_reference_link_missing_target_fails(tmp_path):
    (tmp_path / "README.md").write_text("[Missing][x]\n\n[x]: absent.md\n", encoding="utf-8")
    with pytest.raises(EvidenceError, match="MISSING_LOCAL_DOCUMENT_LINK"):
        validate_documentation(tmp_path, ["README.md"])
