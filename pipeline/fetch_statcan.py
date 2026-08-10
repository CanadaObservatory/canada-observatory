"""
Fetch and clean data from Statistics Canada Web Data Service.
Uses the stats_can library for simplified API access.
"""

import pandas as pd
from pipeline.config import DATA_DIR, STATCAN_TABLES
from pipeline.metadata import save_metadata, validate_columns, SchemaError
from pipeline.release_schedule import next_release_date, SCHEDULE_TITLES
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _get_table(table_id):
    """Download a StatCan table and return as DataFrame.

    Downloads the CSV zip directly from StatCan's bulk download endpoint.
    This avoids caching issues with the stats_can library's h5 store.
    """
    import zipfile, io, requests

    # Build direct download URL (works reliably on fresh CI environments)
    # StatCan table IDs like "17-10-0008-01" → URL uses "17100008" (first 3 groups)
    parts = table_id.split("-")
    table_num = "".join(parts[:3])  # e.g., "17100008"
    url = f"https://www150.statcan.gc.ca/n1/tbl/csv/{table_num}-eng.zip"
    logger.info(f"  Downloading zip from: {url}")

    r = requests.get(url, timeout=120)
    r.raise_for_status()

    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        csv_names = [n for n in z.namelist() if n.endswith('.csv')]
        if not csv_names:
            raise ValueError(f"No CSV found in zip for table {table_id}")
        with z.open(csv_names[0]) as f:
            return pd.read_csv(f, low_memory=False)


def fetch_statcan_indicator(ind):
    """Generic single-series StatCan fetch driven by an Indicator registry entry.

    Downloads the table, applies the entry's equality filters (column -> value),
    and emits a simple [date, <value_col>] CSV for a single Canada-wide series.
    Used for indicators like NHPI, median income, and the low-income rate; the
    multi-geography population tables and CPI keep their bespoke functions.
    """
    logger.info(f"StatCan indicator: {ind.id} ({ind.title})")
    try:
        df = _get_table(ind.statcan_table)
    except Exception as e:
        logger.error(f"  Failed to fetch StatCan table {ind.statcan_table}: {e}")
        return None

    for col, val in ind.statcan_filters.items():
        if col not in df.columns:
            raise SchemaError(
                f"{ind.id}: filter column '{col}' missing from table "
                f"{ind.statcan_table}. Available: {list(df.columns)}"
            )
        df = df[df[col] == val]

    validate_columns(df, ["REF_DATE", "VALUE"], ind.id)
    df = df[["REF_DATE", "VALUE"]].rename(
        columns={"REF_DATE": "date", "VALUE": ind.value_col})
    df["date"] = pd.to_datetime(df["date"].astype(str), format=ind.date_format,
                                errors="coerce")
    df[ind.value_col] = pd.to_numeric(df[ind.value_col], errors="coerce")
    df = (df.dropna(subset=["date", ind.value_col])
            .sort_values("date").reset_index(drop=True))
    if df.empty:
        logger.warning(f"  {ind.id}: no rows after filtering — check filter values")
        return None

    out_path = ind.out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    next_rel = (next_release_date(SCHEDULE_TITLES[ind.release_key])
                if ind.release_key else None)
    save_metadata(out_path, df=df, date_column="date", next_release_date=next_rel,
        source="Statistics Canada",
        source_table=ind.source_table,
        frequency=ind.frequency,
        unit=ind.unit,
        transformations=[f"filtered to {ind.statcan_filters}"],
    )
    logger.info(f"  saved {len(df)} rows -> {out_path.name}")
    return df


def fetch_population_quarterly():
    """
    Fetch quarterly population estimates for Canada and provinces.
    StatCan Table: 17-10-0009-01
    """
    logger.info("Fetching StatCan population estimates (quarterly)...")
    table_id = STATCAN_TABLES["population_quarterly"]

    try:
        df = _get_table(table_id)
    except Exception as e:
        logger.error(f"Failed to fetch StatCan table {table_id}: {e}")
        return None

    # Validate expected columns from StatCan
    validate_columns(df, ["REF_DATE", "GEO", "VALUE"], "population_quarterly")

    # Clean and reshape
    df = df.rename(columns={"REF_DATE": "date", "GEO": "geography", "VALUE": "population"})
    df = df[["date", "geography", "population"]].copy()

    # Convert date
    df["date"] = pd.to_datetime(df["date"])

    # Remove rows with missing population
    df = df.dropna(subset=["population"])

    # Sort
    df = df.sort_values(["geography", "date"]).reset_index(drop=True)

    # Save
    out_path = DATA_DIR / "population" / "statcan_population_quarterly.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df,
        source="Statistics Canada",
        source_table="17-10-0009-01",
        frequency="quarterly",
        unit="persons",
        transformations=["filtered to date, geography, population columns"],
    )
    logger.info(f"Saved {len(df)} rows to {out_path}")

    return df


def fetch_population_components():
    """
    Fetch components of population growth (births, deaths, immigration, emigration).
    StatCan Table: 17-10-0014-01
    """
    logger.info("Fetching StatCan population components...")
    table_id = STATCAN_TABLES["population_components"]

    try:
        df = _get_table(table_id)
    except Exception as e:
        logger.error(f"Failed to fetch StatCan table {table_id}: {e}")
        return None

    validate_columns(df, ["REF_DATE", "GEO", "Components of population growth", "VALUE"],
                     "population_components")

    df = df.rename(columns={
        "REF_DATE": "date",
        "GEO": "geography",
        "Components of population growth": "component",
        "VALUE": "value",
    })
    df = df[["date", "geography", "component", "value"]].copy()

    # Dates are fiscal year format "1971/1972" — use the ending year as July 1
    df["date"] = df["date"].astype(str).str.extract(r"(\d{4})$")[0]
    df["date"] = pd.to_datetime(df["date"], format="%Y")
    df = df.dropna(subset=["value"])
    df = df.sort_values(["geography", "component", "date"]).reset_index(drop=True)

    out_path = DATA_DIR / "population" / "statcan_population_components.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df,
        source="Statistics Canada",
        source_table="17-10-0008-01",
        frequency="quarterly",
        unit="persons",
        transformations=["filtered to date, geography, component, value columns"],
    )
    logger.info(f"Saved {len(df)} rows to {out_path}")

    return df


def fetch_cpi():
    """
    Fetch Consumer Price Index (monthly, all-items) for Canada.
    StatCan Table: 18-10-0004-01
    """
    logger.info("Fetching StatCan CPI (monthly, all-items)...")
    table_id = STATCAN_TABLES["cpi"]

    try:
        df = _get_table(table_id)
    except Exception as e:
        logger.error(f"Failed to fetch StatCan table {table_id}: {e}")
        return None

    validate_columns(df, ["REF_DATE", "GEO", "Products and product groups", "VALUE"], "cpi")

    df = df.rename(columns={
        "REF_DATE": "date",
        "GEO": "geography",
        "Products and product groups": "product_group",
        "VALUE": "cpi_value",
    })
    df = df[["date", "geography", "product_group", "cpi_value"]].copy()
    df_all = df.copy()   # keep every product group before narrowing to All-items

    # Keep only All-items CPI for Canada
    df = df[df["product_group"] == "All-items"]
    df = df[df["geography"] == "Canada"]

    df["date"] = pd.to_datetime(df["date"])
    df["cpi_value"] = pd.to_numeric(df["cpi_value"], errors="coerce")
    df = df.dropna(subset=["cpi_value"])
    df = df.sort_values("date").reset_index(drop=True)

    # Calculate year-over-year inflation rate (12 months back, single series)
    df["inflation_yoy"] = df["cpi_value"].pct_change(periods=12) * 100

    out_path = DATA_DIR / "economics" / "statcan_cpi.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df,
        source="Statistics Canada",
        source_table="18-10-0004-01",
        frequency="monthly",
        unit="index (2002=100)",
        next_release_date=next_release_date(SCHEDULE_TITLES["cpi"]),
        transformations=[
            "filtered to All-items CPI, Canada only",
            "computed year-over-year inflation rate (12-month pct_change)",
        ],
    )
    logger.info(f"Saved {len(df)} rows to {out_path}")

    # Side output: the major-component sub-indices (Canada) for the
    # "where inflation is coming from" breakdown — the 8 standard CPI components
    # plus All-items, monthly index. YoY is computed per component at render time.
    majors = ["All-items", "Food", "Shelter",
              "Household operations, furnishings and equipment",
              "Clothing and footwear", "Transportation", "Health and personal care",
              "Recreation, education and reading",
              "Alcoholic beverages, tobacco products and recreational cannabis"]
    comp = df_all[(df_all["geography"] == "Canada")
                  & (df_all["product_group"].isin(majors))].copy()
    comp["date"] = pd.to_datetime(comp["date"])
    comp["cpi_value"] = pd.to_numeric(comp["cpi_value"], errors="coerce")
    comp = (comp.dropna(subset=["cpi_value"])
            .sort_values(["product_group", "date"]).reset_index(drop=True))
    comp_path = DATA_DIR / "economics" / "statcan_cpi_components.csv"
    comp.to_csv(comp_path, index=False)
    save_metadata(comp_path, df=comp,
        source="Statistics Canada", source_table="18-10-0004-01",
        frequency="monthly", unit="index (2002=100)",
        transformations=["filtered to the 8 major CPI components + All-items, Canada only"])
    logger.info(f"Saved {len(comp)} rows to {comp_path}")

    # Side output: the major FOOD sub-components (Canada) — the grocery categories +
    # restaurants under "Food" — for the food-breakdown figure (review §160). Monthly
    # index; the price change over various windows is computed at render time.
    food_sub = {
        "Meat": "Meat",
        "Fish, seafood and other marine products": "Fish & seafood",
        "Dairy products and eggs": "Dairy & eggs",
        "Bakery and cereal products (excluding baby food)": "Bakery & cereals",
        "Fruit, fruit preparations and nuts": "Fruit & nuts",
        "Vegetables and vegetable preparations": "Vegetables",
        "Other food products and non-alcoholic beverages": "Other foods & beverages",
        "Food purchased from restaurants": "Restaurant meals",
    }
    food = df_all[(df_all["geography"] == "Canada")
                  & (df_all["product_group"].isin(food_sub))].copy()
    food["food_group"] = food["product_group"].map(food_sub)
    food["date"] = pd.to_datetime(food["date"])
    food["cpi_value"] = pd.to_numeric(food["cpi_value"], errors="coerce")
    food = (food.dropna(subset=["cpi_value"])[["date", "food_group", "cpi_value"]]
            .sort_values(["food_group", "date"]).reset_index(drop=True))
    food_path = DATA_DIR / "economics" / "statcan_cpi_food_components.csv"
    food.to_csv(food_path, index=False)
    save_metadata(food_path, df=food, date_column="date",
        source="Statistics Canada", source_table="18-10-0004-01",
        frequency="monthly", unit="index (2002=100)",
        transformations=["Canada; major food sub-components (grocery categories + restaurants)"])
    logger.info(f"Saved {len(food)} rows to {food_path}")

    return df


# Median after-tax income by recipient type (11-10-0190-01), Canada, 2024 constant $.
# A WIDE CSV [date, all, families, individuals] for the income-page family-type
# dropdown (review §228). Kept separate from statcan_median_income.csv (which crea.py
# and the combined series still read) so nothing downstream changes.
MEDIAN_INCOME_FAMILY = {
    "Economic families and persons not in an economic family": "all",
    "Economic families": "families",
    "Persons not in an economic family": "individuals",
}


def fetch_median_income_by_family():
    """Median after-tax income (Canada, 2024 constant $) by recipient type — all /
    economic families / unattached individuals — for the income-page dropdown.
    Wide CSV; the 'all' column mirrors statcan_median_income.csv."""
    logger.info("Fetching StatCan median income by family type (11-10-0190-01)...")
    try:
        df = _get_table("11-10-0190-01")
    except Exception as e:
        logger.error(f"  Failed to fetch StatCan table 11-10-0190-01: {e}")
        return None
    df = df[(df["GEO"] == "Canada")
            & (df["Income concept"] == "Median after-tax income")
            & (df["UOM"] == "2024 constant dollars")
            & (df["Economic family type"].isin(MEDIAN_INCOME_FAMILY))].copy()
    df["col"] = df["Economic family type"].map(MEDIAN_INCOME_FAMILY)
    df["date"] = pd.to_datetime(df["REF_DATE"].astype(str), format="%Y", errors="coerce")
    df["VALUE"] = pd.to_numeric(df["VALUE"], errors="coerce")
    wide = (df.pivot_table(index="date", columns="col", values="VALUE")
              .reset_index().sort_values("date").dropna(subset=["all"]).reset_index(drop=True))
    out_path = DATA_DIR / "income" / "statcan_median_income_by_family.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wide.to_csv(out_path, index=False)
    save_metadata(out_path, df=wide, date_column="date", source="Statistics Canada",
        source_table="11-10-0190-01", frequency="annual", unit="2024 constant dollars",
        transformations=["Canada; median after-tax income by recipient type "
                         "(all / economic families / unattached individuals)"])
    logger.info(f"  saved {len(wide)} rows -> {out_path.name}")
    return wide


# Non-permanent residents by permit type (17-10-0121-01). These five categories
# sum exactly to the published "Total, non-permanent residents" (verified): the
# permit-holder subrows + asylum total + the family-member residual ("Other").
NPR_TYPES = {
    "Work permit holders only": "Work permits",
    "Study permit holders only": "Study permits",
    "Work and study permit holders": "Work + study permits",
    "Total, asylum claimants, protected persons and related groups": "Asylum claimants",
    "Other": "Other",
}


def fetch_npr_by_type():
    """Non-permanent residents by permit type, Canada (StatCan 17-10-0121-01,
    quarterly from 2021) -> data/population/statcan_npr_by_type.csv [date, type,
    count]. The five categories sum to the published NPR total."""
    logger.info("Fetching StatCan non-permanent residents by type (17-10-0121-01)...")
    try:
        df = _get_table("17-10-0121-01")
    except Exception as e:
        logger.error(f"  Failed to fetch StatCan table 17-10-0121-01: {e}")
        return None
    col = "Non-permanent resident types"
    validate_columns(df, ["REF_DATE", "GEO", col, "VALUE"], "npr_by_type")
    df = df[(df["GEO"] == "Canada") & (df[col].isin(NPR_TYPES))].copy()
    df["type"] = df[col].map(NPR_TYPES)
    df["date"] = pd.to_datetime(df["REF_DATE"].astype(str), format="%Y-%m", errors="coerce")
    df["count"] = pd.to_numeric(df["VALUE"], errors="coerce")
    df = (df.dropna(subset=["date", "count"])[["date", "type", "count"]]
            .sort_values(["type", "date"]).reset_index(drop=True))
    if df.empty:
        logger.warning("  npr_by_type: no rows after filtering — check type labels")
        return None
    out_path = DATA_DIR / "population" / "statcan_npr_by_type.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="date",
        source="Statistics Canada", source_table="17-10-0121-01",
        frequency="quarterly", unit="persons",
        transformations=["Canada; non-permanent residents by permit type "
                         "(5 categories summing to the published total)"])
    logger.info(f"  saved {len(df)} rows -> {out_path.name}")
    return df


MINWAGE_URL = ("https://open.canada.ca/data/dataset/390ee890-59bb-4f34-a37c-9732781ef8a0/"
               "resource/2ddfbfd4-8347-467d-b6d5-797c5421f4fb/download/"
               "general-historical-minimum-wage.csv")

_MINWAGE_NAMES = {
    "FJ": "Federal", "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba",
    "NB": "New Brunswick", "NL": "Newfoundland and Labrador", "NS": "Nova Scotia",
    "NWT": "Northwest Territories", "NU": "Nunavut", "ON": "Ontario",
    "PEI": "Prince Edward Island", "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon",
}


def fetch_minimum_wage():
    """General adult minimum wage by jurisdiction, as an annual panel.

    Source: ESDC "Historical Minimum Wage Rates in Canada" (open.canada.ca, OGL-
    Canada). The file lists each rate *change* (jurisdiction, effective date, rate);
    we forward-fill to the rate in effect at year-end for every year, for the 13
    provinces/territories plus the federal jurisdiction. Nominal dollars; the page
    deflates to real using the CPI already fetched. The real-vs-nominal gap is the
    cost-of-living story.

    Gotchas handled: the effective date is `DD-Mon-YY`, so pandas mis-parses the
    1960s rows to 20xx — any parsed year far in the future is shifted back a century.
    The rate is a `$`-prefixed string.
    """
    import requests, io, datetime
    logger.info("Fetching ESDC historical minimum wage...")
    try:
        r = requests.get(MINWAGE_URL, timeout=60)
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
    except Exception as e:
        logger.error(f"  Failed minimum-wage fetch: {e}")
        return None

    validate_columns(df, ["Jurisdiction", "Effective Date", "Minimum Wage"], "minimum_wage")
    df = df.rename(columns={"Jurisdiction": "code", "Effective Date": "eff",
                            "Minimum Wage": "wage_raw"})
    df["date"] = pd.to_datetime(df["eff"], format="%d-%b-%y", errors="coerce")
    # 2-digit years pivot at 1969, so pre-1969 rows land a century too late.
    too_late = df["date"].dt.year > 2040
    df.loc[too_late, "date"] = df.loc[too_late, "date"] - pd.DateOffset(years=100)
    df["wage"] = pd.to_numeric(
        df["wage_raw"].astype(str).str.replace(r"[\$,]", "", regex=True), errors="coerce")
    df["jurisdiction"] = df["code"].map(_MINWAGE_NAMES)
    df = (df.dropna(subset=["date", "wage", "jurisdiction"])
            .drop_duplicates(subset=["code", "date"], keep="last")
            .sort_values(["code", "date"]))
    if df.empty:
        return None

    cur_year = datetime.date.today().year
    rows = []
    for code, g in df.groupby("code"):
        name = g["jurisdiction"].iloc[0]
        for y in range(int(g["date"].dt.year.min()), cur_year + 1):
            asof = pd.Timestamp(year=y, month=12, day=31)
            in_effect = g[g["date"] <= asof]
            if len(in_effect):
                rows.append({"code": code, "jurisdiction": name, "year": y,
                             "min_wage": round(float(in_effect["wage"].iloc[-1]), 2)})
    out = pd.DataFrame(rows).sort_values(["jurisdiction", "year"]).reset_index(drop=True)

    out_path = DATA_DIR / "economics" / "esdc_minimum_wage.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
        source="Employment and Social Development Canada (Historical Minimum Wage Rates in Canada)",
        source_table="open.canada.ca 390ee890",
        frequency="annual", unit="nominal CAD per hour",
        transformations=["rate in effect at year-end, forward-filled per jurisdiction; 14 jurisdictions"])
    logger.info(f"  saved {len(out)} rows ({out.jurisdiction.nunique()} jurisdictions) -> {out_path.name}")
    return out


TURNOUT_URL = "https://www.elections.ca/content.aspx?section=ele&dir=turn&document=index&lang=e"
TURNOUT_AGE_URL = ("https://open.canada.ca/data/dataset/b545fe25-5cf5-4488-9923-b5c2ebeeb8cc/"
                   "resource/73586e35-290d-431c-94ba-5cf8a97c4ae5/download/"
                   "turnout_by_age_gender_and_province_ge38_ge45.csv")
_REFERENDUM_YEARS = {1898, 1942, 1992}   # the only non-election rows in the historical table
_TURNOUT_AGES = ["18 to 24 years", "25 to 34 years", "35 to 44 years", "45 to 54 years",
                 "55 to 64 years", "65 to 74 years", "75 years and over"]


def fetch_voter_turnout():
    """Federal voter turnout: the long historical series (Elections Canada, 1867–)
    and turnout by age group (Elections Canada open data, 2004–).

    The historical series is the turnout table on elections.ca, read with
    pandas.read_html; the three referendum years (1898/1942/1992) are dropped so
    the line is general-elections only. The by-age file is national, all-genders
    turnout for the seven standard age groups — the persistent young-vs-old gap.
    Emits two CSVs (the by-age one as a side output).
    """
    import io, requests
    logger.info("Fetching federal voter turnout (Elections Canada)...")

    # 1. Historical turnout, 1867– (HTML table)
    try:
        tables = pd.read_html(TURNOUT_URL)
    except Exception as e:
        logger.error(f"  Failed historical-turnout fetch: {e}")
        return None
    t = max(tables, key=len).copy()       # the turnout table is the largest on the page
    t.columns = ["date", "population", "electors", "ballots", "turnout"][:len(t.columns)]
    t["year"] = pd.to_numeric(t["date"].astype(str).str.extract(r"(\d{4})")[0], errors="coerce")
    t["turnout"] = pd.to_numeric(
        t["turnout"].astype(str).str.extract(r"([\d.]+)")[0], errors="coerce")
    hist = (t.dropna(subset=["year", "turnout"])
            .astype({"year": int})
            .query("year not in @_REFERENDUM_YEARS")[["year", "turnout"]]
            .drop_duplicates("year").sort_values("year").reset_index(drop=True))
    if hist.empty:
        return None
    out_path = DATA_DIR / "population" / "voter_turnout.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    hist.to_csv(out_path, index=False)
    save_metadata(out_path, df=hist, date_column="year",
        source="Elections Canada (Voter Turnout at Federal Elections and Referendums)",
        source_table="elections.ca turnout table",
        frequency="per election", unit="% of registered electors",
        transformations=["general elections only (1898/1942/1992 referendums dropped)"])
    logger.info(f"  saved {len(hist)} elections -> {out_path.name}")

    # 2. Turnout by age group, national, 2004– (open.canada.ca)
    try:
        r = requests.get(TURNOUT_AGE_URL, timeout=60)
        r.raise_for_status()
        a = pd.read_csv(io.BytesIO(r.content), encoding="utf-8-sig")  # file has a UTF-8 BOM
        a = a[(a["PROVINCE_E"] == "Canada") & (a["GENDER_E"] == "All genders")
              & (a["AGE_GROUP_E"].isin(_TURNOUT_AGES))].copy()
        a = a.rename(columns={"YEAR": "year", "AGE_GROUP_E": "age_group",
                              "TURNOUT_ELIGIBLE_ELECTOR": "turnout"})
        # the open-data turnout is a 0–1 fraction; express as % to match the historical series
        a["turnout"] = pd.to_numeric(a["turnout"], errors="coerce") * 100
        a = (a.dropna(subset=["turnout"])[["year", "age_group", "turnout"]]
             .sort_values(["year", "age_group"]).reset_index(drop=True))
        age_path = DATA_DIR / "population" / "voter_turnout_by_age.csv"
        a.to_csv(age_path, index=False)
        save_metadata(age_path, df=a, date_column="year",
            source="Elections Canada (Turnout by Age, Gender and Province)",
            source_table="open.canada.ca b545fe25",
            frequency="per election", unit="% of eligible electors",
            transformations=["Canada, all genders, 7 standard age groups, 2004–"])
        logger.info(f"  saved {len(a)} by-age rows -> {age_path.name}")
    except Exception as e:
        logger.warning(f"  by-age turnout unavailable (historical series still saved): {e}")

    return hist


def fetch_cma_vacancy():
    """Rental vacancy rate by census metropolitan area, latest year (CMHC Rental
    Market Survey via StatCan 34-10-0127-01) — the by-city spread behind the
    national average (which the all-Canada line alone hides). One row per CMA for
    the most recent reported year; the page draws it as a ranked bar.
    """
    logger.info("Fetching CMA rental vacancy (34-10-0127-01)...")
    try:
        df = _get_table("34-10-0127-01")
    except Exception as e:
        logger.error(f"Failed to fetch vacancy table: {e}")
        return None
    df = df.rename(columns={"REF_DATE": "year", "GEO": "geo", "VALUE": "vacancy_rate"})
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["vacancy_rate"] = pd.to_numeric(df["vacancy_rate"], errors="coerce")
    # CMA-level rows only ("City, Province"); drop the Ottawa-Gatineau "part" rows
    df = df[df["geo"].str.contains(",", na=False)
            & ~df["geo"].str.contains(" part,", na=False)]
    df = df.dropna(subset=["vacancy_rate", "year"])
    df["cma"] = df["geo"].str.split(",").str[0].str.strip()
    latest_year = int(df["year"].max())
    out = (df[df["year"] == latest_year][["cma", "year", "vacancy_rate"]]
           .drop_duplicates("cma").sort_values("vacancy_rate").reset_index(drop=True))
    out["year"] = out["year"].astype(int)
    if out.empty:
        return None
    out_path = DATA_DIR / "housing" / "statcan_cma_vacancy.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
        source="Statistics Canada / CMHC", source_table="34-10-0127-01",
        frequency="annual", unit="vacancy rate (%)",
        transformations=[f"CMA-level rows, latest year ({latest_year})"])
    logger.info(f"  saved {len(out)} CMAs ({latest_year}) -> {out_path.name}")
    return out


def fetch_debt_service_ratio():
    """Household debt service ratio — the share of disposable income absorbed by
    obligated debt payments (interest *and* principal) — from StatCan 11-10-0065-01
    (National Balance Sheet Accounts, seasonally adjusted, quarterly since 1990).

    This is the payment-burden measure that the debt-to-income *level* (the OECD
    household-debt chart on the same page) misses: the DSR climbs when interest
    rates rise even if the debt stock is flat, so it captures the renewal stress of
    the 2022–24 rate cycle. Split into mortgage vs non-mortgage (which sum exactly
    to the total) so the larger, more rate-sensitive mortgage component is visible.
    """
    logger.info("Fetching household debt service ratio (11-10-0065-01)...")
    try:
        df = _get_table("11-10-0065-01")
    except Exception as e:
        logger.error(f"  Failed to fetch DSR table: {e}")
        return None
    df = df[(df["Seasonal adjustment"] == "Seasonally adjusted at annual rates")
            & (df["UOM"] == "Ratio")]
    wanted = {"Debt service ratio": "dsr_total",
              "Mortgage debt service ratio": "dsr_mortgage",
              "Non-mortgage debt service ratio": "dsr_nonmortgage",
              "Debt service ratio, interest only": "dsr_interest_only"}
    df = df[df["Estimates"].isin(wanted)].copy()
    df["value"] = pd.to_numeric(df["VALUE"], errors="coerce")
    df["date"] = pd.to_datetime(df["REF_DATE"], format="%Y-%m", errors="coerce")
    wide = (df.pivot_table(index="date", columns="Estimates", values="value")
              .rename(columns=wanted).reset_index().sort_values("date"))
    wide = wide.dropna(subset=["dsr_total"])
    if wide.empty:
        return None
    cols = ["date"] + [c for c in ("dsr_total", "dsr_mortgage", "dsr_nonmortgage",
                                   "dsr_interest_only") if c in wide.columns]
    out_path = DATA_DIR / "housing" / "statcan_debt_service_ratio.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wide[cols].to_csv(out_path, index=False)
    save_metadata(out_path, df=wide, date_column="date",
        source="Statistics Canada", source_table="11-10-0065-01",
        frequency="quarterly", unit="% of disposable income (seasonally adjusted)",
        transformations=["Seasonally adjusted at annual rates",
                         "total / mortgage / non-mortgage debt service ratio"])
    logger.info(f"  saved {len(wide)} quarters -> {out_path.name}")
    return wide[cols]


def fetch_provincial_finance():
    """Provincial government finances on the comparable StatCan CGFS basis
    (Canadian Government Finance Statistics, table 10-10-0017-01), normalised by
    provincial GDP (36-10-0222-01, current prices).

    The CGFS basis is consistent ACROSS provinces — unlike each province's own
    Public Accounts, which use differing conventions — so it's the right source
    for a cross-province comparison (the figures will not match a province's own
    budget documents). Flows (revenue, expense, net operating balance) are the
    'Transactions and other economic flows' display; the balance-sheet position
    (net financial worth) is the 'Stocks' display. Net debt = − net financial
    worth. Ten provinces only — the territories are tiny and largely federally
    funded, which would distort a %-of-GDP comparison.
    """
    logger.info("Fetching provincial government finances (10-10-0017-01 + GDP)...")
    PROV = ["Newfoundland and Labrador", "Prince Edward Island", "Nova Scotia",
            "New Brunswick", "Quebec", "Ontario", "Manitoba", "Saskatchewan",
            "Alberta", "British Columbia"]
    try:
        cg = _get_table("10-10-0017-01")
        gd = _get_table("36-10-0222-01")
    except Exception as e:
        logger.error(f"  Failed provincial-finance fetch: {e}")
        return None
    SOB = "Statement of operations and balance sheet"
    cg = cg[(cg["Public sector components"] == "Provincial and territorial governments")
            & (cg["GEO"].isin(PROV))].copy()
    cg["VALUE"] = pd.to_numeric(cg["VALUE"], errors="coerce")
    cg["year"] = pd.to_numeric(cg["REF_DATE"], errors="coerce")
    flows = {"Revenue [1]": "revenue", "Expense [2]": "expense",
             "Net operating balance": "net_op_balance"}
    fl = (cg[(cg[SOB].isin(flows)) & (cg["Display value"] == "Transactions and other economic flows")]
          .assign(item=lambda d: d[SOB].map(flows))
          .pivot_table(index=["GEO", "year"], columns="item", values="VALUE"))
    st = (cg[(cg[SOB] == "Net financial worth") & (cg["Display value"] == "Stocks")]
          .groupby(["GEO", "year"])["VALUE"].first().rename("net_fin_worth"))
    fin = fl.join(st).reset_index()
    g = gd[(gd["Prices"] == "Current prices")
           & (gd["Estimates"] == "Gross domestic product at market prices")
           & (gd["GEO"].isin(PROV))].copy()
    g["gdp"] = pd.to_numeric(g["VALUE"], errors="coerce")
    g["year"] = pd.to_numeric(g["REF_DATE"], errors="coerce")
    out = fin.merge(g[["GEO", "year", "gdp"]], on=["GEO", "year"], how="inner").dropna(subset=["gdp"])
    out["revenue_pct_gdp"] = out["revenue"] / out["gdp"] * 100
    out["expense_pct_gdp"] = out["expense"] / out["gdp"] * 100
    out["balance_pct_gdp"] = out["net_op_balance"] / out["gdp"] * 100
    out["net_debt_pct_gdp"] = -out["net_fin_worth"] / out["gdp"] * 100
    out = (out.rename(columns={"GEO": "province"})
           .sort_values(["province", "year"]).reset_index(drop=True))
    out["year"] = out["year"].astype(int)
    if out.empty:
        return None
    out_path = DATA_DIR / "government" / "statcan_provincial_finance.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
        source="Statistics Canada", source_table="10-10-0017-01; 36-10-0222-01",
        frequency="annual", unit="$ millions and % of provincial GDP (CGFS basis)",
        transformations=["CGFS provincial-territorial governments, 10 provinces; "
                         "flows=Transactions, net financial worth=Stocks; "
                         "normalised by current-price provincial GDP"])
    logger.info(f"  saved {len(out)} rows ({out.province.nunique()} provinces) -> {out_path.name}")
    return out


_INCOME_DECILES = ["Lowest decile", "Second decile", "Third decile", "Fourth decile",
                   "Fifth decile", "Sixth decile", "Seventh decile", "Eighth decile",
                   "Ninth decile", "Highest decile"]


def fetch_income_distribution():
    """Income distribution over time (StatCan 11-10-0193-01, Canada, 1976–).

    Emits two tidy CSVs:
    - income_deciles_avg.csv — average **after-tax** income by decile, in constant
      dollars (real), per year → the decile year-slider (the distribution stretches
      as real incomes grow and the top decile pulls away).
    - income_top_bottom_share.csv — the share of income held by the top 10% (highest
      decile) and the bottom 40% (four lowest deciles), for both **market** and
      **after-tax** income → the inequality-over-time line with a redistribution
      toggle (taxes and transfers narrow the gap).
    """
    logger.info("Fetching income distribution by decile (11-10-0193-01)...")
    try:
        df = _get_table("11-10-0193-01")
    except Exception as e:
        logger.error(f"Failed to fetch income-distribution table: {e}")
        return None
    ca = df[df["GEO"] == "Canada"].copy()
    ca["year"] = pd.to_numeric(ca["REF_DATE"], errors="coerce")
    ca["VALUE"] = pd.to_numeric(ca["VALUE"], errors="coerce")

    # 1. Average income by decile (real, constant 2024 $) — before tax (total
    #    income) and after tax — for the year-slider distribution chart.
    _CONCEPTS = {"Adjusted total income": "before_tax",
                 "Adjusted after-tax income": "after_tax"}
    a = ca[(ca["Statistics"] == "Average income")
           & (ca["Income concept"].isin(_CONCEPTS))
           & (ca["Income decile"].isin(_INCOME_DECILES))].copy()
    a["concept"] = a["Income concept"].map(_CONCEPTS)
    avg = (a.pivot_table(index=["year", "Income decile"], columns="concept", values="VALUE")
           .reset_index().rename(columns={"Income decile": "decile"}))
    avg = avg.dropna(subset=["after_tax"])
    avg["year"] = avg["year"].astype(int)
    if avg.empty:
        return None
    avg_path = DATA_DIR / "income" / "income_deciles_avg.csv"
    avg_path.parent.mkdir(parents=True, exist_ok=True)
    avg.sort_values(["year", "decile"]).to_csv(avg_path, index=False)
    save_metadata(avg_path, df=avg, date_column="year",
        source="Statistics Canada", source_table="11-10-0193-01",
        frequency="annual", unit="average income by decile (constant 2024 $)",
        transformations=["Canada; average before-tax (total) and after-tax income by decile, real $"])
    logger.info(f"  saved {len(avg)} rows -> {avg_path.name}")

    # 2. Top-10% vs bottom-40% income share, market + after-tax — for the share line
    rows = []
    for concept, label in [("Adjusted market income", "Market income"),
                           ("Adjusted after-tax income", "After-tax income")]:
        sh = ca[(ca["Statistics"] == "Share of income")
                & (ca["Income concept"] == concept)
                & (ca["Income decile"].isin(_INCOME_DECILES))]
        piv = sh.pivot_table(index="year", columns="Income decile", values="VALUE")
        for y, r in piv.iterrows():
            top10 = r.get("Highest decile")
            bottom40 = r.reindex(_INCOME_DECILES[:4]).sum()
            if pd.notna(top10):
                rows.append({"year": int(y), "concept": label,
                             "top10": round(float(top10), 1), "bottom40": round(float(bottom40), 1)})
    share = pd.DataFrame(rows).sort_values(["concept", "year"]).reset_index(drop=True)
    share_path = DATA_DIR / "income" / "income_top_bottom_share.csv"
    share.to_csv(share_path, index=False)
    save_metadata(share_path, df=share, date_column="year",
        source="Statistics Canada", source_table="11-10-0193-01",
        frequency="annual", unit="% share of income",
        transformations=["Canada; top-10% (highest decile) and bottom-40% (4 lowest) shares; market + after-tax"])
    logger.info(f"  saved {len(share)} rows -> {share_path.name}")
    return avg


PROVINCES = ["Newfoundland and Labrador", "Prince Edward Island", "Nova Scotia",
             "New Brunswick", "Quebec", "Ontario", "Manitoba", "Saskatchewan",
             "Alberta", "British Columbia"]


def fetch_provincial_electricity():
    """Provincial electricity generation mix, with fossil fuels split by type.

    The base mix comes from StatCan 25-10-0015-01 (latest 12 months, generation
    by turbine type → Hydro / Nuclear / Wind / Solar / Biomass + a single fossil
    total). That table only knows turbine type, not fuel, so the fossil slice is
    split into Coal / Natural gas / Oil using the fuel-level generation shares
    from 25-10-0084-01 (latest annual). Coal matters because it emits roughly
    twice the CO2 of natural gas per kWh. This is generation (output), not
    installed capacity.
    """
    logger.info("Fetching StatCan provincial electricity generation (with fuel split)...")
    try:
        gen = _get_table("25-10-0015-01")
        fuel = _get_table("25-10-0084-01")
    except Exception as e:
        logger.error(f"  failed to fetch provincial electricity tables: {e}")
        return None

    # --- base mix from 25-10-0015 (latest 12 months) ---
    TYPE = "Type of electricity generation"
    CLASS = "Class of electricity producer"
    validate_columns(gen, ["REF_DATE", "GEO", CLASS, TYPE, "VALUE"], "provincial_electricity")
    gen = gen[(gen[CLASS] == "Total all classes of electricity producer")
              & gen["GEO"].isin(PROVINCES)].copy()
    gen["VALUE"] = pd.to_numeric(gen["VALUE"], errors="coerce")
    months = sorted(gen["REF_DATE"].dropna().unique())[-12:]
    gen = gen[gen["REF_DATE"].isin(months)]

    FOSSIL = "Total electricity production from non-renewable combustible fuels"
    base_map = {
        "Hydraulic turbine": "Hydro",
        "Nuclear steam turbine": "Nuclear",
        "Wind power turbine": "Wind",
        "Solar": "Solar",
        "Total electricity production from biomass": "Biomass",
        FOSSIL: "Fossil",
    }
    gen = gen[gen[TYPE].isin(base_map)].copy()
    gen["bucket"] = gen[TYPE].map(base_map)
    base = gen.groupby(["GEO", "bucket"])["VALUE"].sum().unstack(fill_value=0.0)

    # --- fossil split (Coal/Natural gas/Oil) from 25-10-0084 (latest annual) ---
    NAICS = "North American Industry Classification System (NAICS)"
    validate_columns(fuel, ["REF_DATE", "GEO", NAICS, "Fuel type", "UOM", "VALUE"],
                     "provincial_electricity_fuel")
    fuel = fuel[(fuel["UOM"] == "Megawatt hours")
                & (fuel[NAICS] == "Total all classes of electricity")
                & fuel["GEO"].isin(PROVINCES)].copy()
    fuel["VALUE"] = pd.to_numeric(fuel["VALUE"], errors="coerce")
    fyear = sorted(fuel["REF_DATE"].dropna().unique())[-1]
    fuel = fuel[fuel["REF_DATE"] == fyear]
    fuel_map = {
        "Total coal, electricity generated": "Coal",
        "Natural gas, electricity generated": "Natural gas",
        "Total petroleum products, electricity generated": "Oil",
    }
    fuel = fuel[fuel["Fuel type"].isin(fuel_map)].copy()
    fuel["fbucket"] = fuel["Fuel type"].map(fuel_map)
    fsplit = fuel.groupby(["GEO", "fbucket"])["VALUE"].sum().unstack(fill_value=0.0)

    # --- combine: split each province's fossil total by its fuel proportions ---
    rows = []
    for prov in PROVINCES:
        if prov not in base.index:
            continue
        b = base.loc[prov]
        fossil_total = float(b.get("Fossil", 0.0))
        if prov in fsplit.index and fsplit.loc[prov].sum() > 0:
            p = fsplit.loc[prov] / fsplit.loc[prov].sum()
            coal, gas, oil = (fossil_total * p.get("Coal", 0.0),
                              fossil_total * p.get("Natural gas", 0.0),
                              fossil_total * p.get("Oil", 0.0))
        else:                                  # no fuel detail → leave as gas
            coal, gas, oil = 0.0, fossil_total, 0.0
        by_src = {"Hydro": float(b.get("Hydro", 0.0)), "Nuclear": float(b.get("Nuclear", 0.0)),
                  "Wind": float(b.get("Wind", 0.0)), "Solar": float(b.get("Solar", 0.0)),
                  "Biomass": float(b.get("Biomass", 0.0)),
                  "Natural gas": gas, "Oil": oil, "Coal": coal}
        total = sum(by_src.values())
        if total <= 0:
            continue
        for src, val in by_src.items():
            rows.append({"province": prov, "source": src,
                         "generation_mwh": val, "share": val / total * 100})
    g = pd.DataFrame(rows)

    out_path = DATA_DIR / "environment" / "statcan_provincial_electricity.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    g.to_csv(out_path, index=False)
    save_metadata(out_path, df=g, latest_observation_date=str(months[-1]),
        source="Statistics Canada", source_table="25-10-0015-01, 25-10-0084-01",
        frequency="annual", unit="% of provincial electricity generation",
        transformations=[f"base mix 25-10-0015-01 latest 12 months ({months[0]}–{months[-1]}); "
                         f"fossil split into Coal/Natural gas/Oil by 25-10-0084-01 fuel shares ({fyear})"])
    logger.info(f"  saved {len(g)} rows -> {out_path.name}")
    return g


def fetch_tuition():
    """University tuition fees by province and level of study (domestic /
    international, undergraduate / graduate). StatCan 37-10-0045-01 (TLAC survey),
    current dollars, academic years 2006/2007–. Multi-series, so bespoke rather
    than the generic single-series fetcher. Tidy: year, geography, level, tuition.
    """
    logger.info("Fetching StatCan university tuition (by level)...")
    try:
        df = _get_table("37-10-0045-01")
    except Exception as e:
        logger.error(f"  failed to fetch tuition table: {e}")
        return None
    LEVEL = "Level of study"
    validate_columns(df, ["REF_DATE", "GEO", LEVEL, "VALUE"], "tuition")
    df = df.rename(columns={"GEO": "geography", LEVEL: "level", "VALUE": "tuition"})
    # REF_DATE is the academic year "2006/2007"; key on the starting calendar year.
    df["year"] = df["REF_DATE"].astype(str).str[:4].astype(int)
    df["tuition"] = pd.to_numeric(df["tuition"], errors="coerce")
    df = (df.dropna(subset=["tuition"])[["year", "geography", "level", "tuition"]]
            .sort_values(["geography", "level", "year"]).reset_index(drop=True))
    if df.empty:
        return None
    out_path = DATA_DIR / "education" / "statcan_tuition.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    maxy = int(df["year"].max())
    save_metadata(out_path, df=df, latest_observation_date=f"{maxy}/{maxy + 1}",
        source="Statistics Canada", source_table="Statistics Canada 37-10-0045-01 (TLAC)",
        frequency="annual", unit="average tuition, current dollars",
        transformations=["tidy: year, geography, level of study, tuition (current $)"])
    logger.info(f"  saved {len(df)} rows -> {out_path.name}")
    return df


def fetch_trade_us():
    """Merchandise trade with the United States vs. all countries (StatCan
    12-10-0011-01, Customs basis, seasonally adjusted, $ millions, monthly 1997–).
    Computes the US export share and trade balances (exports − imports, since the
    by-partner 'Trade Balance' rows are sparse). Tidy wide: date + columns."""
    logger.info("Fetching StatCan merchandise trade (US vs. world)...")
    try:
        df = _get_table("12-10-0011-01")
    except Exception as e:
        logger.error(f"  failed to fetch trade table: {e}")
        return None
    PART = "Principal trading partners"
    validate_columns(df, ["REF_DATE", "GEO", "Trade", "Basis",
                          "Seasonal adjustment", PART, "VALUE"], "trade_us")
    df = df[(df["GEO"] == "Canada") & (df["Basis"] == "Customs")
            & (df["Seasonal adjustment"] == "Seasonally adjusted")].copy()
    df["VALUE"] = pd.to_numeric(df["VALUE"], errors="coerce")

    def ser(trade, partner):
        s = df[(df["Trade"] == trade) & (df[PART] == partner)][["REF_DATE", "VALUE"]]
        return s.dropna().set_index("REF_DATE")["VALUE"]

    out = pd.DataFrame({
        "exports_us": ser("Export", "United States"),
        "imports_us": ser("Import", "United States"),
        "exports_china": ser("Export", "China"),
        "exports_eu": ser("Export", "European Union"),
        "exports_total": ser("Export", "All countries"),
        "imports_total": ser("Import", "All countries"),
    }).dropna()
    if out.empty:
        return None
    # Export shares for the three largest destinations, to scale US dominance
    # against Canada's next-largest markets (the EU and China).
    out["exports_us_share"] = out["exports_us"] / out["exports_total"] * 100
    out["exports_china_share"] = out["exports_china"] / out["exports_total"] * 100
    out["exports_eu_share"] = out["exports_eu"] / out["exports_total"] * 100
    out["balance_us"] = out["exports_us"] - out["imports_us"]
    out["balance_total"] = out["exports_total"] - out["imports_total"]
    out["balance_row"] = out["balance_total"] - out["balance_us"]   # rest of world
    out = out.reset_index().rename(columns={"REF_DATE": "date"})
    out["date"] = pd.to_datetime(out["date"], format="%Y-%m", errors="coerce")
    out = out.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)

    out_path = DATA_DIR / "economics" / "statcan_trade_us.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="date",
        source="Statistics Canada", source_table="Statistics Canada 12-10-0011-01",
        frequency="monthly", unit="$ millions (Customs basis, seasonally adjusted)",
        transformations=["US vs. all-countries exports/imports; export shares for "
                         "the US, China and the EU; balances computed as exports − imports"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out


def fetch_cma_unemployment():
    """Current unemployment rate by census metropolitan area (StatCan Labour Force
    Survey 14-10-0459-01, 3-month moving average, seasonally adjusted).
    Replaces the frozen 2021-Census snapshot on the by-city unemployment map with a
    live, weekly-updating series for the ~41 largest CMAs. Joins to cma_2021.geojson
    via the CMA code parsed from the DGUID (2021S0503<cmauid>); Canada/province rows
    (non-S0503 DGUIDs) drop out."""
    logger.info("Fetching StatCan LFS unemployment by CMA (14-10-0459-01)...")
    try:
        df = _get_table("14-10-0459-01")
    except Exception as e:
        logger.error(f"  failed to fetch LFS CMA table: {e}")
        return None
    validate_columns(df, ["REF_DATE", "GEO", "DGUID", "Labour force characteristics",
                          "Statistics", "Data type", "VALUE"], "cma_unemployment")
    sub = df[(df["Labour force characteristics"] == "Unemployment rate")
             & (df["Statistics"] == "Estimate")
             & (df["Data type"] == "Seasonally adjusted")].copy()
    sub["VALUE"] = pd.to_numeric(sub["VALUE"], errors="coerce")
    sub["cmauid"] = sub["DGUID"].astype(str).str.extract(r"2021S0503(\d+)$")[0].str.zfill(3)
    sub = sub.dropna(subset=["cmauid", "VALUE"])
    if sub.empty:
        return None
    latest = sub["REF_DATE"].max()                       # 3-month MA ending this month
    sub = sub[sub["REF_DATE"] == latest]
    out = (pd.DataFrame({
                "cmauid": sub["cmauid"].values,
                "name": sub["GEO"].str.split(",").str[0].str.strip().values,
                "unemployment": sub["VALUE"].values,
                "period": str(latest)})
           .drop_duplicates("cmauid").sort_values("cmauid").reset_index(drop=True))

    out_path = DATA_DIR / "economics" / "statcan_cma_unemployment.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, source="Statistics Canada",
        next_release_date=next_release_date(SCHEDULE_TITLES["lfs"]),
        source_table="Statistics Canada 14-10-0459-01 (Labour Force Survey)",
        frequency="monthly", unit="unemployment rate (%), 3-month moving average, SA",
        transformations=["LFS unemployment rate by CMA, latest 3-month moving average; "
                         "CMA code parsed from DGUID 2021S0503<cmauid>"])
    logger.info(f"  saved {len(out)} CMAs ({latest}) -> {out_path.name}")
    return out


def fetch_tuition_by_field():
    """Canadian undergraduate tuition by field of study (StatCan 37-10-0003-01,
    TLAC), current dollars. Tidy: year, geography, field, tuition."""
    logger.info("Fetching StatCan university tuition (by field of study)...")
    try:
        df = _get_table("37-10-0003-01")
    except Exception as e:
        logger.error(f"  failed to fetch tuition-by-field table: {e}")
        return None
    FIELD = "Field of study"
    validate_columns(df, ["REF_DATE", "GEO", FIELD, "VALUE"], "tuition_by_field")
    df = df.rename(columns={"GEO": "geography", FIELD: "field", "VALUE": "tuition"})
    df["year"] = df["REF_DATE"].astype(str).str[:4].astype(int)
    df["tuition"] = pd.to_numeric(df["tuition"], errors="coerce")
    df = (df.dropna(subset=["tuition"])[["year", "geography", "field", "tuition"]]
            .sort_values(["geography", "field", "year"]).reset_index(drop=True))
    if df.empty:
        return None
    out_path = DATA_DIR / "education" / "statcan_tuition_by_field.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    maxy = int(df["year"].max())
    save_metadata(out_path, df=df, latest_observation_date=f"{maxy}/{maxy + 1}",
        source="Statistics Canada", source_table="Statistics Canada 37-10-0003-01 (TLAC)",
        frequency="annual", unit="average undergraduate tuition, current dollars",
        transformations=["tidy: year, geography, field of study, tuition (current $)"])
    logger.info(f"  saved {len(df)} rows -> {out_path.name}")
    return df


def fetch_age_structure():
    """
    Population by age and gender, Canada — single years of age (0–100+), broad
    groups, and the median/average-age summary rows (UOM='Years'), 1971–.
    StatCan Table: 17-10-0005-01 (~3.8 MB zip). Drives the population pyramid
    and the median-age trend on the Population page.
    """
    logger.info("Fetching StatCan population by age and gender (17-10-0005-01)...")
    try:
        df = _get_table("17-10-0005-01")
    except Exception as e:
        logger.error(f"Failed to fetch StatCan table 17-10-0005-01: {e}")
        return None

    validate_columns(df, ["REF_DATE", "GEO", "Gender", "Age group", "UOM", "VALUE"],
                     "age_structure")

    df = df[df["GEO"] == "Canada"].rename(columns={
        "REF_DATE": "year", "Gender": "gender",
        "Age group": "age_group", "UOM": "uom", "VALUE": "value",
    })
    df["gender"] = df["gender"].map(
        {"Total - gender": "Total", "Men+": "Men", "Women+": "Women"})
    df = df.dropna(subset=["value", "gender"])
    df = df[["year", "gender", "age_group", "uom", "value"]].copy()
    df["year"] = pd.to_numeric(df["year"], errors="coerce").astype("Int64")
    df = df.sort_values(["year", "gender"]).reset_index(drop=True)

    out_path = DATA_DIR / "population" / "statcan_age_structure.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_path, index=False)
    save_metadata(out_path, df=df, date_column="year",
        source="Statistics Canada",
        source_table="17-10-0005-01",
        frequency="annual",
        unit="persons (median/average age rows in years)",
        transformations=["Canada only; gender labels simplified",
                         "single-year ages + broad groups + median/average age kept"],
    )
    logger.info(f"Saved {len(df)} rows to {out_path}")
    return df


def fetch_npr_share():
    """Non-permanent residents as a share of Canada's population, quarterly, from 2000.

    StatCan publishes the NPR *stock* on a consistent basis only from 2021
    (17-10-0121-01). The longer view reconstructs the stock from StatCan's own
    quarterly net-NPR *flows* (17-10-0040-01) via the demographic-accounting identity
    (stock_t = stock_{t-1} + net_flow_t), anchored to the 2021 published stock and
    cumulated backward — all StatCan-sourced. Divided by the quarterly population
    estimate (17-10-0009-01), which itself includes NPRs. From 2021 the published
    stock is used directly; earlier quarters are the reconstruction. The share is the
    normalised companion to the NPR-by-type counts and the net-NPR growth component."""
    logger.info("Building NPR share of population (17-10-0040 + 17-10-0121 + 17-10-0009)...")
    try:
        flow = _get_table("17-10-0040-01")
        stock = _get_table("17-10-0121-01")
        pop = _get_table("17-10-0009-01")
    except Exception as e:
        logger.error(f"  failed to fetch NPR-share inputs: {e}")
        return None
    # net NPR flows (Canada, quarterly)
    f = flow[(flow["GEO"] == "Canada")
             & (flow["Components of population growth"] == "Net non-permanent residents")].copy()
    f["date"] = pd.to_datetime(f["REF_DATE"], format="%Y-%m", errors="coerce")
    f["net"] = pd.to_numeric(f["VALUE"], errors="coerce")
    f = f.dropna(subset=["date", "net"]).sort_values("date").set_index("date")["net"]
    # published NPR stock (Canada, total) — the modern consistent benchmark
    s = stock[(stock["GEO"] == "Canada")
              & (stock["Non-permanent resident types"] == "Total, non-permanent residents")].copy()
    s["date"] = pd.to_datetime(s["REF_DATE"], format="%Y-%m", errors="coerce")
    s["stock"] = pd.to_numeric(s["VALUE"], errors="coerce")
    s = s.dropna(subset=["date", "stock"]).sort_values("date").set_index("date")["stock"]
    if f.empty or s.empty:
        return None
    # Reconstruct the stock over the flow's quarters: published where available, else
    # cumulate net flows backward from the earliest published benchmark (anchor).
    npr = pd.Series(index=f.index, dtype=float)
    for d in s.index:
        if d in npr.index:
            npr.loc[d] = s.loc[d]
    dates = list(f.index)
    anchor = s.index.min()
    for i in range(len(dates) - 1, -1, -1):           # backward fill below the anchor
        d = dates[i]
        if d < anchor and pd.isna(npr.loc[d]):
            npr.loc[d] = npr.loc[dates[i + 1]] - f.loc[dates[i + 1]]
    for i in range(1, len(dates)):                     # forward fill above published max
        d = dates[i]
        if d > s.index.max() and pd.isna(npr.loc[d]):
            npr.loc[d] = npr.loc[dates[i - 1]] + f.loc[d]
    # population (Canada, quarterly) — includes NPRs, so NPR ÷ population is the share
    p = pop[pop["GEO"] == "Canada"].copy()
    p["date"] = pd.to_datetime(p["REF_DATE"], format="%Y-%m", errors="coerce")
    p["pop"] = pd.to_numeric(p["VALUE"], errors="coerce")
    p = p.dropna(subset=["date", "pop"]).set_index("date")["pop"]
    out = pd.DataFrame({"npr": npr, "population": p}).dropna()
    out["npr_share"] = out["npr"] / out["population"] * 100
    out = out[out.index >= "2000-01-01"].sort_index()
    out.index.name = "date"
    out = out.reset_index()
    if out.empty:
        return None
    out_path = DATA_DIR / "population" / "statcan_npr_share.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out[["date", "npr", "population", "npr_share"]].to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="date", source="Statistics Canada",
        source_table="17-10-0121-01, 17-10-0040-01, 17-10-0009-01",
        frequency="quarterly", unit="% of population",
        transformations=["NPR stock: published (17-10-0121) from 2021, reconstructed earlier "
                         "from cumulated net-NPR flows (17-10-0040) anchored to the 2021 stock; "
                         "share = NPR / quarterly population (17-10-0009); from 2000"])
    logger.info(f"  saved {len(out)} quarters -> {out_path.name}")
    return out


def fetch_interprovincial_migration():
    """
    Net interprovincial migration by province/territory — quarterly in- and
    out-migrants summed to July–June estimate years (labelled by ENDING year),
    1962–. Net = in-migrants − out-migrants; incomplete years are dropped.
    StatCan Table: 17-10-0020-01 (~49 KB zip).
    """
    logger.info("Fetching StatCan interprovincial migration (17-10-0020-01)...")
    try:
        df = _get_table("17-10-0020-01")
    except Exception as e:
        logger.error(f"Failed to fetch StatCan table 17-10-0020-01: {e}")
        return None

    validate_columns(df, ["REF_DATE", "GEO", "Interprovincial migration", "VALUE"],
                     "interprovincial_migration")

    df = df[df["GEO"] != "Canada"].copy()      # national in == out by construction
    wide = (df.pivot_table(index=["REF_DATE", "GEO"],
                           columns="Interprovincial migration", values="VALUE",
                           aggfunc="first")
              .reset_index())
    wide["net"] = wide["In-migrants"] - wide["Out-migrants"]

    # Quarters run Jul/Oct/Jan/Apr — group into July–June years, label by end year.
    d = pd.to_datetime(wide["REF_DATE"], format="%Y-%m")
    wide["year"] = d.dt.year + (d.dt.month >= 7).astype(int)
    g = wide.groupby(["year", "GEO"])["net"].agg(["sum", "count"]).reset_index()
    g = g[g["count"] == 4].rename(columns={"GEO": "geography", "sum": "net_migration"})
    out = g[["year", "geography", "net_migration"]].sort_values(
        ["geography", "year"]).reset_index(drop=True)

    out_path = DATA_DIR / "population" / "statcan_interprovincial_migration.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
        source="Statistics Canada",
        source_table="17-10-0020-01",
        frequency="annual",
        unit="persons (net)",
        transformations=["net = in-migrants − out-migrants",
                         "quarters summed to July–June years labelled by ending year",
                         "incomplete years dropped"],
    )
    logger.info(f"Saved {len(out)} rows to {out_path}")
    return out


if __name__ == "__main__":
    fetch_population_quarterly()
    fetch_population_components()
    fetch_cpi()


def fetch_tertiary_attainment():
    """Tertiary educational attainment (% of the population), Canada vs the OECD
    average, over time, by age group and by attainment level (the tertiary total
    plus its College/CEGEP / Bachelor's / Master's-or-Doctoral components),
    by gender (Total / Men / Women). StatCan 37-10-0130-01.

    The OECD-average benchmark is built into the table's geography dimension, so
    Canada is directly comparable here — unlike the OECD Education-at-a-Glance
    attainment flow, which mixed methodologies and excluded Canada from a clean key.
    """
    logger.info("fetch_tertiary_attainment (37-10-0130-01)")
    try:
        d = _get_table("37-10-0130-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    OECD = "Organisation for Economic Co-operation and Development (OECD) - average"
    age_map = {
        "Total, 25 to 64 years": "25–64 (all)",
        "25 to 34 years": "25–34",
        "35 to 44 years": "35–44",
        "45 to 54 years": "45–54",
        "55 to 64 years": "55–64",
    }
    # The tertiary rollup plus its three components — the college/CEGEP vs
    # university split. The three components sum to the "Tertiary (all)" total.
    level_map = {
        "Tertiary education": "Tertiary (all)",
        "Short-cycle tertiary": "College/CEGEP",
        "Bachelor's level": "Bachelor's",
        "Master's or Doctoral level": "Master's/Doctoral",
    }
    gender_map = {"Total - Gender": "Total", "Men+": "Men", "Women+": "Women"}
    d = d[(d["Education attainment level"].isin(level_map))
          & (d["Gender"].isin(gender_map))
          & (d["GEO"].isin(["Canada", OECD]))
          & (d["Age group"].isin(age_map))].copy()
    if d.empty:
        return None
    d["geo"] = d["GEO"].replace({OECD: "OECD average"})
    d["age_group"] = d["Age group"].map(age_map)
    d["gender"] = d["Gender"].map(gender_map)
    d["level"] = d["Education attainment level"].map(level_map)
    d["year"] = d["REF_DATE"].astype(str).str[:4].astype(int)
    d["pct"] = pd.to_numeric(d["VALUE"], errors="coerce")
    out = (d[["year", "geo", "age_group", "gender", "level", "pct"]]
           .dropna(subset=["pct"])
           .sort_values(["geo", "age_group", "gender", "level", "year"]).reset_index(drop=True))
    if out.empty:
        return None
    out_path = DATA_DIR / "education" / "statcan_tertiary_attainment.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
                  source="Statistics Canada",
                  source_table="Statistics Canada 37-10-0130-01",
                  frequency="annual", unit="% of population (tertiary education)",
                  transformations=["Canada + OECD-average geographies; by gender (Total/Men/Women), "
                                   "age group, and attainment level (tertiary total + College/CEGEP="
                                   "short-cycle, Bachelor's, Master's/Doctoral)"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out


def fetch_grads_by_field():
    """Gender balance of postsecondary graduates by field of study, Canada.

    StatCan 37-10-0135-01 (Postsecondary Student Information System): graduate
    counts by the 11 broad CIP field groupings, filtered to Canada / all ISCED
    levels / all ages / Man+Woman, 1992–. Emits women & men counts plus the
    women's share per field and year — drives the "gender balance by discipline"
    snapshot (latest year) and the women's-share-over-time trend on the Education
    page. This is a graduate FLOW (annual), not the 25–64 attainment stock.
    """
    logger.info("fetch_grads_by_field (37-10-0135-01)")
    try:
        d = _get_table("37-10-0135-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    isced_col = "International Standard Classification of Education (ISCED)"
    # Credential levels kept for the level dropdown (drop upper-secondary,
    # post-secondary-non-tertiary, and "not applicable").
    level_map = {
        "Total, International Standard Classification of Education (ISCED)": "All credentials",
        "Short-cycle tertiary education": "College",
        "Bachelor's or equivalent": "Bachelor's",
        "Master's or equivalent": "Master's",
        "Doctoral or equivalent": "Doctorate",
    }
    # 11 substantive CIP groupings → short display names (drop Total, Unclassified,
    # Other, and the non-credential "Personal improvement and leisure").
    field_short = {
        "Education [1]": "Education",
        "Visual and performing arts, and communications technologies [2]": "Arts & communications",
        "Humanities [3]": "Humanities",
        "Social and behavioural sciences and law [4]": "Social sciences & law",
        "Business, management and public administration [5]": "Business & public admin",
        "Physical and life sciences and technologies [6]": "Physical & life sciences",
        "Mathematics, computer and information sciences [7]": "Math & computer science",
        "Architecture, engineering and related technologies [8]": "Architecture & engineering",
        "Agriculture, natural resources and conservation [9]": "Agriculture & natural resources",
        "Health and related fields [10]": "Health",
        "Personal, protective and transportation services [11]": "Protective & transport services",
    }
    d = d[(d["GEO"] == "Canada")
          & (d["Age group"] == "Total, age group")
          & (d[isced_col].isin(level_map))
          & (d["UOM"] == "Number")
          & (d["Gender"].isin(["Man", "Woman"]))
          & (d["Field of study"].isin(field_short))].copy()
    if d.empty:
        return None
    d["level"] = d[isced_col].map(level_map)
    d["field"] = d["Field of study"].map(field_short)
    d["year"] = d["REF_DATE"].astype(str).str[:4].astype(int)
    d["count"] = pd.to_numeric(d["VALUE"], errors="coerce")
    g = (d.pivot_table(index=["year", "level", "field"], columns="Gender", values="count",
                       aggfunc="sum").reset_index()
         .rename(columns={"Man": "men", "Woman": "women"}))
    g = g.dropna(subset=["men", "women"])
    g["total"] = g["men"] + g["women"]
    g = g[g["total"] > 0]
    g["pct_women"] = (g["women"] / g["total"] * 100).round(1)
    out = (g[["year", "level", "field", "women", "men", "total", "pct_women"]]
           .sort_values(["year", "level", "field"]).reset_index(drop=True))
    if out.empty:
        return None
    out_path = DATA_DIR / "education" / "statcan_grads_by_field.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
                  source="Statistics Canada",
                  source_table="Statistics Canada 37-10-0135-01",
                  frequency="annual", unit="graduates / % women",
                  transformations=["PSIS graduates; Canada; all ages; by ISCED credential level "
                                   "(All/College/Bachelor's/Master's/Doctorate) and 11 broad CIP "
                                   "field groupings; women/men counts + women's share"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out


def fetch_poverty_by_group():
    """MBM poverty rate (the official poverty line) by selected demographic group,
    Canada — the disparity behind the cost-of-living burden. Combines two StatCan
    tables, both filtered to Canada / Market basket measure, 2023 base / Percentage:
    11-10-0135-01 (age & family type) and 11-10-0093-01 (population groups).
    Persons-with-disabilities is omitted (published from a separate source)."""
    logger.info("fetch_poverty_by_group (11-10-0135-01 + 11-10-0093-01)")
    MBM, PCT = "Market basket measure, 2023 base", "Percentage of persons in low income"
    # (source member -> short label), per table; the order also sets a stable sort key.
    g135 = {
        "All persons": "All Canadians",
        "Persons under 18 years": "Children",
        "Persons 65 years and over": "Seniors (65+)",
        "Persons not in an economic family": "Unattached individuals",
    }
    g093 = {
        "Visible minority population": "Racialized groups",
        "Recent immigrants (10 years or less) aged 15 years and over": "Recent immigrants",
        "Indigenous population": "Indigenous population",
    }
    rows = []
    for tid, dim, gmap in [("11-10-0135-01", "Persons in low income", g135),
                           ("11-10-0093-01", "Demographic characteristics", g093)]:
        try:
            d = _get_table(tid)
        except Exception as e:
            logger.error(f"  {tid} failed: {e}")
            continue
        d = d[(d["GEO"] == "Canada") & (d["Low income lines"] == MBM)
              & (d["Statistics"] == PCT) & (d[dim].isin(gmap))].copy()
        d["year"] = d["REF_DATE"].astype(str).str[:4].astype(int)
        d["rate"] = pd.to_numeric(d["VALUE"], errors="coerce")
        d["group"] = d[dim].map(gmap)
        rows.append(d[["year", "group", "rate"]].dropna(subset=["rate"]))
    if not rows:
        return None
    out = (pd.concat(rows, ignore_index=True)
           .sort_values(["year", "group"]).reset_index(drop=True))
    if out.empty:
        return None
    out_path = DATA_DIR / "income" / "statcan_poverty_by_group.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year", source="Statistics Canada",
        source_table="Statistics Canada 11-10-0135-01 and 11-10-0093-01",
        frequency="annual", unit="% in low income (MBM, 2023 base)",
        transformations=["Canada; Market basket measure 2023 base; percentage; selected groups from age/family + population-group tables"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out


def fetch_income_by_age():
    """Median total income by age group, Canada — the age-income (life-course)
    profile. StatCan 11-10-0239-01 (Total income; Median excluding zeros; both
    genders). Income rises steeply in early career, peaks at 45-54, then steps
    down in retirement — the structural reason an income decile is not a fixed
    group of people (the same person occupies different deciles over a lifetime)."""
    logger.info("fetch_income_by_age (11-10-0239-01)")
    try:
        d = _get_table("11-10-0239-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    AGES = {"15 to 24 years": "15–24", "25 to 34 years": "25–34", "35 to 44 years": "35–44",
            "45 to 54 years": "45–54", "55 to 64 years": "55–64", "65 years and over": "65+"}
    gcol = next(c for c in d.columns if "ender" in c)
    d = d[(d["GEO"] == "Canada") & (d[gcol] == "Total - Gender")
          & (d["Income source"] == "Total income")
          & (d["Statistics"] == "Median income (excluding zeros)")
          & (d["Age group"].isin(AGES))].copy()
    d["year"] = pd.to_numeric(d["REF_DATE"], errors="coerce")
    d["median_income"] = pd.to_numeric(d["VALUE"], errors="coerce")
    d["age_group"] = d["Age group"].map(AGES)
    out = (d[["year", "age_group", "median_income"]]
           .dropna(subset=["year", "median_income"]))
    out["year"] = out["year"].astype(int)
    out = out.sort_values(["year", "age_group"]).reset_index(drop=True)
    if out.empty:
        return None
    out_path = DATA_DIR / "income" / "statcan_income_by_age.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year", source="Statistics Canada",
        source_table="Statistics Canada 11-10-0239-01",
        frequency="annual", unit="median total income (current $)",
        transformations=["Canada; both genders; Total income; median (excl. zeros); by age group"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out


def fetch_low_income_persistence():
    """How long low income lasts — over an 8-year window, the share of tax filers
    by the number of years spent in low income. Most who experience it are there
    only briefly: low income is more often a temporary spell than a permanent
    state, the flip side of income mobility. StatCan 11-10-0025-01 (Canada, both
    sexes, variable low income measure), latest window."""
    logger.info("fetch_low_income_persistence (11-10-0025-01)")
    try:
        d = _get_table("11-10-0025-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    d = d[(d["GEO"] == "Canada") & (d["Selected characteristics"] == "Both sexes")
          & (d["Low income threshold"] == "Variable low income measure")
          & (d["Statistics"] == "Percentage of tax filers in low income")].copy()
    d["pct_of_filers"] = pd.to_numeric(d["VALUE"], errors="coerce")
    window = sorted(d["REF_DATE"].dropna().unique())[-1]
    w = d[d["REF_DATE"] == window].copy()
    w["years"] = w["Years in low income"].str.extract(r"(\d+)").astype(int)
    out = (w[["years", "pct_of_filers"]].dropna(subset=["pct_of_filers"])
           .sort_values("years").reset_index(drop=True))
    out["window"] = window
    if out.empty:
        return None
    out_path = DATA_DIR / "income" / "statcan_low_income_persistence.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="window", source="Statistics Canada",
        source_table="Statistics Canada 11-10-0025-01",
        frequency="annual", unit="% of tax filers (8-year window)",
        transformations=[f"Canada; both sexes; variable LIM; years in low income over the {window} window"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out


def fetch_wages_by_province():
    """Average weekly wage by province over time (all industries, both genders,
    age 15+, full- and part-time employees). StatCan 14-10-0064-01 — drives the
    regional-wage comparison and the pay-vs-prices view on the cost-of-living page."""
    logger.info("fetch_wages_by_province (14-10-0064-01)")
    try:
        d = _get_table("14-10-0064-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    naics = next(c for c in d.columns if "NAICS" in c)
    # Keep BOTH the average and the median weekly wage. The average is pulled up by
    # top earners and by composition shifts (e.g. the 2020 COVID layoffs of low-paid
    # workers mechanically raised the mean); the median tracks the typical worker and
    # is the honest series for the "did pay keep up with prices" comparison.
    wage_map = {"Average weekly wage rate": "avg_weekly_wage",
                "Median weekly wage rate": "median_weekly_wage"}
    d = d[(d["Wages"].isin(wage_map))
          & (d["Type of work"] == "Both full- and part-time employees")
          & (d[naics] == "Total employees, all industries")
          & (d["Gender"] == "Total - Gender")
          & (d["Age group"] == "15 years and over")].copy()
    d["year"] = pd.to_numeric(d["REF_DATE"], errors="coerce")
    d["value"] = pd.to_numeric(d["VALUE"], errors="coerce")
    d["measure"] = d["Wages"].map(wage_map)
    out = (d.dropna(subset=["year", "value"])
            .pivot_table(index=["year", "GEO"], columns="measure", values="value")
            .reset_index().rename(columns={"GEO": "geo"}))
    out.columns.name = None
    out["year"] = out["year"].astype(int)
    out = out.sort_values(["year", "geo"]).reset_index(drop=True)
    if out.empty:
        return None
    out_path = DATA_DIR / "income" / "statcan_wages_by_province.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year", source="Statistics Canada",
        source_table="Statistics Canada 14-10-0064-01",
        frequency="annual", unit="weekly wage ($)",
        transformations=["Canada + provinces; all industries; both genders; 15+; both full/part-time; average and median weekly wage"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out


# Average rent by city and bedroom type (CMHC Rental Market Survey via StatCan
# 34-10-0133-01) — the dollar-level companion to the rent CPI (a trend index) and
# the by-city vacancy spread; answers "what's typical rent in my city". The
# comprehensive "row and apartment structures of three units and over" universe;
# there is no "Total" unit, so the 2-bedroom (CMHC's reference unit) anchors the
# page and all four sizes are kept.
RENT_STRUCT = "Row and apartment structures of three units and over"
RENT_UNITS = {
    "Bachelor units": "Bachelor",
    "One bedroom units": "1 bedroom",
    "Two bedroom units": "2 bedroom",
    "Three bedroom units": "3 bedroom +",
}


def fetch_cma_rent():
    """Average monthly rent by city and bedroom type, latest survey year (CMHC
    Rental Market Survey via StatCan 34-10-0133-01). Tidy: cma, year, bedroom,
    avg_rent. CMA/centre rows only; the Ottawa-Gatineau "part" rows are dropped in
    favour of the combined whole-CMA row (same dup-geography trap as the vacancy and
    crime joins)."""
    logger.info("Fetching CMA average rent (34-10-0133-01)...")
    try:
        df = _get_table("34-10-0133-01")
    except Exception as e:
        logger.error(f"  failed to fetch rent table: {e}")
        return None
    STRUCT, UNIT = "Type of structure", "Type of unit"
    validate_columns(df, ["REF_DATE", "GEO", STRUCT, UNIT, "VALUE"], "cma_rent")
    df = df[(df[STRUCT] == RENT_STRUCT) & (df[UNIT].isin(RENT_UNITS))
            & ~df["GEO"].str.contains(" part,", na=False)].copy()
    df["year"] = pd.to_numeric(df["REF_DATE"], errors="coerce")
    df["avg_rent"] = pd.to_numeric(df["VALUE"], errors="coerce")
    df = df.dropna(subset=["year", "avg_rent"])
    if df.empty:
        return None
    latest_year = int(df["year"].max())
    df = df[df["year"] == latest_year].copy()
    df["cma"] = df["GEO"].str.split(",").str[0].str.strip()
    df["bedroom"] = df[UNIT].map(RENT_UNITS)
    out = (df[["cma", "year", "bedroom", "avg_rent"]]
           .drop_duplicates(["cma", "bedroom"])
           .sort_values(["bedroom", "avg_rent"]).reset_index(drop=True))
    out["year"] = out["year"].astype(int)
    out_path = DATA_DIR / "housing" / "statcan_cma_rent.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
        source="Statistics Canada / CMHC", source_table="34-10-0133-01",
        frequency="annual", unit="average monthly rent ($)",
        transformations=[f"row + apartment structures of three units and over; "
                         f"CMA/centre rows (Ottawa-Gatineau parts dropped); "
                         f"latest year ({latest_year}); four bedroom types"])
    logger.info(f"  saved {len(out)} rows ({out.cma.nunique()} centres, {latest_year}) -> {out_path.name}")
    return out


# Wealth, real-estate assets and mortgage debt per household by age group (StatCan
# Distributions of Household Economic Accounts, 36-10-0660-01). The DHEA are
# MODELLED, EXPERIMENTAL distributions of the national balance sheet — flagged as
# such on the page. "Value per household" in dollars, Canada, quarterly. The
# millennial-stress trio (younger households hold the least net worth and carry the
# most mortgage debt).
WEALTH_AGES = ["Less than 35 years", "35 to 44 years", "45 to 54 years",
               "55 to 64 years", "65 years and over"]
WEALTH_AGE_LABELS = {"Less than 35 years": "Under 35", "35 to 44 years": "35–44",
                     "45 to 54 years": "45–54", "55 to 64 years": "55–64",
                     "65 years and over": "65+"}
WEALTH_MEASURES = {"Net worth (wealth)": "net_worth", "Real estate": "real_estate",
                   "Mortgage liabilities": "mortgage"}


def fetch_wealth_by_age():
    """Net worth, real-estate wealth and mortgage debt per household by age group,
    Canada (StatCan DHEA 36-10-0660-01, quarterly). Wide CSV: date, age_group,
    net_worth, real_estate, mortgage. The DHEA are modelled experimental estimates
    (a distribution of the national balance sheet), labelled as such on the page."""
    logger.info("Fetching wealth by age (DHEA 36-10-0660-01)...")
    try:
        df = _get_table("36-10-0660-01")
    except Exception as e:
        logger.error(f"  failed to fetch DHEA table: {e}")
        return None
    validate_columns(df, ["REF_DATE", "GEO", "Statistics", "Characteristics",
                          "Wealth", "UOM", "VALUE"], "wealth_by_age")
    d = df[(df["GEO"] == "Canada") & (df["Statistics"] == "Value per household")
           & (df["UOM"] == "Dollars") & (df["Characteristics"].isin(WEALTH_AGES))
           & (df["Wealth"].isin(WEALTH_MEASURES))].copy()
    d["date"] = pd.to_datetime(d["REF_DATE"], format="%Y-%m", errors="coerce")
    d["value"] = pd.to_numeric(d["VALUE"], errors="coerce")
    d["age_group"] = d["Characteristics"].map(WEALTH_AGE_LABELS)
    d["measure"] = d["Wealth"].map(WEALTH_MEASURES)
    wide = (d.dropna(subset=["date", "value"])
            .pivot_table(index=["date", "age_group"], columns="measure", values="value")
            .reset_index())
    order = {lab: i for i, lab in enumerate(WEALTH_AGE_LABELS.values())}
    wide = (wide.assign(_o=wide["age_group"].map(order))
            .sort_values(["date", "_o"]).drop(columns="_o")
            .dropna(subset=["net_worth"]).reset_index(drop=True))
    if wide.empty:
        return None
    cols = ["date", "age_group"] + [c for c in ("net_worth", "real_estate", "mortgage")
                                    if c in wide.columns]
    out_path = DATA_DIR / "housing" / "statcan_wealth_by_age.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wide[cols].to_csv(out_path, index=False)
    save_metadata(out_path, df=wide, date_column="date",
        source="Statistics Canada", source_table="36-10-0660-01",
        frequency="quarterly", unit="dollars per household (modelled, DHEA)",
        transformations=["Canada; value per household; five age groups; "
                         "net worth / real estate / mortgage liabilities; "
                         "DHEA modelled experimental estimates"])
    logger.info(f"  saved {len(wide)} rows -> {out_path.name}")
    return wide[cols]


_SUICIDE_AGES = {
    "15 to 19 years": "15–19", "20 to 24 years": "20–24", "25 to 29 years": "25–29",
    "30 to 34 years": "30–34", "35 to 39 years": "35–39", "40 to 44 years": "40–44",
    "45 to 49 years": "45–49", "50 to 54 years": "50–54", "55 to 59 years": "55–59",
    "60 to 64 years": "60–64", "65 to 69 years": "65–69", "70 to 74 years": "70–74",
    "75 to 79 years": "75–79", "80 to 84 years": "80–84", "85 to 89 years": "85–89",
    "90 years and over": "90+",
}


def fetch_suicide_by_age():
    """Suicide (intentional self-harm) age-specific mortality rate per 100,000, by
    age group and sex, Canada (StatCan 13-10-0392-01). The descriptive by-age/sex
    pattern behind the headline rate: rates among men run roughly three times those
    among women and peak in midlife. Ages 15+ only (younger groups are near zero and
    largely suppressed). Tidy: year, age_group, sex, rate."""
    logger.info("fetch_suicide_by_age (13-10-0392-01)")
    try:
        d = _get_table("13-10-0392-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    CAUSE, AGE = "Cause of death (ICD-10)", "Age at time of death"
    RATE = "Age-specific mortality rate per 100,000 population"
    SUICIDE = "Intentional self-harm (suicide) [X60-X84, Y87.0]"
    ages = {f"Age at time of death, {a}": lab for a, lab in _SUICIDE_AGES.items()}
    d = d[(d[CAUSE] == SUICIDE) & (d["Characteristics"] == RATE)
          & (d["Sex"].isin(["Males", "Females"])) & (d[AGE].isin(ages))].copy()
    d["year"] = pd.to_numeric(d["REF_DATE"], errors="coerce")
    d["rate"] = pd.to_numeric(d["VALUE"], errors="coerce")
    d["age_group"] = d[AGE].map(ages)
    d["sex"] = d["Sex"].map({"Males": "Men", "Females": "Women"})
    out = (d.dropna(subset=["year", "rate"])[["year", "age_group", "sex", "rate"]]
             .sort_values(["year", "sex", "age_group"]).reset_index(drop=True))
    out["year"] = out["year"].astype(int)
    if out.empty:
        return None
    out_path = DATA_DIR / "health" / "statcan_suicide_by_age.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year", source="Statistics Canada",
        source_table="Statistics Canada 13-10-0392-01",
        frequency="annual", unit="age-specific deaths per 100,000 population",
        transformations=["Canada; intentional self-harm (suicide); age-specific rate "
                         "by age group and sex; ages 15+"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out


def fetch_piaac_skills():
    """Adult skills (PIAAC Cycle 2, collected 2022–23): average literacy,
    numeracy, and adaptive-problem-solving scores plus proficiency-level shares,
    for Canada, the provinces, and the table's built-in OECD average, by age
    group and gender. StatCan 37-10-0259-01 (released 2024-12-10).

    PIAAC runs roughly once a decade, so values are stable between cycles; the
    weekly fetch simply re-stamps freshness. The peer-country means live in
    data/education/oecd_piaac_means.csv (one-time pipeline/build_piaac.py)."""
    logger.info("fetch_piaac_skills (37-10-0259-01)")
    try:
        d = _get_table("37-10-0259-01")
    except Exception as e:
        logger.error(f"  failed: {e}")
        return None
    OECD = "Organisation for Economic Co-operation and Development (OECD)"
    stat_map = {
        "Average score": "avg_score",
        "Percentage of the population scoring at proficiency level 1 or below": "pct_level1_below",
        "Percentage of the population scoring at proficiency level 2": "pct_level2",
        "Percentage of the population scoring at proficiency level 3 or above": "pct_level3_above",
    }
    gender_map = {"Total, gender": "Total", "Men+": "Men", "Women+": "Women"}
    age_map = {
        "Total, age groups": "16–65 (all)",
        "16 to 24 years": "16–24", "25 to 34 years": "25–34",
        "35 to 44 years": "35–44", "45 to 54 years": "45–54",
        "55 to 65 years": "55–65",
    }
    d = d[d["Statistics"].isin(stat_map) & d["Gender"].isin(gender_map)
          & d["Age Group"].isin(age_map)].copy()
    if d.empty:
        return None
    d["geo"] = d["GEO"].replace({OECD: "OECD average"})
    d["age_group"] = d["Age Group"].map(age_map)
    d["gender"] = d["Gender"].map(gender_map)
    d["skill"] = d["Skill"]
    d["statistic"] = d["Statistics"].map(stat_map)
    d["year"] = d["REF_DATE"].astype(str).str[:4].astype(int)
    d["value"] = pd.to_numeric(d["VALUE"], errors="coerce")
    out = (d.pivot_table(index=["year", "geo", "age_group", "gender", "skill"],
                         columns="statistic", values="value", aggfunc="first")
             .reset_index())
    for col in ["avg_score", "pct_level1_below", "pct_level2", "pct_level3_above"]:
        if col not in out.columns:
            out[col] = float("nan")
    out = (out[["year", "geo", "age_group", "gender", "skill",
                "avg_score", "pct_level1_below", "pct_level2", "pct_level3_above"]]
           .dropna(subset=["avg_score", "pct_level1_below"], how="all")
           .sort_values(["geo", "skill", "age_group", "gender"]).reset_index(drop=True))
    if out.empty:
        return None
    out_path = DATA_DIR / "education" / "statcan_piaac_skills.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
                  source="Statistics Canada",
                  source_table="Statistics Canada 37-10-0259-01 (PIAAC Cycle 2)",
                  frequency="per assessment cycle (~decadal)",
                  unit="mean score (0–500) / % of population aged 16–65",
                  transformations=["Canada + provinces + the table's built-in OECD average; "
                                   "by gender and age group; average score plus proficiency-"
                                   "level shares (level 1 or below / level 2 / level 3 or "
                                   "above) per skill domain"])
    logger.info(f"  saved {len(out)} rows -> {out_path.name}")
    return out
