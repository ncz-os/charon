"""Timestamp parsing and formatting helpers for MPF."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


class MalformedTimestampError(ValueError):
    """Raised when a non-empty timestamp string fails to parse.

    Import callers treat this as a per-record failure: an envelope
    that supplies a malformed timestamp (rather than omitting it)
    is reporting corrupted lifecycle data, and silently substituting
    NOW() would mask the corruption. Persistence treats omitted
    timestamps with COALESCE(now), which is the correct absent-field
    behaviour; non-empty-but-malformed timestamps must NOT take that
    path.
    """


def _iso(value) -> Optional[str]:
    """Render a DB timestamp value as an RFC 3339 / ISO 8601 string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc).isoformat()
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    """Parse an MPF timestamp string.

    Distinguishes three states:
    - ``None`` / empty string: timestamp was omitted. Returns None so
      callers can apply the COALESCE(now) absent-field semantics.
    - non-empty, well-formed ISO 8601 string: returns the parsed
      datetime (may be naive or aware).
    - non-empty, malformed string: raises ``MalformedTimestampError``
      so the caller fails the affected record/sidecar rather than
      silently substituting NOW().
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise MalformedTimestampError(
            f"timestamp must be an ISO 8601 string, got {type(value).__name__}"
        )
    if not value:
        return None
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except (ValueError, TypeError) as exc:
        raise MalformedTimestampError(
            f"timestamp {value!r} is not a valid ISO 8601 string: {exc}"
        ) from exc


def _parse_iso_naive(value: Optional[str]) -> Optional[datetime]:
    """Parse a timestamp and return a UTC-aware value for DB writes.

    The helper name is retained for import-call compatibility from the
    pre-v5.0.3 TIMESTAMP schema. Postgres lifecycle columns are now
    TIMESTAMPTZ and asyncpg expects aware datetime values.

    Re-raises ``MalformedTimestampError`` for non-empty-but-malformed
    inputs (see ``_parse_iso`` docstring); only omits timestamps
    (None / empty string) silently fall back to COALESCE(now).
    """
    parsed = _parse_iso(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
