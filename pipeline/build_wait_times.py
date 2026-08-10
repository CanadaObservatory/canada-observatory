"""
ONE-TIME / ANNUAL builder for the CIHI wait-times chart (NOT part of the weekly
pipeline). CIHI publishes "Wait Times for Priority Procedures in Canada" once a
year (each spring) as a downloadable XLSX, with no automated data feed — so this
is refreshed manually, like build_census_geo.py / build_geography.py.

To refresh after CIHI's annual release: bump CIHI_URL to the new edition's data
tables file and re-run `python -m pipeline.build_wait_times`.

Output: data/health/cihi_wait_times.csv (procedure, year, pct_meeting_benchmark)
— national, "% Meeting Benchmark", the April–September window (pure-year rows;
the FY / Q3Q4 COVID-era variants are dropped to keep one consistent annual
series). Open Government Licence – Canada.
"""

import io
import requests
import pandas as pd
from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Bump this each year when CIHI releases the new edition (~spring). Since the
# 2026 edition the release is titled "Wait times in Canada" (cihi.ca/en/
# wait-times-in-canada-2026) and the data tables are named by DATA RANGE
# (2008-2025), not edition year.
CIHI_URL = ("https://www.cihi.ca/sites/default/files/document/"
            "wait-times-priority-procedures-in-canada-2008-2025-data-tables-en.xlsx")

# The recognizable priority procedures with a pan-Canadian benchmark.
PROCEDURES = ["Hip Replacement", "Knee Replacement", "Cataract Surgery",
              "Hip Fracture Repair", "Radiation Therapy"]


def build_wait_times():
    logger.info("Building CIHI wait-times series (annual, manual)...")
    r = requests.get(CIHI_URL, timeout=120)
    r.raise_for_status()
    df = pd.read_excel(io.BytesIO(r.content), sheet_name="Table 1", header=1, dtype=str)
    df.columns = [str(c).strip() for c in df.columns]

    # Metric label case-insensitive: "% Meeting Benchmark" through the 2025
    # edition, "% meeting benchmark" since 2026.
    nat = df[(df["Reporting level"] == "National")
             & (df["Metric"].str.lower() == "% meeting benchmark")
             & (df["Indicator"].isin(PROCEDURES))].copy()
    # Keep the consistent April–September series (pure 4-digit years); drop the
    # FY (Apr–Mar) and Q3Q4 (Oct–Mar) COVID-era reporting variants.
    nat = nat[nat["Data year"].str.fullmatch(r"\d{4}")]
    nat["year"] = nat["Data year"].astype(int)
    nat["pct_meeting_benchmark"] = pd.to_numeric(nat["Indicator result"], errors="coerce")

    out = (nat.dropna(subset=["pct_meeting_benchmark"])
              .rename(columns={"Indicator": "procedure"})
              [["procedure", "year", "pct_meeting_benchmark"]]
              .sort_values(["procedure", "year"])
              .reset_index(drop=True))
    if out.empty:
        raise SystemExit("No CIHI wait-times rows parsed — has the file layout changed?")

    path = DATA_DIR / "health" / "cihi_wait_times.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    save_metadata(path, df=out, latest_observation_date=str(int(out["year"].max())),
        source="Canadian Institute for Health Information (CIHI)",
        source_table="CIHI Wait Times for Priority Procedures in Canada (data tables)",
        frequency="annual",
        unit="% of patients treated within the pan-Canadian benchmark",
        transformations=["national, % Meeting Benchmark, April–September window "
                         "(pure-year rows), selected priority procedures"])
    logger.info(f"  saved {len(out)} rows -> {path.name} "
                f"({out['procedure'].nunique()} procedures, {out['year'].min()}–{out['year'].max()})")
    return out


if __name__ == "__main__":
    build_wait_times()
