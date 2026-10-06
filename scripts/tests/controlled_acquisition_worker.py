"""Test-only one-shot barrier around the real acquisition reader and worker.

The production executable never imports this module. The test pauses after one
real batch has persisted, so cancellation and lease recovery do not depend on a
large fixture or machine speed. Heartbeats and all product checks remain active.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path


def main():
    project = os.environ.get("TRACKVANCE_CERTIFICATION_PROJECT", "")
    if not re.fullmatch(r"trackvance-v070-test-corrections-functional-[a-f0-9]{12}", project):
        raise ValueError("Controlled worker requires its owned functional test project")
    from trackvance.acquisition import FileBatchReader
    from trackvance.acquisition_worker import main as worker_main

    actual = FileBatchReader.__iter__
    arm, reached = Path("/tmp/trackvance-reader-arm"), Path("/tmp/trackvance-reader-reached")

    def controlled(reader):
        for batch in actual(reader):
            yield batch
            if arm.exists():
                arm.unlink()
                reached.write_text("REAL_BATCH_PERSISTED", encoding="utf-8")
                began = time.monotonic()
                try:
                    while reached.exists():
                        reader.check()
                        if time.monotonic() - began > 90:
                            raise TimeoutError("Functional reader barrier was not released")
                        time.sleep(0.05)
                finally:
                    reached.unlink(missing_ok=True)

    FileBatchReader.__iter__ = controlled
    worker_main()


if __name__ == "__main__":
    main()
