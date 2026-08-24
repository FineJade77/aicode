"""Parse incoming timestamps.

Wire timestamps are ISO-8601 with an explicit offset. This layer is the boundary
that normalises them to UTC; everything downstream may assume UTC.
"""

from datetime import datetime, timezone


def parse_timestamp(raw: str) -> datetime:
    return datetime.fromisoformat(raw).astimezone(timezone.utc)
