"""Validated, effective bounds for acquisition, independent of legacy HTTP limits."""

import os
from dataclasses import asdict, dataclass


def _integer(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        raise RuntimeError(f"{name} debe ser un entero.") from None
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} debe estar entre {minimum} y {maximum}.")
    return value


@dataclass(frozen=True)
class AcquisitionLimits:
    # A batch is capped by BOTH records and observed UTF-8 bytes. No inferred
    # percentage or whole-result materialization is implied by these budgets.
    max_upload_bytes: int = 1024 * 1024 * 1024
    max_rows: int = 5_000_000
    max_observed_bytes: int = 2 * 1024 * 1024 * 1024
    batch_rows: int = 5_000
    batch_bytes: int = 8 * 1024 * 1024
    bounded_format_bytes: int = 10 * 1024 * 1024
    bounded_format_rows: int = 100_000
    memory_bytes: int = 256 * 1024 * 1024
    min_free_bytes: int = 512 * 1024 * 1024
    timeout_seconds: int = 1_800
    upload_ttl_seconds: int = 86_400

    @classmethod
    def configured(cls) -> "AcquisitionLimits":
        return cls(
            max_upload_bytes=_integer("TRACKVANCE_ACQUISITION_MAX_UPLOAD_BYTES", 1024**3, 1024, 5 * 1024**3),
            max_rows=_integer("TRACKVANCE_ACQUISITION_MAX_ROWS", 5_000_000, 1, 100_000_000),
            max_observed_bytes=_integer("TRACKVANCE_ACQUISITION_MAX_OBSERVED_BYTES", 2 * 1024**3, 1024, 20 * 1024**3),
            batch_rows=_integer("TRACKVANCE_ACQUISITION_BATCH_ROWS", 5_000, 1, 50_000),
            batch_bytes=_integer("TRACKVANCE_ACQUISITION_BATCH_BYTES", 8 * 1024**2, 65_536, 64 * 1024**2),
            bounded_format_bytes=_integer("TRACKVANCE_ACQUISITION_BOUNDED_FORMAT_BYTES", 10 * 1024**2, 1024, 64 * 1024**2),
            bounded_format_rows=_integer("TRACKVANCE_ACQUISITION_BOUNDED_FORMAT_ROWS", 100_000, 1, 100_000),
            memory_bytes=_integer("TRACKVANCE_ACQUISITION_MEMORY_BYTES", 256 * 1024**2, 64 * 1024**2, 4 * 1024**3),
            min_free_bytes=_integer("TRACKVANCE_ACQUISITION_MIN_FREE_BYTES", 512 * 1024**2, 16 * 1024**2, 20 * 1024**3),
            timeout_seconds=_integer("TRACKVANCE_ACQUISITION_TIMEOUT_SECONDS", 1_800, 30, 86_400),
            upload_ttl_seconds=_integer("TRACKVANCE_ACQUISITION_UPLOAD_TTL_SECONDS", 86_400, 300, 604_800),
        )

    def as_dict(self) -> dict[str, int]:
        return asdict(self)

