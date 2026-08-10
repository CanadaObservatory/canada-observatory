"""
Air-quality custom fetchers (the Environment > Air Quality page).

Two Open Government Licence – Canada sources from Environment and Climate Change
Canada, both annual:

* **CESI air-quality indicators** — national, population-weighted concentration
  trends derived from the NAPS monitoring network. This is the analysis-ready
  version of NAPS: ECCC/StatCan have already done the population-weighting and
  validation, so we ingest a clean year-stamped CSV (same idiom as the CESI GHG
  series in fetch_environment.py). The file reports each pollutant as a
  "percentage change from 2009 level"; we convert to an index (2009 = 100). It
  carries both the *average* concentration and a *peak* percentile for each
  pollutant — the PM2.5 peak drives the wildfire-smoke chart.
  Year-stamped URL → bump CESI_AQ_RELEASE each year (data lags ~3 years).

* **APEI** — Canada's Air Pollutant Emissions Inventory: national emissions of
  the criteria air contaminants (SOx, NOx, VOC, CO, PM2.5, NH3), 1990–. These
  explain *why* concentrations fell (e.g. the acid-rain-era SOx controls). The
  bulk cube is filtered to the national GRAND TOTAL (REGION='CA').
"""

import io
import requests
import pandas as pd
from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (CanObs pipeline; +https://canadaobservatory.ca)"}


# ---------------------------------------------------------------------------
# CESI air-quality concentrations (population-weighted, indexed to 2009 = 100)
# ---------------------------------------------------------------------------
CESI_AQ_RELEASE = "2026"   # CESI release year; bump each year (~Feb)
CESI_AQ_URL = ("https://www.canada.ca/content/dam/eccc/documents/csv/cesindicators/"
               f"air-quality/{CESI_AQ_RELEASE}/EN/1-national-indexed-{CESI_AQ_RELEASE}-en.csv")

# Wide indexed-file column (sans the "(percentage change from 2009 level)" suffix)
# -> (pollutant, metric). "average" = mean concentration; "peak" = the high-end
# percentile (98th/99th/4th-highest) — the PM2.5 peak is the wildfire-smoke signal.
_CESI_COLS = {
    "PM2.5 average concentration": ("PM2.5", "average"),
    "PM2.5 average peak (98th percentile) 24-hour concentration": ("PM2.5", "peak"),
    "O3 average 8-hour concentration": ("O3", "average"),
    "O3 average peak (4th highest) 8-hour concentration": ("O3", "peak"),
    "NO2 average concentration": ("NO2", "average"),
    "NO2 average peak (98th percentile) 1-hour concentration": ("NO2", "peak"),
    "SO2 average concentration": ("SO2", "average"),
    "SO2 average peak (99th percentile) 1-hour concentration": ("SO2", "peak"),
    "VOC average concentration": ("VOC", "average"),
}


def fetch_cesi_air_quality():
    """National air-pollutant concentration trends (2009–), population-weighted,
    from ECCC's CESI air-quality indicator (built on NAPS). Output tidy long:
    year, pollutant, metric, index (2009 = 100)."""
    logger.info("Fetching ECCC CESI air-quality concentrations...")
    try:
        r = requests.get(CESI_AQ_URL, headers=_UA, timeout=60)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"  CESI air-quality fetch failed: {e}")
        return None
    # title row + blank row precede the header (skiprows=2); file is latin-1.
    df = pd.read_csv(io.StringIO(r.content.decode("latin-1")), skiprows=2)
    df = df.rename(columns={df.columns[0]: "year"})

    def _base(c):
        return str(c).split("(percentage change")[0].strip()

    df = df.rename(columns={c: _base(c) for c in df.columns if c != "year"})
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df = df.dropna(subset=["year"])
    df["year"] = df["year"].astype(int)

    rows = []
    for col, (pol, metric) in _CESI_COLS.items():
        if col not in df.columns:
            logger.warning(f"  CESI column not found: {col!r}")
            continue
        vals = pd.to_numeric(df[col], errors="coerce")
        for yr, pct in zip(df["year"], vals):
            if pd.notna(pct):
                rows.append({"year": int(yr), "pollutant": pol, "metric": metric,
                             "index": round(100.0 + float(pct), 1)})
    out_df = pd.DataFrame(rows).sort_values(["pollutant", "metric", "year"]).reset_index(drop=True)
    if out_df.empty:
        return None
    out = DATA_DIR / "environment" / "cesi_air_quality.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out, index=False)
    save_metadata(out, df=out_df, latest_observation_date=str(int(out_df["year"].max())),
        source="Environment and Climate Change Canada",
        source_table="ECCC Canadian Environmental Sustainability Indicators — air quality (NAPS)",
        frequency="annual", unit="index (2009 = 100), population-weighted concentration",
        transformations=["national population-weighted concentration, indexed to 2009 = 100",
                         "tidy long: year, pollutant, metric (average/peak), index"])
    logger.info(f"  saved {len(out_df)} rows -> {out.name}")
    return out_df


# ---------------------------------------------------------------------------
# APEI — national emissions of the criteria air contaminants (1990–)
# ---------------------------------------------------------------------------
# The data-mart browse URL is a JS app; the bytes come from the /api/file?path=
# endpoint on the .az. host (the plain /data/ URL returns only the SPA shell).
APEI_URL = ("https://data-donnees.az.ec.gc.ca/api/file?path=/substances/monitor/"
            "canada-s-air-pollutant-emissions-inventory/"
            "A-APEI_Tables_Canada_Provinces_Territories/EN_APEI-Can-Prov_Terr.csv")

# Cube pollutant column -> short label. Stored tidy; the chart uses the criteria
# air contaminants with the clearest long-run declines (SOx/NOx/VOC/CO).
_APEI_POLLUTANTS = {
    "SOX (t)": "SOx", "NOX (t)": "NOX", "VOC (t)": "VOC",
    "CO (t)": "CO", "PM25 (t)": "PM2.5", "NH3 (t)": "NH3",
}


def fetch_apei_emissions():
    """National emissions of the criteria air contaminants, 1990–, from ECCC's
    Air Pollutant Emissions Inventory — the GRAND TOTAL across all sources for
    REGION='CA'. Output tidy long: year, pollutant, emissions_t."""
    logger.info("Fetching ECCC APEI national emissions...")
    try:
        r = requests.get(APEI_URL, headers=_UA, timeout=120)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"  APEI fetch failed: {e}")
        return None
    df = pd.read_csv(io.BytesIO(r.content), encoding="latin-1", low_memory=False)
    df.columns = [c.strip() for c in df.columns]
    nat = df[(df["REGION"] == "CA") & (df["Source"] == "GRAND TOTAL")].copy()
    if nat.empty:
        logger.error("  APEI: no national GRAND TOTAL rows found")
        return None
    nat["Year"] = pd.to_numeric(nat["Year"], errors="coerce")

    rows = []
    for col, label in _APEI_POLLUTANTS.items():
        if col not in nat.columns:
            logger.warning(f"  APEI column not found: {col!r}")
            continue
        vals = pd.to_numeric(nat[col], errors="coerce")
        for yr, v in zip(nat["Year"], vals):
            if pd.notna(yr) and pd.notna(v):
                rows.append({"year": int(yr), "pollutant": label, "emissions_t": float(v)})
    out_df = pd.DataFrame(rows).sort_values(["pollutant", "year"]).reset_index(drop=True)
    if out_df.empty:
        return None
    out = DATA_DIR / "environment" / "apei_emissions.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out, index=False)
    save_metadata(out, df=out_df, latest_observation_date=str(int(out_df["year"].max())),
        source="Environment and Climate Change Canada",
        source_table="ECCC Air Pollutant Emissions Inventory (APEI) — national GRAND TOTAL",
        frequency="annual", unit="tonnes per year",
        transformations=["national total (REGION=CA, all sources combined)",
                         "tidy long: year, pollutant, emissions_t"])
    logger.info(f"  saved {len(out_df)} rows -> {out.name}")
    return out_df


if __name__ == "__main__":
    fetch_cesi_air_quality()
    fetch_apei_emissions()
