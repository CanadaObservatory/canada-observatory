"""ONE-TIME builder (not weekly): 2021 Census commuting — main mode of commuting
and average commute duration, by city — from StatCan 98-10-0457-01.

The cube is ~500 MB uncompressed, so it is chunk-read and filtered to the totals
(Total age, Total gender, Count). Census is 5-yearly, so this is a static
snapshot: re-run on the 2026 Census (bump nothing — the table id is stable).

    python -m pipeline.build_commute

Outputs (committed; the weekly pipeline does not touch them):
- data/population/commute_mode.csv          — geo, mode, share (% of commuters)
- data/population/commute_duration.csv      — geo, avg_min (all modes)
- data/population/commute_duration_by_mode.csv — geo, mode, avg_min (the mode
  selector: All modes / Car / Public transit / Walked / Bicycle). The 2021 Census
  is the only authoritative source that breaks commute time down by mode AND by
  city; the Labour Force Survey series (build_commute_lfs.py) is more current but
  national-only by mode.
"""
import zipfile
import io
import requests
import pandas as pd
from pipeline.config import DATA_DIR

URL = "https://www150.statcan.gc.ca/n1/tbl/csv/98100457-eng.zip"
TOTAL_COL = "Commuting duration (7):Total - Commuting duration[1]"
AVG_COL = "Commuting duration (7):Average commuting duration (in minutes)[7]"
# The mutually-exclusive leaf modes (they sum to the "Total" count). Motorcycle is
# folded into "Other" on the chart; here we keep the raw six and combine later.
LEAF_MODES = ["Car, truck or van", "Public transit", "Walked", "Bicycle",
              "Motorcycle, scooter or moped", "Other method"]
MODE_LABEL = {"Car, truck or van": "Car, truck or van", "Public transit": "Public transit",
              "Walked": "Walked", "Bicycle": "Bicycle",
              "Motorcycle, scooter or moped": "Other", "Other method": "Other"}
TOTAL_MODE = "Total - Main mode of commuting"
# Modes carried into the by-mode duration selector (cube label -> display label).
# These are the average-duration figures StatCan publishes for each mode; "All
# modes" leads the selector. Walked/Bicycle are kept distinct (the cube also has
# an "Active transportation" aggregate, but separate walk/cycle reads better).
DURATION_MODES = {
    "Total - Main mode of commuting": "All modes",
    "Car, truck or van": "Car",
    "Public transit": "Public transit",
    "Walked": "Walked",
    "Bicycle": "Bicycle",
}


def _clean_geo(g):
    """'Toronto (CMA), Ont.' -> 'Toronto'; 'Canada' -> 'Canada'."""
    return g.split(" (CMA)")[0].strip() if "(CMA)" in g else g.strip()


def build_commute():
    print(f"Downloading {URL} ...")
    r = requests.get(URL, timeout=180)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    csv_name = next(n for n in z.namelist() if n.endswith(".csv") and "MetaData" not in n)

    cols = ["GEO", "Time leaving for work (7)", "Age (15A)", "Gender (3)", "Statistics (3)",
            "Main mode of commuting (21)", TOTAL_COL, AVG_COL]
    chunks = []
    for ch in pd.read_csv(z.open(csv_name), usecols=cols, chunksize=200000, low_memory=False):
        chunks.append(ch[(ch["Time leaving for work (7)"] == "Total - Time leaving for work")
                         & (ch["Age (15A)"] == "Total - Age")
                         & (ch["Gender (3)"] == "Total - Gender")
                         & (ch["Statistics (3)"] == "Count")])
    df = pd.concat(chunks).rename(columns={
        "Main mode of commuting (21)": "mode", TOTAL_COL: "count", AVG_COL: "avg_min"})
    df["count"] = pd.to_numeric(df["count"], errors="coerce")
    df["avg_min"] = pd.to_numeric(df["avg_min"], errors="coerce")

    # Keep Canada + CMA-level geographies only; drop the Ottawa-Gatineau province
    # "part" rows (they clean to the same name as the combined CMA → duplicates)
    df = df[(df["GEO"] == "Canada") | (df["GEO"].str.contains(r"\(CMA\)", na=False))].copy()
    df = df[~df["GEO"].str.contains(" part", na=False)]
    df["geo"] = df["GEO"].map(_clean_geo)

    # --- mode shares ---
    totals = (df[df["mode"] == TOTAL_MODE].drop_duplicates("geo").set_index("geo")["count"])
    mrows = []
    for geo, g in df[df["mode"].isin(LEAF_MODES)].groupby("geo"):
        tot = totals.get(geo)
        if pd.isna(tot) or not tot:
            continue
        # combine the raw leaf modes into the display labels (Motorcycle -> Other)
        by_label = g.assign(label=g["mode"].map(MODE_LABEL)).groupby("label")["count"].sum()
        for label, cnt in by_label.items():
            mrows.append({"geo": geo, "mode": label, "share": round(cnt / tot * 100, 1)})
    mode_df = pd.DataFrame(mrows).sort_values(["geo", "mode"]).reset_index(drop=True)
    mode_path = DATA_DIR / "population" / "commute_mode.csv"
    mode_path.parent.mkdir(parents=True, exist_ok=True)
    mode_df.to_csv(mode_path, index=False)
    print(f"  saved {len(mode_df)} rows ({mode_df.geo.nunique()} geos) -> {mode_path.name}")

    # --- average duration ---
    dur = (df[df["mode"] == TOTAL_MODE][["geo", "avg_min"]].dropna()
           .drop_duplicates("geo").sort_values("avg_min").reset_index(drop=True))
    dur_path = DATA_DIR / "population" / "commute_duration.csv"
    dur.to_csv(dur_path, index=False)
    print(f"  saved {len(dur)} geos -> {dur_path.name}")

    # --- average duration BY MODE (drives the mode selector) ---
    dm = df[df["mode"].isin(DURATION_MODES)][["geo", "mode", "avg_min"]].dropna(subset=["avg_min"])
    dm = (dm.assign(mode=dm["mode"].map(DURATION_MODES))
            .drop_duplicates(["geo", "mode"]).sort_values(["geo", "mode"]).reset_index(drop=True))
    dmode_path = DATA_DIR / "population" / "commute_duration_by_mode.csv"
    dm.to_csv(dmode_path, index=False)
    print(f"  saved {len(dm)} rows ({dm.geo.nunique()} geos) -> {dmode_path.name}")
    return mode_df, dur


if __name__ == "__main__":
    build_commute()
