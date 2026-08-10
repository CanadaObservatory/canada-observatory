"""
Substance use in Canada, by age group — Statistics Canada 13-10-0972-01
("Health characteristics, two-year period estimates"; CCHS self-report).

ANNUAL MANUAL BUILDER — NOT in the weekly registry. The source CSV is ~512 MB
uncompressed and the table is released only every ~2 years, so (like fetch_crime
and build_wait_times) it is refreshed by hand each spring rather than weekly:

    python -m pipeline.build_substance_use

Emits data/health/statcan_substance_use_by_age.csv — Canada, both sexes, the
self-reported PERCENT prevalence of cannabis use (past 12 months), current smoking
(daily or occasional) and heavy drinking, by age group (Total 18+, 18-34, 35-49,
50-64, 65+) across five two-year periods (2015/2016 → 2023/2024). Cannabis use falls
steeply with age; smoking peaks middle-aged; heavy drinking declines with age. The
table is 18+ only (no 12-17 youth bracket).
"""

import io
import logging
import zipfile

import pandas as pd
import requests

from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata

logger = logging.getLogger(__name__)

URL = "https://www150.statcan.gc.ca/n1/tbl/csv/13100972-eng.zip"

# Indicator member string -> output column (the three substances we chart)
INDICATORS = {
    "Cannabis use, past 12 months": "cannabis_pct",
    "Current smoker, daily or occasional": "smoking_pct",
    "Heavy drinking": "drinking_pct",
}
AGE_ORDER = ["Total, 18 years and over", "18 to 34 years", "35 to 49 years",
             "50 to 64 years", "65 years and over"]


def build_substance_use_by_age():
    logger.info("StatCan 13-10-0972 — substance use by age")
    r = requests.get(URL, timeout=300)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    csv_name = next(n for n in zf.namelist()
                    if n.endswith(".csv") and "MetaData" not in n)

    usecols = ["REF_DATE", "GEO", "Age group", "Sex",
               "Indicators", "Characteristics", "VALUE"]
    keep = []
    with zf.open(csv_name) as fh:
        for ch in pd.read_csv(fh, usecols=usecols, dtype=str, chunksize=200_000):
            m = ((ch["GEO"] == "Canada") & (ch["Sex"] == "Both sexes")
                 & (ch["Characteristics"] == "Percent")
                 & (ch["Indicators"].isin(INDICATORS)))
            if m.any():
                keep.append(ch[m])
    df = pd.concat(keep, ignore_index=True)
    df["value"] = pd.to_numeric(df["VALUE"], errors="coerce")
    df["substance"] = df["Indicators"].map(INDICATORS)

    wide = (df.pivot_table(index=["REF_DATE", "Age group"], columns="substance",
                           values="value", aggfunc="first").reset_index()
            .rename(columns={"REF_DATE": "period", "Age group": "age_group"}))
    wide["period_end"] = wide["period"].str.split("/").str[1].astype(int)
    wide["age_group"] = pd.Categorical(wide["age_group"], AGE_ORDER, ordered=True)
    wide = wide.sort_values(["age_group", "period"]).reset_index(drop=True)
    cols = ["period", "period_end", "age_group",
            "cannabis_pct", "smoking_pct", "drinking_pct"]
    wide = wide[[c for c in cols if c in wide.columns]]

    out = DATA_DIR / "health" / "statcan_substance_use_by_age.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    wide.to_csv(out, index=False)
    save_metadata(out, df=wide, date_column="period_end",
                  source="Statistics Canada",
                  source_table="Table 13-10-0972-01 (CCHS, two-year period estimates)",
                  frequency="biennial", unit="% of population (self-reported)",
                  transformations=["Canada, both sexes, Percent; cannabis (past 12 mo) / "
                                   "current smoker (daily or occasional) / heavy drinking, by age group"])
    logger.info(f"  wrote {len(wide)} rows -> {out.name}")
    return wide


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    build_substance_use_by_age()
