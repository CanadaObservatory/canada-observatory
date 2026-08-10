"""
Fetch World Happiness Report data from the official WHR download site.
Source: World Happiness Report (Gallup World Poll data).
License: Free for public use (CC-BY via OWID redistribution).

The download URL changes each year when a new report is released (typically March).
Update WHR_URL below when a new report is published.
"""

import io
import requests
import pandas as pd
from pipeline.config import DATA_DIR, PEER_COUNTRIES
from pipeline.metadata import save_metadata, validate_columns
import logging

logger = logging.getLogger(__name__)

# Updated annually — WHR 2026 report (covers 2011–2025 data)
WHR_URL = "https://files.worldhappiness.report/WHR26_Data_Figure_2.1.xlsx"


# Map WHR country names to our ISO codes
WHR_COUNTRY_MAP = {
    "Canada": "CAN",
    "United States": "USA",
    "United Kingdom": "GBR",
    "Germany": "DEU",
    "France": "FRA",
    "Italy": "ITA",
    "Japan": "JPN",
    "Australia": "AUS",
    "South Korea": "KOR",
    "Netherlands": "NLD",
    "Sweden": "SWE",
    "Switzerland": "CHE",
    "Norway": "NOR",
    "Denmark": "DNK",
    "Finland": "FIN",
    "Israel": "ISR",
    "Austria": "AUT",
    "Belgium": "BEL",
    "Ireland": "IRL",
    "New Zealand": "NZL",
    # Alternate names that WHR might use
    "Korea, Republic of": "KOR",
    "Korea": "KOR",
    "Korea, Rep.": "KOR",
    "Republic of Korea": "KOR",
    "Korea (Republic of)": "KOR",
    "Turkiye": None,  # Not in peer group, skip
}


def fetch_happiness():
    """
    Fetch World Happiness Report data (Figure 2.1).
    Downloads the consolidated XLSX and extracts happiness scores
    and contributing factors for OECD peer countries.
    """
    logger.info("Fetching World Happiness Report data...")
    logger.info(f"  URL: {WHR_URL}")

    try:
        r = requests.get(WHR_URL, timeout=60)
        r.raise_for_status()
    except Exception as e:
        logger.error(f"  Failed to fetch WHR data: {e}")
        return None

    df = pd.read_excel(io.BytesIO(r.content), engine="openpyxl")
    logger.info(f"  Got {len(df)} total rows")

    # Normalize column names
    col_map = {
        "Year": "year",
        "Rank": "rank",
        "Country name": "country_name",
        "Life evaluation (3-year average)": "happiness_score",
        "Lower whisker": "happiness_lower",
        "Upper whisker": "happiness_upper",
        "Explained by: Log GDP per capita": "explained_gdp",
        "Explained by: Social support": "explained_social_support",
        "Explained by: Healthy life expectancy": "explained_life_expectancy",
        "Explained by: Freedom to make life choices": "explained_freedom",
        "Explained by: Generosity": "explained_generosity",
        "Explained by: Perceptions of corruption": "explained_corruption",
        "Dystopia + residual": "dystopia_residual",
    }

    # Only rename columns that exist
    rename = {k: v for k, v in col_map.items() if k in df.columns}
    df = df.rename(columns=rename)

    # Drop columns that are all NaN (the trailing None columns from Excel)
    df = df.dropna(axis=1, how="all")

    validate_columns(df, ["year", "country_name", "happiness_score"], "happiness")

    # Normalize country names before matching. WHR cells sometimes carry
    # trailing or non-breaking spaces, which silently dropped exact-match
    # countries from the peer group (this is why South Korea went missing).
    df["country_name"] = (df["country_name"].astype(str)
                          .str.replace(" ", " ", regex=False)
                          .str.strip())
    korea_like = sorted({n for n in df["country_name"].unique() if "korea" in n.lower()})
    logger.info(f"  WHR names containing 'korea': {korea_like}")

    # Map to our peer country codes
    peer_names = set(WHR_COUNTRY_MAP.keys())
    df = df[df["country_name"].isin(peer_names)].copy()
    df["country_code"] = df["country_name"].map(WHR_COUNTRY_MAP)
    df = df[df["country_code"].notna()].copy()

    # Use our standard country names, and KEEP ONLY current peers: WHR_COUNTRY_MAP
    # may still list countries since dropped from PEER_COUNTRIES (e.g. Austria,
    # Belgium, Ireland removed 2026-05). Those map to a NaN name here; left in, the
    # NaN name later crashes the peer chart's name-sort (str vs float). Drop them so
    # this fetcher self-tracks the peer group.
    df["country_name"] = df["country_code"].map(PEER_COUNTRIES)
    df = df[df["country_name"].notna()].copy()

    # Convert types
    df["year"] = df["year"].astype(int)
    numeric_cols = [c for c in df.columns if c not in ("country_code", "country_name")]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["happiness_score"])
    df = df.sort_values(["country_code", "year"]).reset_index(drop=True)

    logger.info(f"  Filtered to {len(df)} rows for {df['country_code'].nunique()} peer countries")

    out_path = DATA_DIR / "wellbeing" / "whr_happiness.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="year",
        source="World Happiness Report (Gallup World Poll)",
        source_table="WHR Figure 2.1",
        frequency="annual",
        unit="Cantril ladder score (0-10)",
        transformations=[
            "filtered to 20 OECD peers",
            "normalized column names",
            "mapped country names to ISO codes",
        ],
    )
    logger.info(f"Saved {len(df)} rows to {out_path}")

    return df


if __name__ == "__main__":
    fetch_happiness()
