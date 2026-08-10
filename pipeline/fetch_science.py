"""
Fetcher for the **Science** section's government-science-funding series.

Bespoke (source="custom") because it is a multi-series reshape the generic
single-series StatCan fetcher can't do: the three federal granting councils are
members of a "Major departments and agencies" dimension, not separate tables.

Source: Statistics Canada **27-10-0026-01**, "Federal expenditures on science and
technology, by major departments and agencies" — the machine-readable backbone
behind StatCan's annual *Federal Science Expenditures and Personnel* release (each
June). Federal-fiscal-year series, 2000/2001–. We take **Total expenditures × the
Research-and-development component** for the three councils (NSERC, SSHRC, CIHR)
plus the all-agencies total for context.

Real (constant-dollar) values come from the same source family: **27-10-0005-01**
publishes federal S&T R&D in both current and 2017-constant dollars, so its
current/constant ratio per fiscal year is a same-vintage R&D deflator we apply to
each council's nominal figure (cleaner than a generic CPI/GDP deflator). Values are
current (nominal) $ millions otherwise.

Editorial note: the level of public research funding is descriptive here — funding
*amounts* over time, no "good/bad" framing or scorecard valence.
"""
import logging

import pandas as pd

from pipeline.config import DATA_DIR
from pipeline.fetch_statcan import _get_table
from pipeline.metadata import save_metadata, validate_columns

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OUT_DIR = DATA_DIR / "science"

# The three federal granting councils + the Canada Foundation for Innovation
# (research infrastructure, not operating grants — so its pattern is lumpier) +
# the all-agencies total, mapped to short labels. Member strings are verbatim
# from 27-10-0026-01's agency dimension.
_AGENCIES = {
    "Natural Sciences and Engineering Research Council of Canada": "NSERC",
    "Canadian Institutes of Health Research": "CIHR",
    "Social Sciences and Humanities Research Council of Canada": "SSHRC",
    "Canada Foundation for Innovation": "CFI",
    "Total departments and agencies": "All federal agencies",
}
_COMPONENT = "Scientific and technological components and activities"

# Short display labels for the full agency breakdown (the composition bar). Keyed
# on the source member strings (whitespace-stripped). Agencies not listed keep
# their full name. Note: the Communications Research Centre and Canada Research
# Chairs are NOT separate members of this table — the former rolls into ISED, the
# latter is funded through the three councils.
_AGENCY_SHORT = {
    "National Research Council Canada": "National Research Council",
    "Canadian Institutes of Health Research": "CIHR",
    "Natural Sciences and Engineering Research Council of Canada": "NSERC",
    "Social Sciences and Humanities Research Council of Canada": "SSHRC",
    "Canada Foundation for Innovation": "CFI",
    "Innovation, Science and Economic Development Canada": "Innovation, Science & Econ. Dev.",
    "Canadian Space Agency": "Canadian Space Agency",
    "National Defence": "National Defence",
    "Agriculture and Agri-Food Canada": "Agriculture & Agri-Food",
    "Natural Resources Canada": "Natural Resources Canada",
    "Environment and Climate Change Canada": "Environment & Climate Change",
    "Public Health Agency of Canada": "Public Health Agency",
    "Health Canada": "Health Canada",
    "Global Affairs Canada": "Global Affairs Canada",
    "Fisheries and Oceans Canada": "Fisheries & Oceans",
    "Statistics Canada": "Statistics Canada",
    "Atomic Energy of Canada Limited": "Atomic Energy of Canada",
    "Canadian International Development Agency": "Cdn Int'l Development Agency",
    "International Development Research Centre": "Int'l Development Research Centre",
    "Other departments and agencies": "Other departments & agencies",
}


def _num(s):
    return pd.to_numeric(pd.Series(s).astype(str).str.replace(",", "", regex=False),
                         errors="coerce")


def _fiscal_label(ref):
    """'2000/2001' -> '2000/01' (federal fiscal year)."""
    a, b = ref.split("/")
    return f"{a}/{b[2:]}"


def _rd_deflator():
    """Same-source R&D price deflator from 27-10-0005-01: per fiscal year, the
    2017-constant / current ratio for federal S&T Research and development.
    Returns a Series indexed by fiscal-start year, or None if unavailable."""
    try:
        e = _get_table("27-10-0005-01")
    except Exception as ex:
        logger.warning(f"  R&D deflator unavailable, nominal only: {ex}")
        return None
    et = "Type of expenditures"
    e = e[(e["GEO"] == "Canada") & (e[et] == "Research and development")
          & (e["Prices"].isin(["Current prices", "2017 constant market prices"]))].copy()
    e["year"] = e["REF_DATE"].str[:4].astype(int)
    e["val"] = _num(e["VALUE"])
    w = e.pivot_table(index="year", columns="Prices", values="val", aggfunc="first")
    if not {"Current prices", "2017 constant market prices"} <= set(w.columns):
        return None
    w = w.dropna(subset=["Current prices", "2017 constant market prices"])
    w = w[w["Current prices"] > 0]
    return (w["2017 constant market prices"] / w["Current prices"]).rename("factor")


def fetch_science_funding():
    """Federal research funding over time + the full federal-R&D agency breakdown.

    Two outputs from StatCan 27-10-0026-01 (Total expenditures × R&D, Canada):
      * statcan_science_funding.csv — the four research-funding agencies
        (NSERC/CIHR/SSHRC + CFI) + all-agencies total, nominal + real (deflated
        with 27-10-0005-01); drives the funding-over-time line chart.
      * statcan_federal_rd_by_agency.csv — every reporting department/agency,
        for the latest-year composition bar (where the ~$10B federal S&T R&D goes).
    """
    logger.info("fetch_science_funding (27-10-0026-01 + 27-10-0005-01)")
    try:
        d = _get_table("27-10-0026-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    validate_columns(d, ["REF_DATE", "GEO", "Type of expenditures", _COMPONENT,
                         "Major departments and agencies", "VALUE"], "science_funding")
    base = d[(d["GEO"] == "Canada")
             & (d[_COMPONENT] == "Research and development")].copy()
    base["agency_full"] = base["Major departments and agencies"].str.strip()
    base["nominal"] = _num(base["VALUE"])                 # $ millions, current
    base["year"] = base["REF_DATE"].str[:4].astype(int)
    base["fiscal_year"] = base["REF_DATE"].map(_fiscal_label)
    rd = base[base["Type of expenditures"] == "Total expenditures"]
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ---- (1) research-funding agencies over time, nominal + real -------------
    fu = rd[rd["agency_full"].isin(_AGENCIES)].copy()
    fu["agency"] = fu["agency_full"].map(_AGENCIES)
    out = fu[["year", "fiscal_year", "agency", "nominal"]].dropna(subset=["nominal"])
    defl = _rd_deflator()
    if defl is not None:
        out = out.merge(defl, left_on="year", right_index=True, how="left")
        out["real"] = (out["nominal"] * out["factor"]).round(1)
        out = out.drop(columns=["factor"])
    else:
        out["real"] = pd.NA
    out = out.sort_values(["year", "agency"]).reset_index(drop=True)
    if out.empty:
        return None
    path = OUT_DIR / "statcan_science_funding.csv"
    out.to_csv(path, index=False)
    save_metadata(
        path, df=out, source="Statistics Canada",
        source_table="Statistics Canada 27-10-0026-01 (current $); 27-10-0005-01 (S&T deflator)",
        frequency="annual", unit="$ millions, R&D (total expenditures)",
        latest_observation_date=str(out["fiscal_year"].iloc[-1]),
        transformations=[
            "GEO=Canada; Type=Total expenditures; Component=Research and development",
            "granting councils (NSERC/CIHR/SSHRC) + CFI (research infrastructure) + all-agencies total",
            "real = nominal x (2017-constant / current) R&D ratio from 27-10-0005-01",
        ])
    logger.info(f"  saved {len(out)} rows -> {path.name}")

    # ---- (2) full by-agency breakdown (for the composition bar) --------------
    by = rd[rd["agency_full"] != "Total departments and agencies"].copy()
    by["agency"] = by["agency_full"].map(lambda a: _AGENCY_SHORT.get(a, a))
    by = (by[["year", "fiscal_year", "agency", "agency_full", "nominal"]]
          .dropna(subset=["nominal"])
          .sort_values(["year", "nominal"], ascending=[True, False])
          .reset_index(drop=True))
    if not by.empty:
        bypath = OUT_DIR / "statcan_federal_rd_by_agency.csv"
        by.to_csv(bypath, index=False)
        latest_fy = by[by["year"] == by["year"].max()]["fiscal_year"].iloc[0]
        save_metadata(
            bypath, df=by, source="Statistics Canada",
            source_table="Statistics Canada 27-10-0026-01",
            frequency="annual", unit="$ millions, R&D (total expenditures)",
            latest_observation_date=str(latest_fy),
            transformations=[
                "GEO=Canada; Type=Total expenditures; Component=Research and development",
                "all reporting departments/agencies (excludes the 'Total departments and agencies' row)",
            ])
        logger.info(f"  saved {len(by)} rows -> {bypath.name}")

    # ---- (3) in-house vs granted out (intramural vs extramural), federal total -
    perf = base[(base["agency_full"] == "Total departments and agencies")
                & (base["Type of expenditures"].isin(["Intramural", "Extramural"]))].copy()
    perf["performer"] = perf["Type of expenditures"].map({
        "Intramural": "In-house (intramural)",
        "Extramural": "Granted out (extramural)"})
    perf = (perf[["year", "fiscal_year", "performer", "nominal"]]
            .dropna(subset=["nominal"])
            .sort_values(["year", "performer"]).reset_index(drop=True))
    if not perf.empty:
        perf["share"] = (perf["nominal"]
                         / perf.groupby("year")["nominal"].transform("sum") * 100).round(1)
        ppath = OUT_DIR / "statcan_federal_rd_by_performer.csv"
        perf.to_csv(ppath, index=False)
        save_metadata(
            ppath, df=perf, source="Statistics Canada",
            source_table="Statistics Canada 27-10-0026-01",
            frequency="annual", unit="$ millions, R&D",
            latest_observation_date=str(perf["fiscal_year"].iloc[-1]),
            transformations=[
                "GEO=Canada; Component=Research and development; agency=Total departments and agencies",
                "Type of expenditures: Intramural (in-house) vs Extramural (granted out); share = % of federal R&D",
            ])
        logger.info(f"  saved {len(perf)} rows -> {ppath.name}")
    return out
