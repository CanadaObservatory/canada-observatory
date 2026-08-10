"""One-time builder: OECD PIAAC Cycle 2 peer-country mean scores.

Source: the data annex of OECD (2024), "Do Adults Have the Skills They Need to
Thrive in a Changing World?" (Survey of Adult Skills 2023) — the chapter-2
StatLink bundle (stable URL below) whose Tables 2.1/2.2/2.3 carry average
literacy, numeracy, and adaptive-problem-solving proficiency by country.
Reproduced with attribution per OECD terms.

Assessment scores are immutable historical constants and PIAAC runs roughly
once a decade, so this is a ONE-TIME builder in the build_census_geo mould:
run it once, commit data/education/oecd_piaac_means.csv, re-run at Cycle 3
(2030s). 16 of the site's 17 peers participated (Australia did not); the
United Kingdom is represented by England only. Canada's values match StatCan
37-10-0259-01 (the registry indicator behind the provincial/age charts).

Run:  python -m pipeline.build_piaac
"""

import io
import logging

import pandas as pd
import requests

from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(message)s")

# The report's chapter-2 StatLink (redirects to the bundle on stat.link/files/).
STATLINK = "https://stat.link/qi3pwc"
ASSESSMENT_YEAR = 2023   # OECD brands the cycle "Survey of Adult Skills 2023"
                         # (fieldwork 2022-23; Canada collected 2022).

SHEETS = [("Table 2.1", "Literacy"),
          ("Table 2.2", "Numeracy"),
          ("Table 2.3", "Adaptive problem solving")]

# Annex table label -> (ISO3, site display name). Peers only, plus the OECD
# average (kept in the CSV for prose anchors; the charts exclude it).
NAME_MAP = {
    "Canada": ("CAN", "Canada"),
    "Finland": ("FIN", "Finland"),
    "Japan": ("JPN", "Japan"),
    "Sweden": ("SWE", "Sweden"),
    "Norway": ("NOR", "Norway"),
    "Netherlands": ("NLD", "Netherlands"),
    "Denmark": ("DNK", "Denmark"),
    "England (UK)": ("GBR", "United Kingdom (England)"),
    "Switzerland": ("CHE", "Switzerland"),
    "Germany": ("DEU", "Germany"),
    "New Zealand": ("NZL", "New Zealand"),
    "United States": ("USA", "United States"),
    "France": ("FRA", "France"),
    "Korea": ("KOR", "South Korea"),
    "Italy": ("ITA", "Italy"),
    "Israel": ("ISR", "Israel"),
    "OECD average": ("OECD", "OECD average"),
}


def build_piaac_means():
    logger.info(f"Downloading PIAAC annex bundle: {STATLINK}")
    r = requests.get(STATLINK, timeout=120, allow_redirects=True,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    xl = pd.ExcelFile(io.BytesIO(r.content))
    rows = []
    for sheet, skill in SHEETS:
        df = xl.parse(sheet, header=None)
        data = df[df[0].apply(lambda v: isinstance(v, (int, float)) and pd.notna(v))]
        matched = 0
        for _, rec in data.iterrows():
            name = str(rec[1]).strip().rstrip("*")
            if name in NAME_MAP:
                code, display = NAME_MAP[name]
                rows.append({"year": ASSESSMENT_YEAR, "country_code": code,
                             "country_name": display, "skill": skill,
                             "mean_score": round(float(rec[0]), 1)})
                matched += 1
        logger.info(f"  {sheet} ({skill}): {len(data)} countries in table, {matched} kept")
    out = pd.DataFrame(rows).sort_values(["skill", "mean_score"], ascending=[True, False])

    # Sanity gates: every skill must carry the 16 participating peers + the OECD
    # average, and Canada's literacy mean must sit where StatCan publishes it.
    for skill in [s for _, s in SHEETS]:
        n = (out["skill"] == skill).sum()
        assert n == 17, f"{skill}: expected 17 rows (16 peers + OECD avg), got {n}"
    can_lit = out[(out["country_code"] == "CAN") & (out["skill"] == "Literacy")]["mean_score"].iloc[0]
    assert 265 <= can_lit <= 275, f"Canada literacy {can_lit} outside sanity range"

    out_path = DATA_DIR / "education" / "oecd_piaac_means.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    save_metadata(out_path, df=out, date_column="year",
                  source="OECD",
                  source_table="OECD Survey of Adult Skills 2023 (PIAAC Cycle 2), "
                               "report data annex Tables 2.1-2.3",
                  frequency="per assessment cycle (~decadal); one-time build",
                  unit="mean proficiency score (0–500 scale), adults 16–65",
                  transformations=["16 of the 17 site peers (Australia did not participate) "
                                   "+ the OECD average; United Kingdom = England only; "
                                   f"immutable scores fetched once from {STATLINK}"])
    logger.info(f"Saved {len(out)} rows -> {out_path}")
    return out


if __name__ == "__main__":
    build_piaac_means()
