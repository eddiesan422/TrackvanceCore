"""Portable temporal grammar, independent of application services and engines."""
import re
from datetime import UTC, datetime

TIMESTAMP_PATTERN = (
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])$"
)


def exact_timestamp(value: object) -> datetime:
    """Validate before parsing: never invent an offset or lose fractional digits."""
    if not isinstance(value, str) or not re.fullmatch(TIMESTAMP_PATTERN, value):
        raise ValueError("TIMESTAMP requiere ISO con offset explícito y hasta seis cifras fraccionales.")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("TIMESTAMP requiere offset explícito.")
    # The portable engines represent the instant within Python's calendar.
    # Validate boundary offsets without normalizing the caller's representation.
    parsed.astimezone(UTC)
    return parsed
