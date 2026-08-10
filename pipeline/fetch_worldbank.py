"""
Fetch indicators from the World Bank API (World Development Indicators).
Generic, registry-driven: one function handles any WB indicator code.

The World Bank API is a clean, stable JSON endpoint that uses ISO-3 country
codes (matching our PEER_CODES) and is openly licensed (CC-BY 4.0). It is the
tidiest source for a few series that OECD only exposes awkwardly — e.g. gross
fixed capital formation as % of GDP, and military expenditure as % of GDP
(which the WB sources from SIPRI and covers for all 17 peers, not just NATO).
"""

import requests
import pandas as pd
from pipeline.config import PEER_CODES, PEER_COUNTRIES, DATA_DIR
from pipeline.metadata import save_metadata
import logging

logger = logging.getLogger(__name__)

WB_BASE = "https://api.worldbank.org/v2"


def fetch_world_population():
    """Population of every country (World Bank SP.POP.TOTL, most recent year per
    country) for the "Canada isn't a small country" global-context chart.

    Unlike the peer-only generic fetcher, this keeps ALL countries — but first
    pulls the WB country metadata to drop the regional/income aggregates (region
    == 'Aggregates'; e.g. "World", "High income", "Sub-Saharan Africa"), which
    would otherwise dwarf every real country. `mrnev=1` returns each country's
    most-recent non-empty value.
    """
    logger.info("Fetching World Bank population for all countries...")
    try:
        meta = requests.get(f"{WB_BASE}/country",
                            params={"format": "json", "per_page": "400"},
                            timeout=60).json()
        real = {c["id"]: c["name"] for c in meta[1]
                if c.get("region", {}).get("value") != "Aggregates"}
        js = requests.get(f"{WB_BASE}/country/all/indicator/SP.POP.TOTL",
                          params={"format": "json", "per_page": "20000", "mrnev": "1"},
                          timeout=120).json()
    except Exception as e:
        logger.error(f"  Failed World Bank population fetch: {e}")
        return None

    if not isinstance(js, list) or len(js) < 2 or not js[1]:
        logger.warning("  World Bank returned no population data")
        return None

    seen, rows = set(), []
    for d in js[1]:
        code = d.get("countryiso3code")
        if code in real and d.get("value") is not None and code not in seen:
            seen.add(code)
            rows.append({"country_code": code, "country_name": real[code],
                         "year": int(d["date"]), "population": int(d["value"])})
    df = pd.DataFrame(rows)
    if df.empty or "CAN" not in set(df["country_code"]):
        logger.warning("  population data missing Canada or empty")
        return None
    df = df.sort_values("population", ascending=False).reset_index(drop=True)

    out_path = DATA_DIR / "population" / "worldbank_population.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="year",
        source="World Bank (World Development Indicators)",
        source_table="SP.POP.TOTL",
        frequency="annual", unit="people",
        transformations=["all countries (WB aggregates removed), most recent year each"])
    logger.info(f"  saved {len(df)} countries -> {out_path.name}")
    return df


def fetch_world_gdp():
    """GDP of every country (World Bank NY.GDP.MKTP.CD, current US$, most recent
    year per country) for the economy-size "Canada in the world" chart — the
    counterpart to the population one on the People page. Drops the WB regional/
    income aggregates the same way; `mrnev=1` returns each country's most recent
    non-empty value.
    """
    logger.info("Fetching World Bank GDP for all countries...")
    try:
        meta = requests.get(f"{WB_BASE}/country",
                            params={"format": "json", "per_page": "400"},
                            timeout=60).json()
        real = {c["id"]: c["name"] for c in meta[1]
                if c.get("region", {}).get("value") != "Aggregates"}
        # per_page=400 (not the 20000 the population fetcher uses): the WB API
        # rejects per_page=20000 + mrnev=1 for this indicator with a 400. mrnev=1
        # returns one row per country (~217), so 400 captures them all on page 1.
        js = requests.get(f"{WB_BASE}/country/all/indicator/NY.GDP.MKTP.CD",
                          params={"format": "json", "per_page": "400", "mrnev": "1"},
                          timeout=120).json()
    except Exception as e:
        logger.error(f"  Failed World Bank GDP fetch: {e}")
        return None

    if not isinstance(js, list) or len(js) < 2 or not js[1]:
        logger.warning("  World Bank returned no GDP data")
        return None

    seen, rows = set(), []
    for d in js[1]:
        code = d.get("countryiso3code")
        if code in real and d.get("value") is not None and code not in seen:
            seen.add(code)
            rows.append({"country_code": code, "country_name": real[code],
                         "year": int(d["date"]), "gdp": float(d["value"])})
    df = pd.DataFrame(rows)
    if df.empty or "CAN" not in set(df["country_code"]):
        logger.warning("  GDP data missing Canada or empty")
        return None
    df = df.sort_values("gdp", ascending=False).reset_index(drop=True)

    out_path = DATA_DIR / "economics" / "worldbank_gdp.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="year",
        source="World Bank (World Development Indicators)",
        source_table="NY.GDP.MKTP.CD",
        frequency="annual", unit="current US$",
        transformations=["all countries (WB aggregates removed), most recent year each"])
    logger.info(f"  saved {len(df)} countries -> {out_path.name}")
    return df


def fetch_world_land_area():
    """Land area of every country (World Bank AG.LND.TOTL.K2, sq km, most recent
    year per country) for the "Canada in the world" land chart on the Land &
    Environment page — Canada is the 4th-largest country by LAND area (and 2nd by
    total area once its vast fresh water is counted). We use *land* area, not the
    WB "surface area" series, because the latter carries a known bad value for
    Canada (~15.6M km², vs the true ~10M total); land area is consistent across
    countries. Same all-countries / drop-aggregates / mrnev=1 pattern as the GDP
    and population fetchers; per_page=400 (≈216 country rows).
    """
    logger.info("Fetching World Bank land area for all countries...")
    try:
        meta = requests.get(f"{WB_BASE}/country",
                            params={"format": "json", "per_page": "400"},
                            timeout=60).json()
        real = {c["id"]: c["name"] for c in meta[1]
                if c.get("region", {}).get("value") != "Aggregates"}
        js = requests.get(f"{WB_BASE}/country/all/indicator/AG.LND.TOTL.K2",
                          params={"format": "json", "per_page": "400", "mrnev": "1"},
                          timeout=120).json()
    except Exception as e:
        logger.error(f"  Failed World Bank land-area fetch: {e}")
        return None

    if not isinstance(js, list) or len(js) < 2 or not js[1]:
        logger.warning("  World Bank returned no land-area data")
        return None

    seen, rows = set(), []
    for d in js[1]:
        code = d.get("countryiso3code")
        if code in real and d.get("value") is not None and code not in seen:
            seen.add(code)
            rows.append({"country_code": code, "country_name": real[code],
                         "year": int(d["date"]), "land_area": float(d["value"])})
    df = pd.DataFrame(rows)
    if df.empty or "CAN" not in set(df["country_code"]):
        logger.warning("  land-area data missing Canada or empty")
        return None
    df = df.sort_values("land_area", ascending=False).reset_index(drop=True)

    out_path = DATA_DIR / "geography" / "worldbank_land_area.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="year",
        source="World Bank (World Development Indicators)",
        source_table="AG.LND.TOTL.K2",
        frequency="annual", unit="sq km (land area)",
        transformations=["all countries (WB aggregates removed), most recent year each"])
    logger.info(f"  saved {len(df)} countries -> {out_path.name}")
    return df


def fetch_pm25_global_context():
    """Mean population exposure to PM2.5 (World Bank EN.ATM.PM25.MC.M3, µg/m³) for
    a curated global-context set — Canada, the US, China, India. The deliberate
    "how clean is Canada's air, globally" companion to the OECD peer chart (the
    OECD Green Growth series is members-only, so it can't show China/India). The
    WHO 2021 guideline is 5 µg/m³.
    """
    logger.info("Fetching World Bank PM2.5 (global context)...")
    sel = {"CAN": "Canada", "USA": "United States", "CHN": "China", "IND": "India"}
    try:
        js = requests.get(f"{WB_BASE}/country/{';'.join(sel)}/indicator/EN.ATM.PM25.MC.M3",
                          params={"format": "json", "per_page": "5000"}, timeout=120).json()
    except Exception as e:
        logger.error(f"  Failed World Bank PM2.5 fetch: {e}")
        return None
    if not isinstance(js, list) or len(js) < 2 or not js[1]:
        logger.warning("  World Bank returned no PM2.5 data")
        return None
    rows = [{"country_code": d["countryiso3code"], "year": int(d["date"]),
             "pm25": d["value"]} for d in js[1] if d.get("value") is not None]
    df = pd.DataFrame(rows)
    df = df[df["country_code"].isin(sel)].copy()
    if df.empty:
        return None
    df["country_name"] = df["country_code"].map(sel)
    df = (df[["country_code", "country_name", "year", "pm25"]]
          .sort_values(["country_code", "year"]).reset_index(drop=True))
    out_path = DATA_DIR / "environment" / "worldbank_pm25_global.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="year",
        source="World Bank (World Development Indicators)",
        source_table="EN.ATM.PM25.MC.M3",
        frequency="annual", unit="µg/m³ (mean annual PM2.5 exposure)",
        transformations=["curated global-context set: Canada, US, China, India"])
    logger.info(f"  saved {len(df)} rows -> {out_path.name}")
    return df


def fetch_worldbank_indicator(ind):
    """Generic World Bank fetch driven by an Indicator (source='worldbank').

    Emits the same [country_code, country_name, year, <value_col>] shape as the
    OECD fetcher, so the chart builders work unchanged.
    """
    logger.info(f"World Bank indicator: {ind.id} ({ind.title})")
    codes = ";".join(PEER_CODES)
    url = f"{WB_BASE}/country/{codes}/indicator/{ind.wb_indicator}"
    try:
        r = requests.get(url, params={"format": "json", "per_page": "20000",
                                      "date": f"{ind.start_period}:2026"}, timeout=120)
        r.raise_for_status()
        js = r.json()
    except Exception as e:
        logger.error(f"  Failed World Bank fetch {ind.wb_indicator}: {e}")
        return None

    if not isinstance(js, list) or len(js) < 2 or not js[1]:
        logger.warning(f"  {ind.id}: World Bank returned no data")
        return None

    rows = [{"country_code": d["countryiso3code"], "year": int(d["date"]),
             ind.value_col: d["value"]}
            for d in js[1] if d.get("value") is not None]
    df = pd.DataFrame(rows)
    if df.empty:
        return None

    df = df[df["country_code"].isin(PEER_CODES)].copy()
    df["country_name"] = df["country_code"].map(PEER_COUNTRIES)
    df = df.sort_values(["country_code", "year"]).reset_index(drop=True)
    if ind.transform:
        df = ind.transform(df)
    df = df[["country_code", "country_name", "year", ind.value_col]]

    out_path = ind.out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="year",
        source="World Bank (World Development Indicators)",
        source_table=ind.source_table,
        frequency=ind.frequency, unit=ind.unit,
        transformations=[f"World Bank indicator {ind.wb_indicator}, filtered to 17 OECD peers"])
    logger.info(f"  saved {len(df)} rows -> {out_path.name}")
    return df
