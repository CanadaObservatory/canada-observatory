"""ANNUAL manual builder (not weekly): CURRENT commute times by city, from
Statistics Canada's Labour Force Survey May commuting supplement.

The 2021 Census (build_commute.py) is the only source that breaks commute time
down BY MODE BY CITY, but it is a 5-yearly snapshot taken during the pandemic,
when commuting was unusually light. This LFS series carries the up-to-date
all-modes level (Toronto rose from ~30 minutes in 2021 toward ~35 by 2025) and
the post-pandemic recovery.

StatCan publishes this only as static tables in The Daily each August (there is
no data cube), so this is a manual annual builder: each August, bump `DAILY` to
the new release id when StatCan releases the next May supplement.

    python -m pipeline.build_commute_lfs

Output (committed; the weekly pipeline does not touch it):
- data/population/commute_duration_lfs.csv — geo, year, avg_min
  (Canada + the 15 largest CMAs, all-modes, one-way, May 2021–latest)
"""
import io
import requests
import pandas as pd
from pipeline.config import DATA_DIR

# The Daily, "Number of Canadian commuters increases…", May 2025 supplement.
# BUMP THIS each August to the next release id when StatCan publishes the new year.
DAILY = "https://www150.statcan.gc.ca/n1/daily-quotidien/250826"
CMA_TABLE = f"{DAILY}/t004a-eng.htm"   # Average commute times, 15 largest CMAs
PROV_TABLE = f"{DAILY}/t003a-eng.htm"  # Average commute times by province (carries Canada)
HDR = {"User-Agent": "Mozilla/5.0"}


def _scrape(url, keep_rows=None):
    """Parse a Daily commute-times table → long (geo, year, avg_min). The first
    column is the geography; each 'May YYYY' column is an all-modes minute value;
    the units row (geo is NaN) is dropped. `keep_rows` filters the geographies."""
    r = requests.get(url, headers=HDR, timeout=90)
    r.raise_for_status()
    t = pd.read_html(io.StringIO(r.text))[0]
    t = t.rename(columns={t.columns[0]: "geo"})
    t = t[t["geo"].notna()]
    year_cols = {c: int(str(c).split()[-1]) for c in t.columns if str(c).startswith("May ")}
    long = t.melt(id_vars="geo", value_vars=list(year_cols), var_name="col", value_name="avg_min")
    long["year"] = long["col"].map(year_cols)
    long["avg_min"] = pd.to_numeric(long["avg_min"], errors="coerce")
    long = long.dropna(subset=["avg_min"]).drop(columns="col")
    if keep_rows is not None:
        long = long[long["geo"].isin(keep_rows)]
    return long


def build_commute_lfs():
    print(f"Scraping LFS commute tables from {DAILY} ...")
    cma = _scrape(CMA_TABLE)
    canada = _scrape(PROV_TABLE, keep_rows={"Canada"})
    out = (pd.concat([canada, cma], ignore_index=True)
             .sort_values(["geo", "year"])[["geo", "year", "avg_min"]].reset_index(drop=True))
    path = DATA_DIR / "population" / "commute_duration_lfs.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(path, index=False)
    print(f"  saved {len(out)} rows ({out.geo.nunique()} geos, "
          f"{int(out.year.min())}–{int(out.year.max())}) -> {path.name}")
    return out


if __name__ == "__main__":
    build_commute_lfs()
