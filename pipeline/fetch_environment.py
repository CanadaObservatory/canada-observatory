"""
Environment-section custom fetchers.

Greenhouse gas emissions from Environment and Climate Change Canada's Canadian
Environmental Sustainability Indicators (CESI) — the National Inventory Report
(NIR) basis, which is the series Canada's 2030 target (40–45% below 2005) is
defined against. Open Government Licence – Canada.

The CSV URLs are year-stamped by NIR release year, so **bump GHG_RELEASE each
spring** after ECCC publishes the new inventory.
"""

import io
import requests
import pandas as pd
from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GHG_RELEASE = "2025"  # ECCC CESI / NIR release year — bump each spring
GHG_BASE = ("https://www.canada.ca/content/dam/eccc/documents/csv/cesindicators/"
            f"ghg-emissions/{GHG_RELEASE}/")


def _clean_col(c):
    """Strip the '(megatonnes of carbon dioxide equivalent)' unit suffix."""
    return str(c).split("(")[0].strip()


def fetch_ghg():
    """National total GHG emissions (Mt CO2e), NIR basis, 1990–. The ECCC CSV has
    a title row + blank row before the header, hence skiprows=2."""
    url = GHG_BASE + "ghg-emissions-national-en.csv"
    logger.info("Fetching ECCC national GHG emissions...")
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"  GHG national fetch failed: {e}")
        return None
    df = pd.read_csv(io.StringIO(r.text), skiprows=2)
    df.columns = [_clean_col(c) for c in df.columns]
    df = df.rename(columns={"Year": "year", df.columns[1]: "ghg_mt"})
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["ghg_mt"] = pd.to_numeric(df["ghg_mt"], errors="coerce")
    df = df.dropna(subset=["year", "ghg_mt"])
    df["year"] = df["year"].astype(int)
    df = df[["year", "ghg_mt"]].sort_values("year").reset_index(drop=True)
    if df.empty:
        return None
    out = DATA_DIR / "environment" / "eccc_ghg.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    save_metadata(out, df=df, latest_observation_date=str(int(df["year"].max())),
        source="Environment and Climate Change Canada",
        source_table="ECCC National Inventory Report / CESI (greenhouse gas emissions)",
        frequency="annual", unit="megatonnes CO2 equivalent (NIR basis)",
        transformations=["national total GHG emissions, National Inventory Report basis"])
    logger.info(f"  saved {len(df)} rows -> {out.name}")
    return df


def fetch_ghg_by_sector():
    """GHG emissions by economic sector (oil & gas, transport, buildings, etc.),
    Mt CO2e, NIR basis. Tidy long: year, sector, ghg_mt."""
    url = GHG_BASE + "ghg-emissions-sector-en.csv"
    logger.info("Fetching ECCC GHG emissions by economic sector...")
    try:
        r = requests.get(url, timeout=60)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"  GHG sector fetch failed: {e}")
        return None
    df = pd.read_csv(io.StringIO(r.text), skiprows=2)
    df.columns = [_clean_col(c) for c in df.columns]
    df = df.rename(columns={"Year": "year"})
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df = df.dropna(subset=["year"])
    df["year"] = df["year"].astype(int)
    long = df.melt(id_vars="year", var_name="sector", value_name="ghg_mt")
    long["ghg_mt"] = pd.to_numeric(long["ghg_mt"], errors="coerce")
    long = long.dropna(subset=["ghg_mt"]).sort_values(["sector", "year"]).reset_index(drop=True)
    if long.empty:
        return None
    out = DATA_DIR / "environment" / "eccc_ghg_by_sector.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    long.to_csv(out, index=False)
    save_metadata(out, df=long, latest_observation_date=str(int(long["year"].max())),
        source="Environment and Climate Change Canada",
        source_table="ECCC CESI (greenhouse gas emissions by economic sector)",
        frequency="annual", unit="megatonnes CO2 equivalent (NIR basis)",
        transformations=["tidy long: year, economic sector, GHG (Mt)"])
    logger.info(f"  saved {len(long)} rows -> {out.name}")
    return long


if __name__ == "__main__":
    fetch_ghg()
    fetch_ghg_by_sector()
