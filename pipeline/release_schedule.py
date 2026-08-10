"""
Statistics Canada release-calendar lookup.

Statistics Canada publishes a machine-readable schedule of upcoming releases for
its key economic indicators — the same feed that drives the public "Release
schedule — major economic releases" calendar. We use it to stamp a dataset's
metadata sidecar with the *next* scheduled release date, so pages can show a
forward-looking "Next update: <date>" note beside a chart's source line.

This is the authoritative, non-hardcoded source: the dates come straight from
StatCan's own calendar and refresh whenever a new one is posted. The lookup runs
at *fetch* time (inside the pipeline), baking the date into the committed
sidecar, so rendering never needs network access (and works from file://). A
network or parse failure degrades gracefully — the sidecar simply omits the
field and the page shows no "Next update" line.
"""

import logging
from datetime import date, datetime

import requests

logger = logging.getLogger(__name__)

# StatCan key-indicators release calendar (JSON): every Daily release since 2012
# plus ~150 future-dated entries. Each item is
#   {"date": "YYYY-MM-DD HH:MM:SS", "type": ..., "title": ..., "description": <ref period>, "url": ...}
SCHEDULE_URL = ("https://www150.statcan.gc.ca/n1/dai-quo/ssi/homepage/"
                "schedule-key_indicators-eng.json")

# Map our indicator keys to the EXACT `title` strings used in the calendar feed.
# (Verified against the live feed, 2026-06.)
SCHEDULE_TITLES = {
    "cpi": "Consumer Price Index",
    "lfs": "Labour Force Survey",
    "gdp": "Gross domestic product, income and expenditure",
    "gdp_industry": "Gross domestic product by industry",
}

_cache = None


def _load_schedule():
    """Download and cache the calendar feed (one request per process)."""
    global _cache
    if _cache is None:
        r = requests.get(SCHEDULE_URL, timeout=60,
                         headers={"User-Agent": "CanadaObservatory/1.0"})
        r.raise_for_status()
        _cache = r.json()
    return _cache


def next_release_date(title, *, on=None):
    """Next scheduled release date (``"YYYY-MM-DD"``) for a calendar `title`,
    or ``None`` if unavailable.

    `title` is matched exactly against the feed's ``title`` field (e.g.
    ``"Consumer Price Index"`` — see ``SCHEDULE_TITLES``). The earliest entry
    strictly after `on` (default: today) is returned. Network/parse failures
    return ``None`` so callers degrade gracefully.
    """
    on = on or date.today()
    try:
        events = _load_schedule()
    except Exception as e:  # network, JSON, HTTP — all non-fatal here
        logger.warning(f"  release schedule unavailable ({e}); skipping next-release date")
        return None

    upcoming = []
    for ev in events:
        if ev.get("title") != title:
            continue
        d = (ev.get("date") or "")[:10]
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except ValueError:
            continue
        if dt > on:
            upcoming.append(dt)

    return min(upcoming).isoformat() if upcoming else None
