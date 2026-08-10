"""IRCC open-data fetchers — immigration ORIGINS & CHARACTERISTICS.

Permanent residents by source country & immigration category, study/work permit
holders by source country, and asylum claimants by source country + total claims
over time. All from IRCC's monthly open data (open.canada.ca, Open Government
Licence – Canada), 2015– (asylum 2018–). Drives the Immigration page
(population/immigration.qmd).

IRCC CSV quirks handled by `_ircc`: TAB-delimited despite the `.csv` extension;
EN_/FR_ bilingual column pairs (French dropped); `--` = a suppressed small count
(0–5); all values are rounded to the nearest 5 (fine for the top-country ranked bars
and category totals here — we never divide small cells). Each file carries `Other`
and `…not stated` residual rows: kept in the totals, excluded from the top-country
rankings. There is no `Total` country or category row, so summing the real rows is
the true total (no double counting)."""

import io
import requests
import pandas as pd
from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata
import logging

logger = logging.getLogger(__name__)

_IRCC = "https://www.ircc.canada.ca/opendata-donneesouvertes/data"
_CTRY = "EN_COUNTRY_OF_CITIZENSHIP"
_RESIDUAL = {"Other", "Other Countries", "Country not stated",
             "Country of Citizenship not stated", "Not stated", "Not Stated"}
_OUT = DATA_DIR / "population"

# Common short names for IRCC's long official country labels (charts + downloads).
_COUNTRY_SHORT = {
    "China, People's Republic of": "China",
    "Korea, Republic of": "South Korea",
    "Korea, Democratic People's Republic of": "North Korea",
    "Iran, Islamic Republic of": "Iran",
    "Cameroon, Federal Republic of": "Cameroon",
    "Venezuela, Bolivarian Republic of": "Venezuela",
    "Tanzania, United Republic of": "Tanzania",
    "Moldova, Republic of": "Moldova",
    "Congo, Democratic Republic of the": "DR Congo",
    "Congo, Republic of the": "Congo",
    "Syria, Arab Republic of": "Syria",
    "Russia, Russian Federation": "Russia",
    "Russian Federation": "Russia",
    "Viet Nam": "Vietnam",
    "Bolivia, Plurinational State of": "Bolivia",
}


def _ircc(name):
    """Download an IRCC open-data CSV (tab-delimited; FR_ columns dropped)."""
    r = requests.get(f"{_IRCC}/{name}", timeout=120)
    r.raise_for_status()
    d = pd.read_csv(io.StringIO(r.text), sep="\t", dtype=str)
    return d[[c for c in d.columns if not c.startswith("FR_")]]


def _num(s):
    """IRCC value string → numeric; '--' (suppressed) and blanks → 0 for summation."""
    return pd.to_numeric(s.astype(str).str.replace("--", "0", regex=False)
                         .str.replace(",", "", regex=False), errors="coerce").fillna(0)


def _complete_years(d):
    """Years with all 12 months present (drops the partial current year)."""
    by = d.groupby("EN_YEAR")["EN_MONTH"].nunique()
    return [y for y, n in by.items() if str(y).isdigit() and n >= 12]


def _save(df, name, source_table, transformations):
    path = _OUT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    save_metadata(path, df=df, date_column="year",
        source="Immigration, Refugees and Citizenship Canada (open data)",
        source_table=source_table, frequency="annual",
        unit="persons (rounded to nearest 5)", transformations=transformations)
    logger.info(f"  saved {len(df)} rows -> {name}")


def _annual_country(d):
    """Sum monthly TOTAL to annual counts per country, complete years, residuals out."""
    d = d.copy()
    d["v"] = _num(d["TOTAL"])
    d = d[d["EN_YEAR"].isin([str(y) for y in _complete_years(d)])
          & ~d[_CTRY].isin(_RESIDUAL)]
    g = d.groupby(["EN_YEAR", _CTRY])["v"].sum().reset_index()
    g.columns = ["year", "country", "count"]
    g["year"] = g["year"].astype(int)
    g["country"] = g["country"].replace(_COUNTRY_SHORT)
    return (g[g["count"] > 0]
            .sort_values(["year", "count"], ascending=[True, False]).reset_index(drop=True))


def fetch_pr_origins():
    """Permanent residents admitted, by source country and by immigration category
    (IRCC 'Permanent Residents – Monthly', ODP-PR-Citz_immcat). Two CSVs."""
    logger.info("Fetching IRCC PR origins (ODP-PR-Citz_immcat)...")
    try:
        d = _ircc("ODP-PR-Citz_immcat.csv")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    MC = "EN_IMMIGRATION_CATEGORY-MAIN_CATEGORY"
    d["v"] = _num(d["TOTAL"])
    yrs = [str(y) for y in _complete_years(d)]
    dd = d[d["EN_YEAR"].isin(yrs)]
    # By category over time (no aggregate category row → the four categories total)
    cat = dd.groupby(["EN_YEAR", MC])["v"].sum().reset_index()
    cat.columns = ["year", "category", "count"]
    cat["year"] = cat["year"].astype(int)
    cat["category"] = cat["category"].map({
        "Economic": "Economic",
        "Sponsored Family": "Family (sponsored)",
        "Resettled Refugee & Protected Person in Canada": "Refugee & protected",
        "All Other Immigration": "Other",
    }).fillna(cat["category"])
    cat = cat[cat["count"] > 0].sort_values(["year", "category"]).reset_index(drop=True)
    if cat.empty:
        return None
    _save(cat, "ircc_pr_by_category.csv", "IRCC ODP-PR-Citz_immcat",
          ["PRs admitted, annual by main immigration category; complete years only"])
    _save(_annual_country(d), "ircc_pr_by_country.csv", "IRCC ODP-PR-Citz_immcat",
          ["PRs admitted, annual by source country (citizenship); residual rows excluded"])
    return cat


def fetch_permit_origins():
    """Study + work (IMP+TFWP) permit holders by source country (IRCC Temporary
    Residents monthly updates). Annual new-permit counts; the work total sums the
    International Mobility Program and Temporary Foreign Worker Program files."""
    logger.info("Fetching IRCC study/work permit origins...")
    try:
        study = _annual_country(_ircc("ODP-TR-Study-IS_CITZ.csv"))
        imp = _annual_country(_ircc("ODP-TR-Work-IMP-CITZ.csv"))
        tfwp = _annual_country(_ircc("ODP-TR-Work-TFWP-CITZ.csv"))
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    if study.empty or imp.empty:
        return None
    _save(study, "ircc_study_by_country.csv", "IRCC ODP-TR-Study-IS_CITZ",
          ["study-permit holders, annual by source country (citizenship)"])
    work = (pd.concat([imp, tfwp]).groupby(["year", "country"])["count"].sum()
            .reset_index().sort_values(["year", "count"], ascending=[True, False])
            .reset_index(drop=True))
    _save(work, "ircc_work_by_country.csv", "IRCC ODP-TR-Work-IMP-CITZ + ODP-TR-Work-TFWP-CITZ",
          ["work-permit holders (IMP + TFWP summed), annual by source country"])
    return study


def fetch_asylum_origins():
    """Asylum claimants by source country (top-25 per period) and total claims over
    time (IRCC 'Asylum Claimants – Monthly'). By-country sums over provinces; the
    total sums the province × claim-office-type file."""
    logger.info("Fetching IRCC asylum origins...")
    try:
        citz = _ircc("ODP-Asylum-Top25CitzProv-LastYear.csv")
        tot = _ircc("ODP-Asylum-PT_OfficeType.csv")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    # By country: sum over provinces, annual, complete years, residuals excluded
    citz["v"] = _num(citz["TOTAL"])
    cy = [str(y) for y in _complete_years(citz)]
    bc = citz[citz["EN_YEAR"].isin(cy) & ~citz[_CTRY].isin(_RESIDUAL)]
    bc = bc.groupby(["EN_YEAR", _CTRY])["v"].sum().reset_index()
    bc.columns = ["year", "country", "count"]
    bc["year"] = bc["year"].astype(int)
    bc["country"] = bc["country"].replace(_COUNTRY_SHORT)
    bc = bc[bc["count"] > 0].sort_values(["year", "count"], ascending=[True, False]).reset_index(drop=True)
    if bc.empty:
        return None
    _save(bc, "ircc_asylum_by_country.csv", "IRCC ODP-Asylum-Top25CitzProv",
          ["asylum claimants, annual by country (top-25 per period), summed over provinces"])
    # Total claims over time (province × office type → sum is the full total)
    tot["v"] = _num(tot["TOTAL"])
    ty = [str(y) for y in _complete_years(tot)]
    tt = tot[tot["EN_YEAR"].isin(ty)].groupby("EN_YEAR")["v"].sum().reset_index()
    tt.columns = ["year", "count"]
    tt["year"] = tt["year"].astype(int)
    tt = tt[tt["count"] > 0].sort_values("year").reset_index(drop=True)
    _save(tt, "ircc_asylum_total.csv", "IRCC ODP-Asylum-PT_OfficeType",
          ["total asylum claims, annual (all provinces and claim-office types)"])
    return bc


def fetch_asylum_outcomes():
    """Refugee-protection decision outcomes over time — the share of finalized asylum
    claims accepted (the IRB 'acceptance rate'). Immigration and Refugee Board, Refugee
    Protection Division open data (open.canada.ca dataset 6e47f705); the latest year-end
    revised file carries the full back-history (2013–). Acceptance rate = positive ÷
    (positive + negative) decisions; 'Other' (abandoned/withdrawn) is reported but
    excluded from the rate. Tidy: year, accepted, rejected, other, acceptance_rate."""
    logger.info("Fetching IRB asylum decision outcomes (RPD)...")
    try:
        meta = requests.get("https://open.canada.ca/data/api/3/action/package_show",
                            params={"id": "6e47f705-71ed-41f0-8fd5-d1a8508a3b63"},
                            timeout=60).json()
        cands = []
        for x in meta["result"]["resources"]:
            u = x.get("url", "")
            import re as _re
            m = _re.search(r"rpd-open-data-(20\d{2})-q4", u, _re.I)
            if x.get("format", "").upper() == "CSV" and u.endswith("eng-revised.csv") and m:
                cands.append((int(m.group(1)), u))
        url = max(cands)[1]                      # latest year-end file = full history (2013–)
        d = pd.read_csv(io.StringIO(requests.get(url, timeout=120).text), dtype=str)
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    d.columns = [c.strip() for c in d.columns]
    if "Outcome" not in d.columns or "Total" not in d.columns:
        logger.error(f"  unexpected IRB columns: {list(d.columns)}")
        return None
    d["n"] = pd.to_numeric(d["Total"].astype(str).str.replace(",", "", regex=False),
                           errors="coerce").fillna(0)
    g = d.groupby(["Year", "Outcome"])["n"].sum().unstack(fill_value=0).reset_index()
    for c in ("Positive", "Negative", "Other"):
        if c not in g.columns:
            g[c] = 0
    g["year"] = pd.to_numeric(g["Year"], errors="coerce")
    g = g.dropna(subset=["year"])
    decided = g["Positive"] + g["Negative"]
    g["acceptance_rate"] = (g["Positive"] / decided.where(decided > 0) * 100).round(1)
    out = (g.rename(columns={"Positive": "accepted", "Negative": "rejected", "Other": "other"})
             [["year", "accepted", "rejected", "other", "acceptance_rate"]]
             .dropna(subset=["acceptance_rate"]))
    out["year"] = out["year"].astype(int)
    out = out.sort_values("year").reset_index(drop=True)
    if out.empty:
        return None
    _save(out, "ircc_asylum_outcomes.csv", "IRB Refugee Protection Division decisions (open.canada.ca 6e47f705)",
          ["asylum decision outcomes by year; acceptance rate = positive / (positive + negative)"])
    return out
