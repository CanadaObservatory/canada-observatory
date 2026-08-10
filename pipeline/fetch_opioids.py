"""
Opioid- & stimulant-related harms — Public Health Agency of Canada (PHAC).

One small, tidy quarterly CSV (the federal "Substance-Related Harms" dataset,
Open Government Licence – Canada) drives the whole Substance Use page. From it we
derive four focused outputs:

  phac_opioid_national.csv     national apparent opioid-toxicity deaths + opioid-
                               poisoning hospitalizations, by year (number + rate)
  phac_opioid_provincial.csv   latest full-year opioid death rate by province /
                               territory (PRUID-keyed → joins prov_2021.geojson)
  phac_opioid_demographics.csv latest full-year deaths by age group × sex
  phac_opioid_supply.csv       the "toxic supply" story over time — % of deaths
                               involving fentanyl, % also involving a stimulant,
                               % accidental (vs suicide)

Source long format (one row per region × period × measure × breakdown):
  Substance, Source, Specific_Measure, Region, PRUID, Time_Period, Year_Quarter,
  Aggregator, Disaggregator, Unit, Value   (Value may be 'Suppr.' or 'n/a')

The feed is updated quarterly; the most recent quarters are preliminary. Light
enough (~2.5 MB) to ride the weekly pipeline like any other registry indicator.
"""

import io
import logging

import pandas as pd
import requests

from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata

logger = logging.getLogger(__name__)

SRHD_URL = "https://health-infobase.canada.ca/src/doc/SRHD/SubstanceHarmsData.csv"

# Province / territory PRUID -> name (the 13 standard geographies that join
# prov_2021.geojson). Excludes Canada (1), the combined "Territories" (63), and
# the sub-provincial regions (Winnipeg, Whitehorse, …) the feed also carries.
PT_NAMES = {
    10: "Newfoundland and Labrador", 11: "Prince Edward Island", 12: "Nova Scotia",
    13: "New Brunswick", 24: "Quebec", 35: "Ontario", 46: "Manitoba",
    47: "Saskatchewan", 48: "Alberta", 59: "British Columbia",
    60: "Yukon", 61: "Northwest Territories", 62: "Nunavut",
}


def _parse_year(yq):
    """Year from Year_Quarter ('2016' or '2025 (Jan to Sep)'); None for quarters."""
    s = str(yq).strip()
    if "Q" in s:                       # a quarterly row, not an annual one
        return None
    return int(s[:4]) if s[:4].isdigit() else None


def _is_partial(yq):
    return "(" in str(yq)              # '2025 (Jan to Sep)' = an incomplete year


def _annual(df, **flt):
    """Annual rows matching the filters → DataFrame[year, partial, value]."""
    m = pd.Series(True, index=df.index)
    for col, val in flt.items():
        m &= df[col] == val
    m &= df["Time_Period"] == "By year"
    sub = df[m].copy()
    sub["year"] = sub["Year_Quarter"].map(_parse_year)
    sub["partial"] = sub["Year_Quarter"].map(_is_partial)
    sub = sub.dropna(subset=["year"])
    sub["year"] = sub["year"].astype(int)
    sub["value"] = pd.to_numeric(sub["Value"], errors="coerce")
    return sub[["year", "partial", "value"]].reset_index(drop=True)


def fetch_opioid_harms():
    logger.info("PHAC opioid- & stimulant-related harms")
    try:
        r = requests.get(SRHD_URL, timeout=120)
        r.raise_for_status()
        raw = pd.read_csv(io.StringIO(r.text))
    except Exception as e:
        logger.error(f"  failed to fetch SubstanceHarmsData.csv: {e}")
        return None

    op = raw[raw["Substance"] == "Opioids"]
    out_dir = DATA_DIR / "health"
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- 1. National time series: deaths (number + rate) + hospitalizations rate
    deaths_n = _annual(op, Source="Deaths", Specific_Measure="Overall numbers",
                       Region="Canada", Unit="Number").rename(columns={"value": "deaths_number"})
    deaths_r = _annual(op, Source="Deaths", Specific_Measure="Overall numbers",
                       Region="Canada", Unit="Crude rate").rename(columns={"value": "deaths_rate"})
    hosp_r = _annual(op, Source="Hospitalizations", Specific_Measure="Overall numbers",
                     Region="Canada", Unit="Crude rate").rename(columns={"value": "hosp_rate"})
    national = (deaths_n.merge(deaths_r, on=["year", "partial"], how="outer")
                .merge(hosp_r, on=["year", "partial"], how="outer")
                .sort_values("year").reset_index(drop=True))
    national_path = out_dir / "phac_opioid_national.csv"
    national.to_csv(national_path, index=False)
    save_metadata(national_path, df=national, date_column="year",
                  source="Public Health Agency of Canada",
                  source_table="Substance-Related Harms (apparent opioid toxicity deaths & hospitalizations)",
                  frequency="quarterly", unit="number / per 100,000",
                  transformations=["Filtered Substance=Opioids, national, annual; deaths number & crude rate + hospitalization crude rate"])

    # latest complete calendar year — the snapshot year for the maps/bars below
    full = national[~national["partial"]]
    latest_year = int(full["year"].max())

    # ---- 2. Provincial death rate (latest full year) → PRUID-keyed map
    prov = _annual_by_region(op, latest_year)
    prov_path = out_dir / "phac_opioid_provincial.csv"
    prov.to_csv(prov_path, index=False)
    save_metadata(prov_path, df=prov, date_column="year",
                  source="Public Health Agency of Canada",
                  source_table="Substance-Related Harms (opioid toxicity deaths by province/territory)",
                  frequency="annual", unit="per 100,000",
                  transformations=[f"Apparent opioid toxicity death crude rate by PT, {latest_year}"])

    # ---- 3. Who is affected — deaths by age group × sex (latest full year)
    demo = op[(op["Source"] == "Deaths") & (op["Specific_Measure"] == "Sex and age group")
              & (op["Region"] == "Canada") & (op["Time_Period"] == "By year")
              & (op["Unit"] == "Number") & (op["Year_Quarter"].astype(str) == str(latest_year))].copy()
    demo = pd.DataFrame({
        "year": latest_year,
        "age_group": demo["Aggregator"].values,
        "sex": demo["Disaggregator"].values,
        "deaths_number": pd.to_numeric(demo["Value"], errors="coerce").values,
    }).dropna(subset=["deaths_number"])
    demo_path = out_dir / "phac_opioid_demographics.csv"
    demo.to_csv(demo_path, index=False)
    save_metadata(demo_path, df=demo, date_column="year",
                  source="Public Health Agency of Canada",
                  source_table="Substance-Related Harms (opioid toxicity deaths by age & sex)",
                  frequency="annual", unit="number of deaths",
                  transformations=[f"Apparent opioid toxicity deaths by age group × sex, Canada, {latest_year}"])

    # ---- 4. The toxic supply over time: % fentanyl, % stimulant-involved, % accidental
    fent = _annual(op, Source="Deaths", Specific_Measure="Type of opioids",
                   Region="Canada", Unit="Percent", Disaggregator="Fentanyl").rename(columns={"value": "fentanyl_pct"})
    stim = _annual(op, Source="Deaths", Specific_Measure="Involving stimulants",
                   Region="Canada", Unit="Percent").rename(columns={"value": "stimulant_pct"})
    acc = _annual(op, Source="Deaths", Specific_Measure="Manner of death",
                  Region="Canada", Unit="Percent", Disaggregator="Accidental").rename(columns={"value": "accidental_pct"})
    supply = (fent.merge(stim, on=["year", "partial"], how="outer")
              .merge(acc, on=["year", "partial"], how="outer")
              .sort_values("year").reset_index(drop=True))
    supply_path = out_dir / "phac_opioid_supply.csv"
    supply.to_csv(supply_path, index=False)
    save_metadata(supply_path, df=supply, date_column="year",
                  source="Public Health Agency of Canada",
                  source_table="Substance-Related Harms (opioid death circumstances)",
                  frequency="annual", unit="% of deaths",
                  transformations=["% of apparent opioid toxicity deaths involving fentanyl / a stimulant / ruled accidental, Canada"])

    logger.info(f"  wrote national ({len(national)}), provincial ({len(prov)}), "
                f"demographics ({len(demo)}), supply ({len(supply)}) — latest full year {latest_year}")
    return national


def _annual_by_region(op, year):
    """Latest-full-year opioid death rate + count by province/territory (PRUID)."""
    base = op[(op["Source"] == "Deaths") & (op["Specific_Measure"] == "Overall numbers")
              & (op["Time_Period"] == "By year") & (op["Year_Quarter"].astype(str) == str(year))]
    rate = base[base["Unit"] == "Crude rate"].set_index("PRUID")["Value"]
    num = base[base["Unit"] == "Number"].set_index("PRUID")["Value"]
    rows = []
    for pruid, name in PT_NAMES.items():
        rows.append({
            "pruid": pruid, "region": name, "year": year,
            "deaths_rate": pd.to_numeric(rate.get(pruid), errors="coerce"),
            "deaths_number": pd.to_numeric(num.get(pruid), errors="coerce"),
        })
    return pd.DataFrame(rows)
