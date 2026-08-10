"""
Fetchers for the **Innovation** page (data lands in data/science/, like BERD).

Bespoke OECD SDMX series the generic single-value fetcher can't handle:
  * triadic patents *per million population* — needs two MSTI series divided;
  * government support for business R&D split *direct vs tax* — two measures of
    the R&D-tax-incentive dataflow merged.

Both reuse `_fetch_oecd_csv` and emit the canonical peer shape
[country_code, country_name, year, <value>] so the chart builders work unchanged.
Descriptive peer comparisons — no scorecard valence.
"""
import logging

import pandas as pd

from pipeline.config import PEER_CODES, PEER_COUNTRIES, DATA_DIR
from pipeline.fetch_oecd import _fetch_oecd_csv
from pipeline.fetch_statcan import _get_table
from pipeline.metadata import save_metadata

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OUT_DIR = DATA_DIR / "science"
_MSTI = "OECD.STI.STP,DSD_MSTI@DF_MSTI,1.3"


def _tidy(df, col):
    """OECD SDMX CSV -> [country_code, year, <col>] (numeric)."""
    d = (df[["REF_AREA", "TIME_PERIOD", "OBS_VALUE"]]
         .rename(columns={"REF_AREA": "country_code", "TIME_PERIOD": "year",
                          "OBS_VALUE": col}))
    d["year"] = d["year"].astype(int)
    d[col] = pd.to_numeric(d[col], errors="coerce")
    return d


def fetch_triadic_patents():
    """Triadic patent families per million population (OECD MSTI).

    Triadic families (a patent filed at the EPO, USPTO and JPO for the same
    invention) screen out low-value domestic-only filings, so this is a
    quality-controlled innovation-output measure. P_TRIAD ÷ population
    (TOT_POP is in thousands, so ÷ (pop/1000) = per million).
    """
    logger.info("fetch_triadic_patents (OECD MSTI P_TRIAD / TOT_POP)")
    codes = "+".join(PEER_CODES)
    pat = _fetch_oecd_csv(_MSTI, f"{codes}.A.P_TRIAD.PATN.._Z", start_period=2000)
    pop = _fetch_oecd_csv(_MSTI, f"{codes}.A.TOT_POP.PS.._Z", start_period=2000)
    if pat is None or pop is None or pat.empty or pop.empty:
        return None
    m = _tidy(pat, "patents").merge(_tidy(pop, "pop"), on=["country_code", "year"])
    m = m[m["country_code"].isin(PEER_CODES)].copy()
    m = m[(m["pop"] > 0) & m["patents"].notna()]
    m["patents_per_million"] = (m["patents"] / (m["pop"] / 1000.0)).round(2)
    m["country_name"] = m["country_code"].map(PEER_COUNTRIES)
    out = (m[["country_code", "country_name", "year", "patents_per_million"]]
           .dropna().sort_values(["country_code", "year"]).reset_index(drop=True))
    if out.empty:
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "oecd_triadic_patents.csv"
    out.to_csv(path, index=False)
    save_metadata(path, df=out, date_column="year", source="OECD",
                  source_table="OECD MSTI (triadic patent families; population)",
                  frequency="annual", unit="patents per million population",
                  transformations=["P_TRIAD / (TOT_POP/1000); 17 OECD economies"])
    logger.info(f"  saved {len(out)} rows -> {path.name}")
    return out


def fetch_rd_tax_support():
    """Government support for business R&D, % of GDP — direct funding vs tax
    incentives (OECD R&D Tax Incentive indicators, DSD_RDTAX).

    `DF` = direct government funding of BERD; `DF_GTARD` = the cost of R&D tax
    incentives (e.g. Canada's SR&ED). Canada is unusual for leaning on tax
    incentives more than direct grants. A country reporting only one type is
    treated as ~zero of the other (the OECD's own convention for this series).
    """
    logger.info("fetch_rd_tax_support (OECD DSD_RDTAX direct + tax)")
    codes = "+".join(PEER_CODES)
    flow = "OECD.STI.STP,DSD_RDTAX@DF_RDTAX,1.0"
    direct = _fetch_oecd_csv(flow, f"{codes}.A.DF.PT_B1GQ._Z._Z", start_period=2000)
    tax = _fetch_oecd_csv(flow, f"{codes}.A.DF_GTARD.PT_B1GQ._Z._Z", start_period=2000)
    if direct is None or tax is None or direct.empty or tax.empty:
        return None
    m = _tidy(direct, "direct").merge(_tidy(tax, "tax"),
                                      on=["country_code", "year"], how="outer")
    m = m[m["country_code"].isin(PEER_CODES)].copy()
    m["direct"] = m["direct"].fillna(0.0)
    m["tax"] = m["tax"].fillna(0.0)
    m["total"] = (m["direct"] + m["tax"]).round(4)
    m = m[m["total"] > 0]
    m["country_name"] = m["country_code"].map(PEER_COUNTRIES)
    out = (m[["country_code", "country_name", "year", "direct", "tax", "total"]]
           .sort_values(["country_code", "year"]).reset_index(drop=True))
    if out.empty:
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "oecd_rd_support.csv"
    out.to_csv(path, index=False)
    save_metadata(path, df=out, date_column="year", source="OECD",
                  source_table="OECD R&D Tax Incentive indicators (DSD_RDTAX)",
                  frequency="annual", unit="% of GDP",
                  transformations=["direct (DF) + tax incentives (DF_GTARD), % of GDP; 17 OECD economies"])
    logger.info(f"  saved {len(out)} rows -> {path.name}")
    return out


# --------------------------------------------------------------------------- #
# Structural / ownership context (the "why the gap" backbone) — added 2026-06-23
# --------------------------------------------------------------------------- #
_SECTOR_SHORT = {
    "Agriculture, forestry, fishing and hunting [11]": "Agriculture & forestry",
    "Mining, quarrying, and oil and gas extraction [21]": "Mining, oil & gas",
    "Utilities [22]": "Utilities",
    "Construction [23]": "Construction",
    "Manufacturing [31-33]": "Manufacturing",
    "Wholesale trade [41]": "Wholesale trade",
    "Retail trade [44-45]": "Retail trade",
    "Transportation and warehousing [48-49]": "Transportation & warehousing",
    "Information and cultural industries [51]": "Information & culture",
    "Finance and insurance [52]": "Finance & insurance",
    "Real estate and rental and leasing [53]": "Real estate",
    "Professional, scientific and technical services [54]": "Professional & scientific",
    "Management of companies and enterprises [55]": "Management of companies",
    "Administrative and support, waste management and remediation services [56]": "Admin & support",
    "Educational services [61]": "Education",
    "Health care and social assistance [62]": "Health & social assistance",
    "Arts, entertainment and recreation [71]": "Arts & recreation",
    "Accommodation and food services [72]": "Accommodation & food",
    "Other services (except public administration) [81]": "Other services",
    "Public administration [91]": "Public administration",
}


def fetch_industry_structure():
    """Canada's GDP by major industry (latest month), as a share of all-industry
    GDP — the structural backdrop to the innovation gap (resources and finance
    versus R&D-intensive manufacturing). StatCan 36-10-0434-01, chained (2017) $,
    seasonally adjusted at annual rates."""
    logger.info("fetch_industry_structure (36-10-0434-01)")
    try:
        d = _get_table("36-10-0434-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    naics = [c for c in d.columns if "NAICS" in c][0]
    d = d[(d["GEO"] == "Canada")
          & (d["Seasonal adjustment"] == "Seasonally adjusted at annual rates")].copy()
    prices = [p for p in d["Prices"].unique() if "hained" in str(p)]
    if prices:
        d = d[d["Prices"] == prices[0]]
    d["v"] = pd.to_numeric(d["VALUE"], errors="coerce")
    latest = d["REF_DATE"].max()
    d = d[d["REF_DATE"] == latest]
    tot = d[d[naics] == "All industries [T001]"]["v"]
    if tot.empty or tot.iloc[0] <= 0:
        return None
    sec = d[d[naics].isin(_SECTOR_SHORT)].copy()
    sec["sector"] = sec[naics].map(_SECTOR_SHORT)
    sec["share"] = (sec["v"] / tot.iloc[0] * 100).round(2)
    out = (sec[["sector", "share"]].dropna()
           .sort_values("share", ascending=False).reset_index(drop=True))
    out.insert(0, "period", latest)
    if out.empty:
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "statcan_industry_structure.csv"
    out.to_csv(path, index=False)
    save_metadata(path, df=out, source="Statistics Canada",
                  source_table="Statistics Canada 36-10-0434-01",
                  frequency="monthly", unit="% of GDP (chained 2017 $)",
                  latest_observation_date=str(latest),
                  transformations=["GEO=Canada, SA at annual rates, chained dollars; each 2-digit NAICS sector / All industries [T001]"])
    logger.info(f"  saved {len(out)} rows -> {path.name}")
    return out


def fetch_foreign_rd_control():
    """Share of Canadian in-house business R&D performed by foreign-controlled
    firms, over time. StatCan 27-10-0333-01 (country of control = Foreign / Total)."""
    logger.info("fetch_foreign_rd_control (27-10-0333-01)")
    try:
        d = _get_table("27-10-0333-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    naics = [c for c in d.columns if "NAICS" in c][0]
    etype = "In-house research and development expenditure types"
    d = d[(d["GEO"] == "Canada")
          & (d[etype] == "Total in-house research and development expenditures")].copy()
    naics_total = next((m for m in d[naics].unique()
                        if m.lower().startswith("total") or "all industries" in m.lower()), None)
    if naics_total:
        d = d[d[naics] == naics_total]
    d["v"] = pd.to_numeric(d["VALUE"], errors="coerce")
    d["year"] = d["REF_DATE"].astype(str).str[:4].astype(int)
    w = d.pivot_table(index="year", columns="Country of control", values="v", aggfunc="first")
    need = ["Total country of control", "Foreign"]
    if not set(need) <= set(w.columns):
        return None
    w = w.dropna(subset=need)
    w = w[w["Total country of control"] > 0]
    out = pd.DataFrame({
        "year": w.index.astype(int),
        "foreign_share": (w["Foreign"] / w["Total country of control"] * 100).round(1).values,
        "foreign_rd": w["Foreign"].values,
        "total_rd": w["Total country of control"].values,
    }).sort_values("year").reset_index(drop=True)
    if out.empty:
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "statcan_foreign_rd_control.csv"
    out.to_csv(path, index=False)
    save_metadata(path, df=out, source="Statistics Canada",
                  source_table="Statistics Canada 27-10-0333-01",
                  frequency="annual", unit="% of business in-house R&D",
                  latest_observation_date=str(int(out["year"].iloc[-1])),
                  transformations=["GEO=Canada, total in-house R&D, all industries; Foreign / Total country of control"])
    logger.info(f"  saved {len(out)} rows -> {path.name}")
    return out


def fetch_patents_us_owned():
    """Share of a country's domestically-invented patents that are owned by US
    entities — a proxy for invention value flowing abroad. OECD DSD_PATENTS
    (foreign ownership of domestic inventions; IP5 6F0 families, priority date)."""
    logger.info("fetch_patents_us_owned (OECD DSD_PATENTS)")
    codes = "+".join(PEER_CODES)
    flow = "OECD.STI.PIE,DSD_PATENTS@DF_PATENTS_FOREIGN,1.0"
    df = _fetch_oecd_csv(flow, f"6F0.A.AP.PT_PATN.PRIORITY.{codes}.USA._Z.FO._Z._Z",
                         start_period=2000)
    if df is None or df.empty:
        return None
    d = _tidy(df, "pct_us_owned")
    d = d[d["country_code"].isin(PEER_CODES) & d["pct_us_owned"].notna()].copy()
    d["country_name"] = d["country_code"].map(PEER_COUNTRIES)
    out = (d[["country_code", "country_name", "year", "pct_us_owned"]]
           .sort_values(["country_code", "year"]).reset_index(drop=True))
    if out.empty:
        return None
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / "oecd_patents_us_owned.csv"
    out.to_csv(path, index=False)
    save_metadata(path, df=out, date_column="year", source="OECD",
                  source_table="OECD, Foreign ownership of domestic inventions (DSD_PATENTS)",
                  frequency="annual", unit="% of domestically-invented patents",
                  transformations=["% of priority-date IP5 (6F0) patent families invented domestically but owned by US entities; 17 OECD economies"])
    logger.info(f"  saved {len(out)} rows -> {path.name}")
    return out
