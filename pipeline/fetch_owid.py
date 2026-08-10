"""
Fetch energy mix data from Our World in Data (OWID).
Source: Energy Institute Statistical Review + Ember + EIA, aggregated by OWID.
License: CC-BY-4.0
"""

import requests
import io
import pandas as pd
from pipeline.config import DATA_DIR, PEER_CODES, PEER_COUNTRIES
from pipeline.metadata import save_metadata, validate_columns
import logging

logger = logging.getLogger(__name__)

OWID_ENERGY_URL = "https://owid-public.owid.io/data/energy/owid-energy-data.csv"


def fetch_energy_mix():
    """
    Fetch primary energy consumption by source for OECD peers.
    Returns shares of total primary energy: coal, oil, gas, nuclear, renewables.
    """
    logger.info("Fetching OWID energy mix data...")
    logger.info(f"  URL: {OWID_ENERGY_URL}")

    try:
        r = requests.get(OWID_ENERGY_URL, timeout=120)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"  Failed to fetch OWID energy data: {e}")
        return None

    df = pd.read_csv(io.StringIO(r.text))
    logger.info(f"  Got {len(df)} total rows")

    validate_columns(df, [
        "iso_code", "country", "year",
        "coal_share_energy", "oil_share_energy", "gas_share_energy",
        "nuclear_share_energy", "renewables_share_energy",
        "primary_energy_consumption",
    ], "energy_mix")

    # Filter to our peer countries
    df = df[df["iso_code"].isin(PEER_CODES)].copy()

    # Side output: electricity generation per capita (kWh/person) — a physical proxy
    # for material living standards (the "energy ladder"). Emitted independently of the
    # primary-energy `dropna` below, because the most recent year usually carries
    # electricity data before the full primary-energy series is finalised.
    if "per_capita_electricity" in df.columns:
        epc = df[["iso_code", "country", "year", "per_capita_electricity"]].copy()
        epc["per_capita_electricity"] = pd.to_numeric(epc["per_capita_electricity"], errors="coerce")
        epc = epc.dropna(subset=["per_capita_electricity"]).rename(
            columns={"iso_code": "country_code", "country": "country_name"})
        epc["year"] = epc["year"].astype(int)
        epc["country_name"] = epc["country_code"].map(PEER_COUNTRIES)
        epc = epc.sort_values(["country_code", "year"]).reset_index(drop=True)
        epc_path = DATA_DIR / "environment" / "owid_electricity_per_capita.csv"
        epc_path.parent.mkdir(parents=True, exist_ok=True)
        epc.to_csv(epc_path, index=False)
        save_metadata(epc_path, df=epc, date_column="year",
            source="Our World in Data (Energy Institute, Ember, EIA)",
            source_table="owid-energy-data (per_capita_electricity)",
            frequency="annual", unit="kWh per person",
            transformations=["electricity generation per capita; OECD peer group"])
        logger.info(f"  side output: {len(epc)} rows -> {epc_path.name}")

    # Keep relevant columns — both primary-energy shares (full energy incl.
    # transport & heating) and electricity-generation shares (the grid, where
    # nuclear/hydro are properly sized and the low-carbon story is clearest).
    keep_cols = [
        "iso_code", "country", "year",
        # primary energy (total energy supply)
        "coal_share_energy", "oil_share_energy", "gas_share_energy",
        "nuclear_share_energy", "renewables_share_energy",
        "hydro_share_energy", "solar_share_energy", "wind_share_energy",
        "biofuel_share_energy", "low_carbon_share_energy", "fossil_share_energy",
        # electricity generation
        "coal_share_elec", "oil_share_elec", "gas_share_elec",
        "nuclear_share_elec", "renewables_share_elec", "hydro_share_elec",
        "low_carbon_share_elec", "fossil_share_elec",
        "primary_energy_consumption",
    ]
    available = [c for c in keep_cols if c in df.columns]
    df = df[available].copy()

    # Rename for consistency
    df = df.rename(columns={
        "iso_code": "country_code",
        "country": "country_name",
    })

    # Convert numeric columns
    numeric_cols = [c for c in df.columns if c not in ("country_code", "country_name", "year")]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df["year"] = df["year"].astype(int)
    df = df.dropna(subset=["primary_energy_consumption"])
    df = df.sort_values(["country_code", "year"]).reset_index(drop=True)

    # Use our standard country names
    df["country_name"] = df["country_code"].map(PEER_COUNTRIES)

    out_path = DATA_DIR / "environment" / "owid_energy_mix.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="year",
        source="Our World in Data (Energy Institute, Ember, EIA)",
        source_table="owid-energy-data",
        frequency="annual",
        unit="% of primary energy",
        transformations=["filtered to 20 OECD peers, selected energy share columns"],
    )
    logger.info(f"Saved {len(df)} rows to {out_path}")

    return df


OWID_CO2_URL = "https://raw.githubusercontent.com/owid/co2-data/master/owid-co2-data.csv"

_OWID_CO2_CACHE = None


def _load_owid_co2():
    """Download the OWID CO2 dataset once and cache it for the process.

    Several indicators (consumption CO2, CO2 per GDP) draw from this one large
    CSV; caching avoids re-downloading it per fetch within a single pipeline run.
    Returns None on failure (each caller then rides the STALE fallback).
    """
    global _OWID_CO2_CACHE
    if _OWID_CO2_CACHE is not None:
        return _OWID_CO2_CACHE
    try:
        r = requests.get(OWID_CO2_URL, timeout=180)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"  Failed to fetch OWID CO2 data: {e}")
        return None
    _OWID_CO2_CACHE = pd.read_csv(io.StringIO(r.text))
    return _OWID_CO2_CACHE


def fetch_consumption_co2():
    """Consumption-based CO2 per capita (OWID): emissions allocated to where goods
    are *consumed* rather than produced, so emissions embodied in imports count
    against the importer. The production-based measure (already charted) misses
    this; for trade-exposed economies the two can differ materially.
    """
    logger.info("Fetching OWID consumption-based CO2...")
    df = _load_owid_co2()
    if df is None:
        return None

    validate_columns(df, ["iso_code", "country", "year", "consumption_co2_per_capita"],
                     "consumption_co2")
    df = df[df["iso_code"].isin(PEER_CODES)].copy()
    df = df.rename(columns={"iso_code": "country_code",
                            "consumption_co2_per_capita": "consumption_co2_pc"})
    df["consumption_co2_pc"] = pd.to_numeric(df["consumption_co2_pc"], errors="coerce")
    df = df.dropna(subset=["consumption_co2_pc"])
    df["year"] = df["year"].astype(int)
    df["country_name"] = df["country_code"].map(PEER_COUNTRIES)
    df = (df[["country_code", "country_name", "year", "consumption_co2_pc"]]
          .sort_values(["country_code", "year"]).reset_index(drop=True))
    if df.empty:
        return None

    out_path = DATA_DIR / "environment" / "owid_consumption_co2.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="year",
        source="Our World in Data (Global Carbon Project)",
        source_table="owid-co2-data",
        frequency="annual", unit="tonnes CO2 per capita (consumption-based)",
        transformations=["filtered to 17 OECD economies, consumption_co2_per_capita"])
    logger.info(f"  saved {len(df)} rows -> {out_path.name}")
    return df


def fetch_co2_per_gdp():
    """CO2 emissions intensity (OWID): production-based CO2 per dollar of GDP,
    in kilograms per international-$. The carbon emitted per unit of economic
    output — a measure of how clean the economy is, independent of its size.
    Canada runs high among peers (a cold climate, long transport distances, and
    an energy- and resource-heavy economy all push intensity up).
    """
    logger.info("Fetching OWID CO2 per GDP (emissions intensity)...")
    df = _load_owid_co2()
    if df is None:
        return None

    validate_columns(df, ["iso_code", "country", "year", "co2_per_gdp"], "co2_per_gdp")
    df = df[df["iso_code"].isin(PEER_CODES)].copy()
    df = df.rename(columns={"iso_code": "country_code"})
    df["co2_per_gdp"] = pd.to_numeric(df["co2_per_gdp"], errors="coerce")
    df = df.dropna(subset=["co2_per_gdp"])
    df["year"] = df["year"].astype(int)
    df["country_name"] = df["country_code"].map(PEER_COUNTRIES)
    df = (df[["country_code", "country_name", "year", "co2_per_gdp"]]
          .sort_values(["country_code", "year"]).reset_index(drop=True))
    if df.empty:
        return None

    out_path = DATA_DIR / "environment" / "owid_co2_per_gdp.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="year",
        source="Our World in Data (Global Carbon Project)",
        source_table="owid-co2-data",
        frequency="annual", unit="kg CO2 per $ of GDP",
        transformations=["filtered to 17 OECD economies, co2_per_gdp"])
    logger.info(f"  saved {len(df)} rows -> {out_path.name}")
    return df


def fetch_co2_global_context():
    """CO2 per capita for a small, curated global-context set — Canada, the US,
    China, India, and the world average (OWID). A deliberate companion to the
    OECD peer chart (kept separate, not bolted onto the 17-peer lines): it places
    Canada among the world's highest per-capita emitters while showing the
    emerging giants (China risen sharply, India still low) and the world line.
    """
    logger.info("Fetching OWID CO2 per capita (global context)...")
    df = _load_owid_co2()
    if df is None:
        return None
    validate_columns(df, ["iso_code", "country", "year", "co2_per_capita"], "co2_global")
    sel = {"CAN": "Canada", "USA": "United States", "CHN": "China", "IND": "India"}
    countries = df[df["iso_code"].isin(sel)].copy()
    countries["country_code"] = countries["iso_code"]
    countries["country_name"] = countries["iso_code"].map(sel)
    world = df[df["country"] == "World"].copy()   # OWID aggregate has no iso_code
    world["country_code"] = "WLD"
    world["country_name"] = "World"
    out = pd.concat([countries, world], ignore_index=True)
    out["co2_per_capita"] = pd.to_numeric(out["co2_per_capita"], errors="coerce")
    out = out.dropna(subset=["co2_per_capita"])
    out["year"] = out["year"].astype(int)
    out = out[out["year"] >= 1950]   # the modern era (China's rise is post-2000)
    out = (out[["country_code", "country_name", "year", "co2_per_capita"]]
           .sort_values(["country_code", "year"]).reset_index(drop=True))
    if out.empty:
        return None
    out_path = DATA_DIR / "environment" / "owid_co2_global_context.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
        source="Our World in Data (Global Carbon Project)",
        source_table="owid-co2-data",
        frequency="annual", unit="tonnes CO2 per capita (production-based)",
        transformations=["curated global-context set: Canada, US, China, India, World; 1950–"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out


OWID_LIFE_EXPECTANCY_URL = (
    "https://ourworldindata.org/grapher/life-expectancy.csv"
    "?v=1&csvType=full&useColumnShortNames=true"
)


def fetch_life_expectancy_canada():
    """Canada life expectancy at birth, long-run (OWID).

    A single Canada series back to 1831 — decennial census-year estimates before
    1921, annual from 1921 on. OWID compiles it from the Human Mortality Database,
    the UN World Population Prospects, and historical-demography sources; it aligns
    with the OECD modern series at the 1980+ overlap (OWID 75.06 vs OECD 75.1 in
    1980), so it splices cleanly. Drives the Health page's "long view" chart — the
    deep Canadian history the OECD peer-comparison series (1980-) cannot show.
    """
    logger.info("Fetching OWID life expectancy (Canada long-run)...")
    try:
        r = requests.get(OWID_LIFE_EXPECTANCY_URL,
                         headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
    except Exception as e:
        logger.error(f"  Failed to fetch OWID life expectancy: {e}")
        return None

    validate_columns(df, ["entity", "year", "life_expectancy_0"],
                     "life_expectancy_canada")
    df = df[df["entity"] == "Canada"].copy()
    df["life_expectancy"] = pd.to_numeric(df["life_expectancy_0"], errors="coerce")
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df = df.dropna(subset=["year", "life_expectancy"])
    df["year"] = df["year"].astype(int)
    out = (df[["year", "life_expectancy"]]
           .sort_values("year").reset_index(drop=True))
    if out.empty:
        return None

    out_path = DATA_DIR / "health" / "owid_life_expectancy_canada.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
        source="Our World in Data (Human Mortality Database; UN; historical demography)",
        source_table="life-expectancy",
        frequency="annual (decennial before 1921)",
        unit="years (period life expectancy at birth)",
        transformations=["filtered to Canada; life_expectancy_0 (period LE at birth)"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out


if __name__ == "__main__":
    fetch_energy_mix()
    fetch_consumption_co2()
    fetch_life_expectancy_canada()
