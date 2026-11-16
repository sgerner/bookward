"""Date parsing shared by reading-history signals."""

from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime


def read_day(value: object) -> date | None:
    """Parse supported read dates as UTC days; leave unknown dates neutral."""
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        return value
    elif isinstance(value, str):
        value = value.strip()
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(value, "%Y/%m/%d")
            except ValueError:
                try:
                    parsed = parsedate_to_datetime(value)
                except (TypeError, ValueError, IndexError, OverflowError):
                    return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.astimezone(timezone.utc).date()
    except OverflowError:
        return None
