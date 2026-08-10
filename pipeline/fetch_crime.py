"""
Police-reported crime by type — Statistics Canada incident-based crime statistics
(UCR), table 35-10-0177-01 ("Incident-based crime statistics, by detailed
violations, Canada, provinces, territories, Census Metropolitan Areas").

This ONE table powers most of the everyday-crime expansion: it carries ~200
violation codes (motor-vehicle theft, break & enter, shoplifting, fraud, robbery,
mischief, …) × Canada + every province/territory + ~41 CMAs × 1998–2024, with
actual incidents, rate per 100,000, year-over-year %-change, and clearances —
i.e. levels, trends, a CMA map, and a "how often is it solved?" view all from one
fetch. (The existing Crime Severity Index series come from the companion
35-10-0026-01; CSI is severity-weighted/indexed and must never share an axis with
these per-100k rates.)

The CSV-zip is ~95 MB compressed / ~1.5 GB uncompressed, so we stream it to a
cache and **chunk-read** only the columns + rows we need (curated violations,
four statistics), then reshape to a compact tidy CSV (~20k rows). Annual data
(StatCan releases police-reported crime each July), so the weekly pipeline will
simply re-confirm the same numbers most of the year; the STALE fallback covers a
transient download failure.
"""

import io
import re
import zipfile
import logging
from pathlib import Path

import pandas as pd
import requests

from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata

logger = logging.getLogger(__name__)

CRIME_TABLE = "35-10-0177-01"
_ZIP_URL = "https://www150.statcan.gc.ca/n1/tbl/csv/35100177-eng.zip"
_CACHE = Path("/tmp/datacan_cache")

# Curated everyday-crime violations: bracketed UCR code -> display label, in the
# order they should appear in the legend (property/everyday first, then the
# aggregates used as context, then two violent references for the clearance view).
# Codes verified against the table's Violations dimension.
CURATED = {
    "[2160]": "Fraud",
    "[2165]": "Identity theft",
    "[2143]": "Shoplifting",                    # Shoplifting $5,000 or under
    "[220]":  "Motor vehicle theft",            # Total theft of motor vehicle
    "[2120]": "Breaking & entering",
    "[2142]": "Theft from a vehicle",           # Theft $5,000 or under from a motor vehicle
    "[240]":  "Theft under $5,000",             # Total theft under $5,000 (non-motor vehicle)
    "[250]":  "Mischief",                       # Total mischief (vandalism)
    "[160]":  "Robbery",                        # Total robbery
    "[200]":  "Property crime (total)",
    "[100]":  "Violent crime (total)",
    "[50]":   "All Criminal Code (excl. traffic)",
    # Assault tiers (Criminal Code ss. 266–268), relabelled for a general audience.
    "[1430]": "Common assault",                 # level 1 — no weapon, no serious injury
    "[1420]": "Assault with a weapon or injury",  # level 2
    "[1410]": "Aggravated assault",             # level 3 — wounds/maims/endangers life
    "[110]":  "Homicide",
}

# The four statistics we keep, mapped to tidy output column names.
STATS = {
    "Rate per 100,000 population": "rate",
    "Actual incidents": "incidents",
    "Total cleared": "cleared",
    "Percentage change in rate": "pct_change",
}


def _vcode(violation):
    """Trailing bracketed code of a Violations label, e.g. '… [220]' -> '[220]'."""
    i = violation.rfind("[")
    return violation[i:] if i != -1 else None


def _classify_geo(geo):
    """(geo_level, cmauid, short_name) for a GEO label, or None to drop the row.

    national  -> 'Canada'
    province  -> 2-digit bracket code, e.g. 'Ontario [35]'
    cma       -> 5-digit bracket code, e.g. 'Toronto, Ontario [35535]' (cmauid = last 3,
                 matching cma_2021.geojson). The combined Ottawa-Gatineau row
                 '[24505/35505]' is kept (cmauid 505); its single-province "part"
                 rows and the Canadian Forces Military Police pseudo-CMA are dropped.
    """
    if geo == "Canada":
        return "national", "", "Canada"
    if "part" in geo.lower() or "Military Police" in geo:
        return None
    m = re.search(r"\[([\d/]+)\]", geo)
    if not m:
        return None
    code = m.group(1).split("/")[0]          # first code if a combined CMA
    name = geo.split(",")[0].split("[")[0].strip()
    if len(code) == 2:
        return "province", "", name
    if len(code) == 5:
        return "cma", code[-3:], name
    return None


def _load_table_chunks():
    """Stream the crime zip to a cache and yield filtered DataFrame chunks.

    Only the dimension + value columns are read, and each chunk is pre-filtered to
    the curated violations and the four statistics, so memory stays bounded despite
    the 1.5 GB uncompressed CSV."""
    _CACHE.mkdir(parents=True, exist_ok=True)
    zip_path = _CACHE / "35100177-eng.zip"
    if not (zip_path.exists() and zip_path.stat().st_size > 0):
        logger.info(f"  downloading {CRIME_TABLE} (~95 MB, one-time per cache) ...")
        with requests.get(_ZIP_URL, stream=True, timeout=1800) as r:
            r.raise_for_status()
            with open(zip_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
    with zipfile.ZipFile(zip_path) as z:
        csv_name = next(n for n in z.namelist()
                        if n.endswith(".csv") and "MetaData" not in n)
        with z.open(csv_name) as f:
            reader = pd.read_csv(
                f, usecols=["REF_DATE", "GEO", "Violations", "Statistics", "VALUE"],
                dtype=str, chunksize=200_000)
            for ch in reader:
                ch = ch[ch["Statistics"].isin(STATS)]
                ch = ch[ch["Violations"].map(_vcode).isin(CURATED)]
                if len(ch):
                    yield ch


def fetch_crime_by_type():
    """Tidy police-reported crime by type → data/wellbeing/statcan_crime_by_type.csv.

    Long format: one row per (year, geography, violation) with rate, incidents,
    cleared and the published year-over-year %-change. Drives the national/
    provincial time series (geography dropdown), the rising-vs-falling view, the
    clearance-rate chart, and the by-CMA choropleth.
    """
    logger.info(f"Fetching StatCan crime by type ({CRIME_TABLE})...")
    try:
        parts = list(_load_table_chunks())
    except Exception as e:
        logger.error(f"  failed to fetch/parse {CRIME_TABLE}: {e}")
        return None
    if not parts:
        return None
    raw = pd.concat(parts, ignore_index=True)

    cls = raw["GEO"].map(_classify_geo)
    keep = cls.notna()
    raw = raw[keep].copy()
    raw[["geo_level", "cmauid", "name"]] = pd.DataFrame(
        cls[keep].tolist(), index=raw.index)
    raw["vcode"] = raw["Violations"].map(_vcode)
    raw["violation"] = raw["vcode"].map(CURATED)
    raw["year"] = pd.to_numeric(raw["REF_DATE"], errors="coerce").astype("Int64")
    raw["VALUE"] = pd.to_numeric(raw["VALUE"], errors="coerce")
    raw["stat"] = raw["Statistics"].map(STATS)

    # Pivot the four statistics into columns.
    idx = ["year", "GEO", "geo_level", "cmauid", "name", "vcode", "violation"]
    wide = (raw.pivot_table(index=idx, columns="stat", values="VALUE", aggfunc="first")
               .reset_index()
               .rename(columns={"GEO": "geography"}))
    for col in ("rate", "incidents", "cleared", "pct_change"):
        if col not in wide.columns:
            wide[col] = pd.NA

    out = wide[["year", "geography", "geo_level", "cmauid", "name", "violation",
                "vcode", "rate", "incidents", "cleared", "pct_change"]]
    out = (out.dropna(subset=["year"])
              .sort_values(["geo_level", "geography", "violation", "year"])
              .reset_index(drop=True))
    if out.empty:
        return None

    out_path = DATA_DIR / "wellbeing" / "statcan_crime_by_type.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    maxy = int(out["year"].max())
    save_metadata(out_path, df=out, latest_observation_date=str(maxy),
        source="Statistics Canada",
        source_table=f"Statistics Canada {CRIME_TABLE}",
        frequency="annual", unit="rate per 100,000 population; counts; %",
        transformations=[
            "chunk-read the incident-based crime table; kept curated violations "
            "(motor-vehicle theft, break & enter, shoplifting, fraud, robbery, "
            "mischief, theft, plus property/violent/all-CC aggregates and homicide/"
            "assault references); Canada + provinces/territories + CMAs; pivoted "
            "rate / actual incidents / total cleared / %-change into columns; "
            "cmauid = last 3 digits of the CMA GEO code (joins cma_2021.geojson)."])
    logger.info(f"  saved {len(out)} rows ({maxy}) -> {out_path.name}")
    return out


# Hate-crime tables are tiny (annual, ~15–70 KB) — read whole, no chunking.
HATE_GEO_TABLE = "35-10-0191-01"    # incidents + rate by geography (Canada + CMA), 2014–
HATE_MOTIV_TABLE = "35-10-0066-01"  # by detailed type of motivation (Canada selected svcs), 2012–


def fetch_hate_crime():
    """Police-reported hate crime → two compact CSVs in data/wellbeing/:
      statcan_hate_crime.csv             national incidents + rate per 100k, 2014–
      statcan_hate_crime_motivation.csv  by type of motivation (Canada, selected police
                                         services), 2012– — the group targeted
                                         (race/religion/sexual orientation + sub-groups).

    The national series is the headline (hate crime ~tripled since 2014); the motivation
    table is the 'who is targeted' breakdown. Caveats baked into the page: police-reported
    figures **undercount** (most hate crime is never reported), and the motivation table
    covers 'selected police services' (~most, not all of Canada). 35-10-0191-01 also has
    CMA-level rates, deliberately not extracted — annual counts per metro are too small to
    map reliably."""
    from pipeline.fetch_statcan import _get_table
    logger.info("Fetching StatCan hate crime (35-10-0191-01 + 35-10-0066-01)...")
    out_dir = DATA_DIR / "wellbeing"
    out_dir.mkdir(parents=True, exist_ok=True)
    first = None

    try:
        geo = _get_table(HATE_GEO_TABLE)
        nat = geo[geo["GEO"] == "Total police-reported hate crime"].copy()
        nat["VALUE"] = pd.to_numeric(nat["VALUE"], errors="coerce")
        piv = nat.pivot_table(index="REF_DATE", columns="Statistics", values="VALUE", aggfunc="first")
        out = (pd.DataFrame({"year": piv.index.astype(int),
                             "incidents": piv.get("Number of hate crime incidents"),
                             "rate": piv.get("Rate per 100,000 population")})
               .dropna(subset=["rate"]).sort_values("year").reset_index(drop=True))
        p = out_dir / "statcan_hate_crime.csv"
        out.to_csv(p, index=False)
        save_metadata(p, df=out, latest_observation_date=str(int(out["year"].max())),
            source="Statistics Canada", source_table=f"Statistics Canada {HATE_GEO_TABLE}",
            frequency="annual", unit="incidents; rate per 100,000 population",
            transformations=["national 'Total police-reported hate crime': incidents + rate over time"])
        logger.info(f"  saved {len(out)} years -> {p.name}")
        first = out
    except Exception as e:
        logger.error(f"  hate-crime national failed: {e}")

    try:
        mot = _get_table(HATE_MOTIV_TABLE)
        ca = mot[mot["GEO"] == "Canada, selected police services"].copy()
        ca["VALUE"] = pd.to_numeric(ca["VALUE"], errors="coerce")
        out2 = (ca.rename(columns={"Type of motivation": "motivation", "VALUE": "count"})
                  .assign(year=lambda x: x["REF_DATE"].astype(int))
                  .dropna(subset=["count"])[["year", "motivation", "count"]]
                  .sort_values(["motivation", "year"]).reset_index(drop=True))
        p2 = out_dir / "statcan_hate_crime_motivation.csv"
        out2.to_csv(p2, index=False)
        save_metadata(p2, df=out2, latest_observation_date=str(int(out2["year"].max())),
            source="Statistics Canada", source_table=f"Statistics Canada {HATE_MOTIV_TABLE}",
            frequency="annual", unit="number of incidents",
            transformations=["Canada (selected police services), by type of motivation, over time"])
        logger.info(f"  saved {len(out2)} rows -> {p2.name}")
        first = first if first is not None else out2
    except Exception as e:
        logger.error(f"  hate-crime motivation failed: {e}")

    return first


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fetch_crime_by_type()
    fetch_hate_crime()
