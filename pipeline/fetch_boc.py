"""
Fetch Bank of Canada series via the Valet API (https://www.bankofcanada.ca/valet).

A single generic fetcher, `fetch_boc_indicator`, serves every BoC series in the
indicator registry. Each registry entry supplies `boc_series` — a mapping of
output column name -> Valet series code (e.g. {"policy_rate": "V122530"}) — and
the fetcher pulls them all in one comma-joined request and writes a tidy wide CSV
(`date` + one column per series). The Bank of Canada permits reproduction of Valet
data with attribution; every chart carries a "Source: Bank of Canada" note.
"""

import requests
import pandas as pd
from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata
import logging

logger = logging.getLogger(__name__)

VALET_OBS = "https://www.bankofcanada.ca/valet/observations"


def fetch_boc_indicator(ind):
    """Generic Bank of Canada (Valet) fetch driven by a registry entry.

    Fetches every code in `ind.boc_series` in one request, reshapes to a wide
    [date, <cols...>] CSV, and writes it (plus its metadata sidecar) to
    `ind.out_path`. Returns the DataFrame, or None on empty/failed fetch (the
    pipeline driver then preserves any existing CSV — STALE fallback)."""
    logger.info(f"BoC indicator: {ind.id} ({ind.title})")
    if not ind.boc_series:
        logger.error(f"  {ind.id}: no boc_series mapping")
        return None
    cols = list(ind.boc_series.keys())
    codes = list(ind.boc_series.values())
    url = (f"{VALET_OBS}/{','.join(codes)}/json"
           f"?start_date={ind.start_period}-01-01")
    logger.info(f"  Fetching: {url}")
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
        obs = r.json().get("observations", [])
    except Exception as e:
        logger.error(f"  BoC request failed: {e}")
        return None
    if not obs:
        return None

    rows = []
    for o in obs:
        row = {"date": o.get("d")}
        for col, code in ind.boc_series.items():
            v = o.get(code, {}).get("v")
            row[col] = pd.to_numeric(v, errors="coerce") if v not in (None, "") else None
        rows.append(row)
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    df = (df.dropna(subset=["date"])
            .dropna(how="all", subset=cols)
            .sort_values("date").reset_index(drop=True))
    if df.empty:
        return None

    out_path = ind.out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="date", source="Bank of Canada",
        source_table=ind.source_table or "Bank of Canada (Valet API)",
        frequency=ind.frequency, unit=ind.unit,
        transformations=[f"BoC Valet series {', '.join(codes)} -> wide CSV"])
    logger.info(f"  saved {len(df)} rows -> {out_path.name}")
    return df
