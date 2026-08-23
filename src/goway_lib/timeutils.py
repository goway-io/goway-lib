from datetime import UTC, datetime


def now_utc() -> datetime:
    return datetime.now(UTC)


def unix_ts(dt: datetime | None = None) -> int:
    return int((dt or now_utc()).timestamp())
