"""
Fetchers for the **Government** section (workforce + federal spending).

These are bespoke (source="custom") because every series here is multi-dimensional
(by level of government, by sector, by spending category, by department) — the
generic single-series StatCan fetcher can't reshape them. Three source families:

  * **Statistics Canada** bulk CSV-zip tables (same endpoint as fetch_statcan):
    employment by level of government (36-10-0489-01), the archived full
    public-sector composition (10-10-0025-01), federal revenue/expenditure back
    to 1961 (36-10-0477-01) + nominal GDP for the %-of-GDP framing (36-10-0222-01),
    federal expense by economic type incl. compensation (10-10-0016-01), and
    all-government spending by function/COFOG (10-10-0005-01).
  * **Treasury Board "Federal public service statistics"** open data on
    open.canada.ca (CKAN package f0d12b41…): the federal public-service headcount
    total + by department / age / tenure / region / language / sex, 2010–2025.
  * **GC InfoBase** committed open-data CSVs (github TBS-EACPD/infobase /data):
    federal spending by standard object (incl. Personnel) and by department.

Editorial note: the size of government and the level of spending are politically
contested, so everything here is descriptive — clear scope definitions, neutral
framing, no "good/bad" scorecard valence (see the .qmd pages).
"""

import io
import re
import zipfile
import logging

import pandas as pd
import requests

from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata, validate_columns, SchemaError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

OUT_DIR = DATA_DIR / "government"

# GC InfoBase committed data (pin to a branch; master moves ~weekly).
INFOBASE_RAW = "https://raw.githubusercontent.com/TBS-EACPD/infobase/master/data"
# Treasury Board "Federal public service statistics" CKAN package.
TBS_FPS_PACKAGE = "f0d12b41-54dc-4784-ad2b-83dffed2ab84"
CKAN_PACKAGE_SHOW = "https://open.canada.ca/data/api/3/action/package_show?id={}"


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #
def _read_csv_bytes(content, **kw):
    """Read CSV bytes, tolerating BOM (utf-8-sig) and latin-1 fallbacks."""
    for enc in ("utf-8-sig", "latin-1"):
        try:
            return pd.read_csv(io.BytesIO(content), encoding=enc, dtype=str,
                               low_memory=False, **kw)
        except (UnicodeDecodeError, UnicodeError):
            continue
    return pd.read_csv(io.BytesIO(content), dtype=str, low_memory=False, **kw)


def _statcan(table_id):
    """Download a StatCan table's bulk CSV-zip and return the data CSV.

    `table_id` is the dotted PID (e.g. "36-10-0489-01"); the first three groups
    form the 8-digit download id. Reads with utf-8-sig so the BOM these files
    carry on the first header doesn't corrupt the REF_DATE column name.
    """
    num = "".join(table_id.split("-")[:3])
    url = f"https://www150.statcan.gc.ca/n1/tbl/csv/{num}-eng.zip"
    logger.info(f"  StatCan {table_id}: {url}")
    r = requests.get(url, timeout=180)
    r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = [n for n in z.namelist()
                 if n.lower().endswith(".csv") and "metadata" not in n.lower()]
        if not names:
            raise ValueError(f"No data CSV in zip for {table_id}")
        return _read_csv_bytes(z.read(names[0]))


def _infobase(name):
    """Fetch a committed GC InfoBase /data CSV by filename."""
    url = f"{INFOBASE_RAW}/{name}"
    logger.info(f"  GC InfoBase: {url}")
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return _read_csv_bytes(r.content)


def _tbs_resources():
    """Resource list for the TBS Federal public service statistics package."""
    r = requests.get(CKAN_PACKAGE_SHOW.format(TBS_FPS_PACKAGE), timeout=120)
    r.raise_for_status()
    return r.json()["result"]["resources"]


def _tbs_csv(resources, must_contain, must_not_contain=()):
    """Resolve one TBS CSV by stable URL-slug match (not the volatile dated
    filename), preferring the English file ('march' over French 'mars'), then
    download it (the open.canada.ca URL 302-redirects to a signed blob — requests
    follows by default). Returns a DataFrame.
    """
    cands = []
    for res in resources:
        if (res.get("format", "") or "").upper() != "CSV":
            continue
        u = (res.get("url", "") or "").lower()
        if all(k in u for k in must_contain) and not any(k in u for k in must_not_contain):
            cands.append(res["url"])
    if not cands:
        raise SchemaError(f"TBS resource not found for slug {must_contain}")
    cands.sort(key=lambda u: ("mars" in u.lower(), len(u)))   # english first
    logger.info(f"  TBS FPS: {cands[0]}")
    resp = requests.get(cands[0], timeout=120)        # follows 302 to signed blob
    resp.raise_for_status()
    return _read_csv_bytes(resp.content)


def _num(s):
    """StatCan/TBS value → float. TBS suppresses small cells with '*' (read as 0
    per TBS's own convention so single-dimension breakdowns reconcile to totals)."""
    return pd.to_numeric(pd.Series(s).astype(str).str.replace("*", "0", regex=False),
                         errors="coerce")


def _save(df, filename, *, source, source_table, unit, frequency,
          latest_observation_date, transformations, notes=None):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / filename
    df.to_csv(out, index=False)
    save_metadata(out, df=df, source=source, source_table=source_table,
                  frequency=frequency, unit=unit,
                  latest_observation_date=str(latest_observation_date),
                  transformations=transformations, notes=notes)
    logger.info(f"  saved {len(df)} rows -> {out.name}")
    return df


# --------------------------------------------------------------------------- #
# WORKFORCE
# --------------------------------------------------------------------------- #
NAICS = "North American Industry Classification System (NAICS)"

# NAICS public-administration members → friendly level labels. Federal [911]
# includes National Defence (military); [911A] is the "except defence" civilian
# slice (kept so the page can note the distinction).
_LEVEL_MAP = {
    "Public administration [91]": "All public administration",
    "Federal government public administration [911]": "Federal",
    "Federal government public administration (except defence) [911A]": "Federal (civilian, excl. defence)",
    "Provincial and territorial public administration [912]": "Provincial / territorial",
    "Local, municipal, regional and indigenous public administration [91A]": "Local & Indigenous",
}


def fetch_govt_employment_by_level():
    """Government employment by level of government (SNA jobs), 1997–.

    StatCan 36-10-0489-01. NAICS "public administration" = civil administration
    only (teachers, nurses, professors sit in the education/health industries,
    NOT here — that gap is the point of the separate public-sector-composition
    chart). Federal [911] includes the military; we also keep [911A] (except
    defence) for the civilian comparison.
    """
    logger.info("fetch_govt_employment_by_level (36-10-0489-01)")
    try:
        df = _statcan("36-10-0489-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    validate_columns(df, ["REF_DATE", "GEO", "Labour statistics", NAICS, "VALUE"],
                     "govt_employment_by_level")
    df = df[(df["GEO"] == "Canada")
            & (df["Labour statistics"] == "Total number of jobs")
            & (df[NAICS].isin(_LEVEL_MAP))].copy()
    df["jobs"] = _num(df["VALUE"])
    df["level"] = df[NAICS].map(_LEVEL_MAP)
    df["year"] = df["REF_DATE"].astype(int)
    out = (df[["year", "level", "jobs"]].dropna(subset=["jobs"])
           .sort_values(["year", "level"]).reset_index(drop=True))
    if out.empty:
        return None
    return _save(out, "statcan_govt_employment_by_level.csv",
                 source="Statistics Canada", source_table="Statistics Canada 36-10-0489-01",
                 unit="jobs", frequency="annual",
                 latest_observation_date=out["year"].max(),
                 transformations=["GEO=Canada, Total number of jobs, NAICS public-administration levels"])


# Public-sector workforce by industry, from the LFS "class of worker" cube
# (14-10-0027-01): "Public sector employees" cross-tabbed by NAICS. The three
# aggregate NAICS rows are dropped so the individual sectors sum to the total.
_PS_AGG_DROP = {"Total employed, all industries", "Services-producing sector",
                "Goods-producing sector"}


def fetch_public_sector_composition():
    """The public-sector workforce by sector, 1976– (CURRENT).

    StatCan 14-10-0027-01 (Labour Force Survey, "Employment by class of worker"):
    "Public sector employees" — anyone whose employer is government, a public
    institution, or a government business enterprise — cross-tabbed by industry.
    This is the up-to-date way to size "the bureaucracy" against the whole public
    sector: public administration (all levels of government) sits alongside the
    larger public education and public health-care workforces, plus Crown
    corporations and other public services. Replaces the discontinued
    institutional-sector table 10-10-0025-01 (which ended in 2011); the
    federal/provincial/local split of public administration is the chart above.
    """
    logger.info("fetch_public_sector_composition (14-10-0027-01, LFS class of worker)")
    try:
        df = _statcan("14-10-0027-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    naics = "North American Industry Classification System (NAICS)"
    validate_columns(df, ["REF_DATE", "GEO", "Class of worker", naics, "Gender", "VALUE"],
                     "public_sector_composition")
    df = df[(df["GEO"] == "Canada")
            & (df["Class of worker"] == "Public sector employees")
            & (df["Gender"] == "Total - Gender")
            & (~df[naics].isin(_PS_AGG_DROP))].copy()
    df["employment"] = _num(df["VALUE"]) * 1000.0          # LFS reports thousands of persons
    df["year"] = df["REF_DATE"].astype(int)
    df["sector"] = df[naics].str.replace(r"\s*\[.*\]$", "", regex=True).str.strip()
    out = (df.groupby(["year", "sector"])["employment"].sum().round(0).reset_index())
    out = out[out["employment"] > 0].sort_values(["year", "sector"]).reset_index(drop=True)
    if out.empty:
        return None
    return _save(out, "statcan_public_sector_composition.csv",
                 source="Statistics Canada",
                 source_table="Statistics Canada 14-10-0027-01 (Labour Force Survey)",
                 unit="persons employed", frequency="annual",
                 latest_observation_date=out["year"].max(),
                 transformations=["GEO=Canada, Public sector employees, by NAICS industry (aggregate rows dropped); thousands→persons"])


def fetch_fps_population():
    """Federal public service total headcount, 2010–2025 (TBS, as of March 31).

    Universe = core public administration (CPA) + separate agencies (SA); excludes
    RCMP regular/civilian members and the Canadian Armed Forces. Output carries
    CPA, SA, and Total rows per year.
    """
    logger.info("fetch_fps_population (TBS FPS total)")
    try:
        res = _tbs_resources()
        df = _tbs_csv(res, ["public-service-march"])
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    validate_columns(df, ["Year", "Universe", "Number of employees"], "fps_population")
    df["employees"] = _num(df["Number of employees"])
    df["year"] = df["Year"].astype(int)
    seg = df.rename(columns={"Universe": "segment"})[["year", "segment", "employees"]]
    tot = seg.groupby("year")["employees"].sum().reset_index().assign(segment="Total")
    out = (pd.concat([seg, tot[["year", "segment", "employees"]]])
           .sort_values(["year", "segment"]).reset_index(drop=True))
    return _save(out, "tbs_fps_population.csv",
                 source="Treasury Board of Canada Secretariat",
                 source_table="TBS Federal public service statistics (open.canada.ca)",
                 unit="employees (March 31 headcount)", frequency="annual",
                 latest_observation_date=out["year"].max(),
                 transformations=["population total; CPA + SA segments + summed Total"],
                 notes="Excludes RCMP regular/civilian members and the Canadian Armed Forces.")


def fetch_fps_by_department():
    """Federal public service headcount by department/agency, 2010–2025 (TBS).

    Summed across CPA+SA. The roster changes year to year as organizations are
    created, merged and renamed, so the page uses the latest year only.
    """
    logger.info("fetch_fps_by_department (TBS FPS by department)")
    try:
        res = _tbs_resources()
        df = _tbs_csv(res, ["by-department-or-agency"])
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    dept_col = "Department or agency"
    validate_columns(df, ["Year", dept_col, "Number of employees"], "fps_by_department")
    df["employees"] = _num(df["Number of employees"])
    df["year"] = df["Year"].astype(int)
    out = (df.groupby(["year", dept_col])["employees"].sum().reset_index()
           .rename(columns={dept_col: "department"})
           .sort_values(["year", "employees"], ascending=[True, False]).reset_index(drop=True))
    return _save(out, "tbs_fps_by_department.csv",
                 source="Treasury Board of Canada Secretariat",
                 source_table="TBS Federal public service statistics (open.canada.ca)",
                 unit="employees (March 31 headcount)", frequency="annual",
                 latest_observation_date=out["year"].max(),
                 transformations=["headcount by department, CPA+SA summed"])


def fetch_fps_demographics():
    """Federal public service headcount by age / tenure / region / language / sex,
    2010–2025 (TBS), combined into one tidy frame (year, dimension, category,
    employees). Age/tenure/region/language sum CPA+SA; sex comes from the
    department×sex cross-tab (drop the per-department 'Total' rows, then sum)."""
    logger.info("fetch_fps_demographics (TBS FPS age/tenure/region/language/sex)")
    try:
        res = _tbs_resources()
        specs = [
            ("Age band", ["by-age-band"], "Age band"),
            ("Tenure", ["by-tenure"], "Tenure"),
            ("Region", ["by-province-or-territory-march"], "Province or territory of work"),
            ("First official language", ["by-first-official-language"], "First official language"),
        ]
        frames = []
        for dim, slug, catcol in specs:
            d = _tbs_csv(res, slug)
            validate_columns(d, ["Year", catcol, "Number of employees"], f"fps_{dim}")
            d["employees"] = _num(d["Number of employees"])
            d["year"] = d["Year"].astype(int)
            g = (d.groupby(["year", catcol])["employees"].sum().reset_index()
                 .rename(columns={catcol: "category"}))
            g["dimension"] = dim
            frames.append(g[["year", "dimension", "category", "employees"]])

        # Sex: only as a department × sex cross-tab (no Universe column; 'Total'
        # rows are per-department subtotals that would double-count).
        sx = _tbs_csv(res, ["by-department-and-sex"])
        validate_columns(sx, ["Year", "Sex", "Number of employees"], "fps_sex")
        sx = sx[sx["Sex"] != "Total"].copy()
        sx["employees"] = _num(sx["Number of employees"])
        sx["year"] = sx["Year"].astype(int)
        gs = (sx.groupby(["year", "Sex"])["employees"].sum().reset_index()
              .rename(columns={"Sex": "category"}))
        gs["dimension"] = "Sex"
        frames.append(gs[["year", "dimension", "category", "employees"]])
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None

    out = pd.concat(frames).sort_values(["dimension", "year", "category"]).reset_index(drop=True)
    return _save(out, "tbs_fps_demographics.csv",
                 source="Treasury Board of Canada Secretariat",
                 source_table="TBS Federal public service statistics (open.canada.ca)",
                 unit="employees (March 31 headcount)", frequency="annual",
                 latest_observation_date=out["year"].max(),
                 transformations=["age/tenure/region/language summed CPA+SA; sex from dept×sex cross-tab (Total rows dropped)"])


def fetch_fps_executive():
    """Executives vs non-executives in the federal public service over time
    (GC InfoBase org_employee_ex_lvl.csv, 2010–). This is the closest clean
    role-structure axis published as machine-readable data — the full
    occupational-group breakdown (scientists, economists, IT, front-line, etc.)
    is NOT published in any reproducible series (see the page note on why). The
    EX group (ex 01–05) is collapsed to "Executive"; everything else is
    "Non-executive"."""
    logger.info("fetch_fps_executive (GC InfoBase org_employee_ex_lvl.csv)")
    try:
        df = _infobase("org_employee_ex_lvl.csv")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    validate_columns(df, ["Year", "Number of employees"], "fps_executive")
    lvl = next((c for c in df.columns if "exec" in c.lower() or "level" in c.lower()), None)
    if lvl is None:
        logger.error(f"  no executive-level column in {list(df.columns)}")
        return None
    df["employees"] = _num(df["Number of employees"])
    df["year"] = pd.to_numeric(df["Year"], errors="coerce")
    df["category"] = df[lvl].astype(str).str.strip().apply(
        lambda v: "Non-executive" if v.lower().startswith("non") else "Executive")
    out = (df.dropna(subset=["year"]).assign(year=lambda d: d["year"].astype(int))
           .groupby(["year", "category"])["employees"].sum()
           .reset_index().sort_values(["year", "category"]).reset_index(drop=True))
    if out.empty:
        return None
    return _save(out, "infobase_fps_executive.csv",
                 source="Treasury Board of Canada Secretariat (GC InfoBase)",
                 source_table="GC InfoBase employee population by executive level",
                 unit="employees", frequency="annual",
                 latest_observation_date=out["year"].max(),
                 transformations=["EX 01–05 collapsed to Executive vs Non-executive; summed across departments"])


# --------------------------------------------------------------------------- #
# SPENDING
# --------------------------------------------------------------------------- #
def fetch_federal_finance_longrun():
    """Federal revenue, expenditure, balance and interest since 1961, plus the
    same as a share of GDP (1981–, where nominal GDP is available).

    StatCan 36-10-0477-01 (quarterly, unadjusted — flows summed to annual totals)
    for the federal account; 36-10-0222-01 (current-prices GDP at market prices)
    for the denominator. Values in $millions. There is no clean federal
    "compensation of employees" line here — that comes from 10-10-0016-01.
    """
    logger.info("fetch_federal_finance_longrun (36-10-0477-01 + 36-10-0222-01)")
    try:
        fin = _statcan("36-10-0477-01")
        gdp = _statcan("36-10-0222-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    validate_columns(fin, ["REF_DATE", "GEO", "Levels of government", "Estimates",
                           "Seasonal adjustment", "VALUE"], "federal_finance")
    cats = {
        "General governments revenue": "revenue",
        "General governments expenditure": "expenditure",
        "General governments surplus or deficit": "balance",
        "Interest on debt": "interest",
    }
    f = fin[(fin["GEO"] == "Canada")
            & (fin["Levels of government"] == "Federal general government")
            & (fin["Seasonal adjustment"] == "Unadjusted")
            & (fin["Estimates"].isin(cats))].copy()
    f["value"] = _num(f["VALUE"])
    f["year"] = f["REF_DATE"].str[:4].astype(int)
    f["metric"] = f["Estimates"].map(cats)
    # Keep only complete years (4 quarters present for revenue), then sum flows.
    qcount = f[f["metric"] == "revenue"].groupby("year")["REF_DATE"].nunique()
    full = set(qcount[qcount == 4].index)
    f = f[f["year"].isin(full)]
    wide = f.groupby(["year", "metric"])["value"].sum().unstack("metric").reset_index()

    # Nominal GDP (annual, $millions)
    g = gdp[(gdp["GEO"] == "Canada") & (gdp["Prices"] == "Current prices")
            & (gdp["Estimates"] == "Gross domestic product at market prices")].copy()
    g["gdp"] = _num(g["VALUE"])
    g["year"] = g["REF_DATE"].astype(int)
    wide = wide.merge(g[["year", "gdp"]], on="year", how="left")
    for m in ("revenue", "expenditure", "interest"):
        wide[f"{m}_pct_gdp"] = (wide[m] / wide["gdp"] * 100).round(2)
    out = wide.sort_values("year").reset_index(drop=True)
    if out.empty:
        return None
    return _save(out, "statcan_federal_finance.csv",
                 source="Statistics Canada",
                 source_table="Statistics Canada 36-10-0477-01, 36-10-0222-01",
                 unit="$ millions / % of GDP", frequency="annual",
                 latest_observation_date=out["year"].max(),
                 transformations=["Federal general government, unadjusted quarterly flows summed to annual; "
                                  "% of GDP vs current-prices GDP at market prices (1981–)"])


_EXPENSE_MAP = {
    "Revenue [1]": "Revenue",
    "Expense [2]": "Total expense",
    "Compensation of employees [21]": "Compensation of employees",
    "Use of goods and services [22]": "Goods & services",
    "Consumption of fixed capital [23]": "Capital consumption",
    "Interest expense [24]": "Interest on debt",
    "Subsidies [25]": "Subsidies",
    "Grants, expense [26]": "Grants",
    "Social benefits [27]": "Social benefits",
    "Other expense [28]": "Other expense",
}


def fetch_federal_expense_by_type():
    """Federal expense by economic type, 2008– (StatCan 10-10-0016-01).

    The GFSM statement of operations — the one source with a clean federal
    "Compensation of employees" line (the cost of the federal workforce), plus
    grants, social benefits, interest, goods & services, subsidies. Values in
    $millions. Components [21]+[22]+…+[28] sum to Expense [2].
    """
    logger.info("fetch_federal_expense_by_type (10-10-0016-01)")
    try:
        df = _statcan("10-10-0016-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    scol = "Statement of operations and balance sheet"
    validate_columns(df, ["REF_DATE", "GEO", "Public sector components", "Display value", scol, "VALUE"],
                     "federal_expense_by_type")
    df = df[(df["GEO"] == "Canada")
            & (df["Public sector components"] == "Federal  government")   # double space, per source
            & (df["Display value"] == "Transactions and other economic flows")
            & (df[scol].isin(_EXPENSE_MAP))].copy()
    df["value_millions"] = _num(df["VALUE"])
    df["year"] = df["REF_DATE"].astype(int)
    df["category"] = df[scol].map(_EXPENSE_MAP)
    out = (df[["year", "category", "value_millions"]].dropna(subset=["value_millions"])
           .sort_values(["year", "category"]).reset_index(drop=True))
    if out.empty:
        return None
    return _save(out, "statcan_federal_expense_by_type.csv",
                 source="Statistics Canada", source_table="Statistics Canada 10-10-0016-01",
                 unit="$ millions", frequency="annual",
                 latest_observation_date=out["year"].max(),
                 transformations=["Federal government, transactions/flows, expense by economic type"])


_COFOG_MAP = {
    "General public services [701]": "General public services",
    "Defence [702]": "Defence",
    "Public order and safety [703]": "Public order & safety",
    "Economic affairs [704]": "Economic affairs",
    "Environmental protection [705]": "Environmental protection",
    "Housing and community amenities [706]": "Housing & community",
    "Health [707]": "Health",
    "Recreation, culture and religion [708]": "Recreation, culture & religion",
    "Education [709]": "Education",
    "Social protection [710]": "Social protection",
}


def fetch_govt_spending_by_function():
    """Government spending by function (COFOG), all levels consolidated, 2008–.

    StatCan 10-10-0005-01, "Consolidated Canadian general government". NOTE: this
    is ALL governments combined (federal + provincial/territorial + local +
    CPP/QPP) — a federal-only functional split is not published (the consolidated
    residual nets out federal transfers to provinces, so it understates federal
    health/education). The page states this. Values in $millions.
    """
    logger.info("fetch_govt_spending_by_function (10-10-0005-01)")
    try:
        df = _statcan("10-10-0005-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    ccol = "Canadian Classification of Functions of Government (CCOFOG)"
    validate_columns(df, ["REF_DATE", "GEO", "Public sector components", ccol, "VALUE"],
                     "govt_spending_by_function")
    df = df[(df["GEO"] == "Canada")
            & (df["Public sector components"] == "Consolidated Canadian general government")
            & (df[ccol].isin(_COFOG_MAP))].copy()
    df["value_millions"] = _num(df["VALUE"])
    df["year"] = df["REF_DATE"].astype(int)
    df["function"] = df[ccol].map(_COFOG_MAP)
    out = (df[["year", "function", "value_millions"]].dropna(subset=["value_millions"])
           .sort_values(["year", "function"]).reset_index(drop=True))
    if out.empty:
        return None
    return _save(out, "statcan_govt_spending_by_function.csv",
                 source="Statistics Canada", source_table="Statistics Canada 10-10-0005-01 (CCOFOG)",
                 unit="$ millions", frequency="annual",
                 latest_observation_date=out["year"].max(),
                 transformations=["Consolidated Canadian general government, by COFOG division"],
                 notes="All levels of government consolidated — not federal-only.")


def _infobase_sobjs():
    """org_sobjs.csv + glossary names; returns (df with so_num/object/value cols,
    glossary name map). Newest Public-Accounts year column is pa_last_year_1."""
    so = _infobase("org_sobjs.csv")
    gl = _infobase("glossary.csv")
    so["so_num"] = pd.to_numeric(so["so_num"], errors="coerce")
    so["value"] = pd.to_numeric(so["pa_last_year_1"], errors="coerce")
    names = (gl.assign(n=gl["id"].str.extract(r"SOBJ(\d+)$")[0])
             .dropna(subset=["n"]).assign(n=lambda d: d["n"].astype(int))
             .set_index("n")["name_en"].to_dict())
    return so, names


def fetch_federal_spending_by_object():
    """Federal spending by standard object, latest Public Accounts year
    (GC InfoBase org_sobjs.csv). Standard objects 1–12 are the budgetary
    categories (1=Personnel, 10=Transfer payments, 11=Public debt charges);
    21/22 are internal revenue-netting codes and are excluded. Values in dollars.
    """
    logger.info("fetch_federal_spending_by_object (GC InfoBase org_sobjs.csv)")
    try:
        so, names = _infobase_sobjs()
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    budg = so[so["so_num"].between(1, 12)]
    g = budg.groupby("so_num")["value"].sum().reset_index()
    g["object"] = g["so_num"].map(names).str.replace("equipement", "equipment", regex=False)
    total = g["value"].sum()
    g["share_pct"] = (g["value"] / total * 100).round(2)
    out = g.sort_values("value", ascending=False)[["so_num", "object", "value", "share_pct"]]
    out = out.reset_index(drop=True)
    if out.empty or total <= 0:
        return None
    return _save(out, "infobase_federal_by_object.csv",
                 source="Treasury Board of Canada Secretariat (GC InfoBase)",
                 source_table="GC InfoBase / Public Accounts of Canada",
                 unit="$ (latest Public Accounts year)", frequency="annual",
                 latest_observation_date="latest Public Accounts",
                 transformations=["sum standard objects 1–12 across departments, latest PA year; shares of budgetary total"])


def fetch_federal_spending_by_dept():
    """Federal spending by department, latest Public Accounts year (GC InfoBase
    org_sobjs.csv summed over standard objects 1–12), with department names from
    igoc_en.csv. Transfer payments dominate, so finance/benefit departments lead.
    """
    logger.info("fetch_federal_spending_by_dept (GC InfoBase org_sobjs + igoc)")
    try:
        so, _ = _infobase_sobjs()
        ig = _infobase("igoc_en.csv")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    budg = so[so["so_num"].between(1, 12)]
    g = budg.groupby("dept_code")["value"].sum().reset_index()

    def _name(row):
        for c in ("applied_title_en", "legal_title_en", "abbr_en"):
            v = row.get(c)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return row["dept_code"]
    ig["department"] = ig.apply(_name, axis=1)
    names = ig.set_index("dept_code")["department"].to_dict()
    g["department"] = g["dept_code"].map(names).fillna(g["dept_code"])
    out = (g[g["value"] > 0].sort_values("value", ascending=False)
           [["dept_code", "department", "value"]].reset_index(drop=True))
    if out.empty:
        return None
    return _save(out, "infobase_federal_by_dept.csv",
                 source="Treasury Board of Canada Secretariat (GC InfoBase)",
                 source_table="GC InfoBase / Public Accounts of Canada",
                 unit="$ (latest Public Accounts year)", frequency="annual",
                 latest_observation_date="latest Public Accounts",
                 transformations=["sum standard objects 1–12 by department, latest PA year; names via igoc_en"])


if __name__ == "__main__":
    for fn in (fetch_govt_employment_by_level, fetch_public_sector_composition,
               fetch_fps_population, fetch_fps_by_department, fetch_fps_demographics,
               fetch_fps_executive,
               fetch_federal_finance_longrun, fetch_federal_expense_by_type,
               fetch_govt_spending_by_function, fetch_federal_spending_by_object,
               fetch_federal_spending_by_dept):
        try:
            fn()
        except Exception as e:
            logger.error(f"{fn.__name__}: {e}")
