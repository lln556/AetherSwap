"""Convert Steam's date-only sale labels into observable local-day facts.

Steam's market history stamps every row with the *Pacific* calendar day.  That
label is authoritative evidence of which Pacific day the item sold on, but it
is not the calendar day this application shows.  Reconciling the label with
the moments we actually observed yields a *local* day only when the window
admits exactly one; when it admits two, the sale is reported as ambiguous
rather than guessed.

Both observed bounds are facts, not heuristics: submission comes from our own
listing request (the sale cannot precede it), and detection is the moment a
poll straight from Steam first showed the item sold (the sale cannot follow
it).  Therefore a label that disagrees with our own record produces no local
day at all.
"""
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

SALE_TZ = ZoneInfo("America/Los_Angeles")
LOCAL_TZ = ZoneInfo("Asia/Shanghai")

# An instant just before the exclusive end of a window, so day arithmetic
# never spills into the next calendar day.
_LAST_INSTANT = 1e-6


def pacific_day_window(day_label):
    """Return the half-open UTC interval covered by one Pacific calendar day."""
    try:
        day = date.fromisoformat(str(day_label))
    except (TypeError, ValueError):
        return None
    start = datetime.combine(day, time.min, SALE_TZ)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


def local_sale_window(sold_day, *, submitted_at=None, detected_at=None):
    """Observed interval that contains the sale, or None if it cannot exist.

    ``submitted_at`` bounds the early side, ``detected_at`` the late side.
    A non-positive intersection means the Steam label and our own record
    contradict each other, so no local day is derived from it.
    """
    label_window = pacific_day_window(sold_day)
    if not label_window:
        return None
    start, end = label_window
    if isinstance(submitted_at, (int, float)) and submitted_at > 0:
        start = max(start, submitted_at)
    if isinstance(detected_at, (int, float)) and detected_at > 0:
        end = min(end, detected_at)
    return None if start >= end else (start, end)


def unique_local_day(sold_day, *, submitted_at=None, detected_at=None):
    """Local day the sale provably happened on, else None.

    Exactly one local calendar day must span the whole window; anything else
    is left unknown so callers never report an invented date.
    """
    window = local_sale_window(sold_day, submitted_at=submitted_at, detected_at=detected_at)
    if not window:
        return None
    first, last = _local_day_bounds(window)
    return first.isoformat() if first == last else None


def local_day_bounds(sold_day, *, submitted_at=None, detected_at=None):
    """First and last local day the sale could have happened on.

    A range-filter must use the *last* bound: hiding a sale that might fall
    inside the requested period loses a real record, whereas keeping a sale
    that turns out to sit one day before it only widens the result.
    """
    window = local_sale_window(sold_day, submitted_at=submitted_at, detected_at=detected_at)
    if not window:
        return None
    first, last = _local_day_bounds(window)
    return first.isoformat(), last.isoformat()


def _local_day(timestamp):
    return datetime.fromtimestamp(timestamp, LOCAL_TZ).date()


def _local_day_bounds(window):
    start, end = window
    return (_local_day(start), _local_day(end - _LAST_INSTANT))
