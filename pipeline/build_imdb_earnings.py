#!/usr/bin/env python3
"""ANNUAL manual builder (NOT in the weekly registry — the source table is ~15 GB).

Statistics Canada IMDB (Longitudinal Immigration Database) table **43-10-0026**,
"Income of immigrant tax-filers, by sex, pre-admission experience, knowledge of official
languages at admission, immigrant admission category, admission year and tax year, for
Canada and provinces, 2023 constant dollars."

Emits median EMPLOYMENT income by **pre-admission experience** and **years since
admission**, for Canada (both sexes, all admission categories, all languages), admission
cohorts 2013– (the active, non-terminated cohorts). Drives the "Do immigrants who worked
here first earn more?" chart on population/immigration.qmd.

Why a manual annual builder, not a weekly registry indicator:
  * the bulk CSV is ~698 MB zipped / **~15 GB uncompressed** — far too big to fetch
    weekly (same reasoning as fetch_crime / build_substance_use); and
  * the IMDB is released only **once a year** (~December), so weekly is pointless.

Re-run each year after the IMDB release:
    python -m pipeline.build_imdb_earnings
It streams the zip and CHUNK-READS the CSV (never loads the whole 15 GB), keeping only the
Canada / total-sex / total-language / total-category / median-employment-income slice.

Note: the IMDB universe here is tax-filers **aged 15+** at the tax year (not the 20–54
custom cut some StatCan articles use). "Years since admission" = tax year − admission year.
"""
import io
import logging
import zipfile

import pandas as pd
import requests

from pipeline.config import DATA_DIR

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

URL = "https://www150.statcan.gc.ca/n1/tbl/csv/43100026-eng.zip"
OUT = DATA_DIR / "population" / "imdb_earnings_by_preadmission.csv"

# coarse pre-admission categories → plain-language labels (the standing public table does
# NOT break work permits into programs like postdoctoral / IMP / TFWP — that is a custom
# research cut, not available here)
LABELS = {
    "With work and study permits": "Worked and studied in Canada",
    "With work permit(s) only": "Worked in Canada",
    "With study permit(s) only": "Studied in Canada",
    "With asylum claim": "Asylum claimant",
    "Without pre-admission experience": "No prior Canadian experience",
}
USECOLS = ["REF_DATE", "GEO", "Sex", "Pre-admission experience",
           "Knowledge of official languages at admission", "Admission year",
           "Immigrant admission category", "Statistics", "VALUE"]


def build():
    logger.info("Downloading IMDB 43-10-0026 (~698 MB zip)…")
    r = requests.get(URL, timeout=900)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    name = next(n for n in z.namelist()
                if n.endswith(".csv") and "MetaData" not in n)

    logger.info("Chunk-reading the ~15 GB CSV…")
    keep = []
    with z.open(name) as f:
        for chunk in pd.read_csv(f, usecols=USECOLS, chunksize=2_000_000,
                                 encoding="utf-8-sig", low_memory=False, dtype=str):
            m = ((chunk["GEO"] == "Canada")
                 & (chunk["Sex"] == "Total, sex")
                 & (chunk["Knowledge of official languages at admission"]
                    == "Total, knowledge of official languages")
                 & (chunk["Immigrant admission category"]
                    == "Total, immigrant admission category")
                 & (chunk["Statistics"] == "Median employment income")
                 & (chunk["Pre-admission experience"].isin(LABELS)))
            sub = chunk[m]
            if len(sub):
                keep.append(sub)

    df = pd.concat(keep, ignore_index=True)
    df["admission_year"] = df["Admission year"].str.extract(r"(\d{4})").astype(int)
    df["tax_year"] = df["REF_DATE"].astype(int)
    df["years_since"] = df["tax_year"] - df["admission_year"]
    df["median_employment_income"] = pd.to_numeric(df["VALUE"], errors="coerce")
    df["pre_admission"] = df["Pre-admission experience"].map(LABELS)

    df = df[(df["admission_year"] >= 2013) & (df["years_since"] >= 1)]
    out = (df.dropna(subset=["median_employment_income"])
             [["admission_year", "pre_admission", "years_since", "tax_year",
               "median_employment_income"]]
             .sort_values(["admission_year", "pre_admission", "years_since"]))
    out["median_employment_income"] = out["median_employment_income"].astype(int)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(OUT, index=False)
    logger.info(f"saved {len(out)} rows -> {OUT} "
                f"(cohorts {out.admission_year.min()}–{out.admission_year.max()})")


if __name__ == "__main__":
    build()
