"""
One-time builder for the census-tract choropleth's STATIC assets.

The census is a 5-yearly snapshot (2021; next 2026), so unlike the weekly
indicators these are generated once and committed, NOT run in the weekly pipeline.
Re-run this when a new census lands.

Outputs (committed):
  data/geo/ct_2021.geojson             simplified CT boundaries, WGS84, feature id = CTUID
  data/geo/statcan_ct_income_2021.csv  ctuid, median_income  (2021 Census, 2020 income)

Run:  python -m pipeline.build_census_geo
"""

import io
import os
import json
import zipfile
import requests
import pandas as pd
import geopandas as gpd

GEO_DIR = "data/geo"
BND_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
           "boundary-limites/files-fichiers/lct_000a21a_e.zip")
SIMPLIFY_TOL = 0.0003      # CT ~33 m; topology-preserving (see _toposimplify). Inlined into
                           # several pages, so kept near the old file size — the sliver fix comes
                           # from the topology, not the tolerance; DAs get the finer tolerance below.
COORD_DECIMALS = 4         # ~11 m precision for CT/CMA (finer than the CT tolerance above)
DA_SIMPLIFY_TOL = 0.0001   # DA ~11 m — dissemination areas are single blocks; keep edges on the street
DA_COORD_DECIMALS = 5      # ~1 m precision for DAs (must stay finer than the DA tolerance)


def _toposimplify(gdf, tolerance):
    """Topology-preserving simplification. Shapely's per-feature `.simplify()`
    simplifies each polygon independently, so a border shared by two areas is
    simplified differently on each side and the two no longer coincide — that is
    what leaves the thin white slivers/gaps between tracts and DAs. topojson
    instead builds the shared boundaries as arcs and simplifies each arc ONCE, so
    neighbours stay welded together. Self-intersections the simplification can
    introduce are repaired with buffer(0). Returns a GeoDataFrame in the same CRS.

    Requires the `topojson` package — an extra dependency of this one-time builder
    (like geopandas, it is intentionally NOT in requirements.txt / the weekly CI;
    install it when re-running the census build: `pip install topojson`)."""
    import topojson as tp
    out = tp.Topology(gdf, prequantize=False).toposimplify(tolerance).to_gdf()
    out = out.set_crs(gdf.crs, allow_override=True)
    invalid = ~out.geometry.is_valid
    if invalid.any():
        out.loc[invalid, "geometry"] = out.loc[invalid, "geometry"].buffer(0)
    return out


def build_income():
    """Median household total income (2020) per census tract, from 98-10-0058."""
    from pipeline.fetch_statcan import _get_table
    df = _get_table("98-10-0058-01")
    incol = [c for c in df.columns if "Median household total income (2020)" in c][0]
    hs = [c for c in df.columns if c.startswith("Household size")][0]
    ht = [c for c in df.columns if c.startswith("Household type")][0]
    tot = df[df[hs].str.startswith("Total", na=False)
             & df[ht].str.startswith("Total", na=False)].copy()
    tot = tot[tot["DGUID"].astype(str).str.contains("S0507")]      # CT-level rows
    tot["ctuid"] = tot["DGUID"].astype(str).str.replace("2021S0507", "", regex=False)
    tot["name"] = tot["GEO"].astype(str)              # e.g. "0001.00 - Abbotsford - Mission"
    tot["median_income"] = pd.to_numeric(tot[incol], errors="coerce")
    out = (tot[["ctuid", "name", "median_income"]]
           .dropna(subset=["median_income"]).drop_duplicates("ctuid"))
    os.makedirs(GEO_DIR, exist_ok=True)
    out.to_csv(f"{GEO_DIR}/statcan_ct_income_2021.csv", index=False)
    print(f"income: {len(out)} census tracts")
    return set(out["ctuid"])


def build_boundaries(income_ctuids):
    print("downloading CT boundary file...")
    z = zipfile.ZipFile(io.BytesIO(requests.get(BND_URL, timeout=300).content))
    z.extractall("/tmp/ct_bnd")
    shp = [f for f in z.namelist() if f.endswith(".shp")][0]
    gdf = gpd.read_file(f"/tmp/ct_bnd/{shp}")
    ctcol = [c for c in gdf.columns if c.upper() == "CTUID"][0]
    gdf = gdf.to_crs(epsg=4326)
    gdf["ctuid"] = gdf[ctcol].astype(str)
    gdf = _toposimplify(gdf[["ctuid", "geometry"]], SIMPLIFY_TOL)
    bnd_ids = set(gdf["ctuid"])
    print(f"boundaries: {len(bnd_ids)} CTs | matched to income: {len(bnd_ids & income_ctuids)}")

    gj = json.loads(gdf.to_json())

    def rnd(o):
        if isinstance(o, float):
            return round(o, COORD_DECIMALS)
        if isinstance(o, list):
            return [rnd(x) for x in o]
        return o

    for feat in gj["features"]:
        feat["id"] = feat["properties"]["ctuid"]
        feat["geometry"]["coordinates"] = rnd(feat["geometry"]["coordinates"])
    path = f"{GEO_DIR}/ct_2021.geojson"
    with open(path, "w") as f:
        f.write(json.dumps(gj, separators=(",", ":")))
    print(f"wrote {path}: {round(os.path.getsize(path)/1e6, 2)} MB")


CMA_BND_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
               "boundary-limites/files-fichiers/lcma000a21a_e.zip")
COMP_CMA_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/dp-pd/prof/details/"
                "download-telecharger/comp/GetFile.cfm?Lang=E&FILETYPE=CSV&GEONO=002")


def build_cma():
    """City-level (CMA/CA) choropleth assets: unemployment, median dwelling value,
    value-to-income (dwelling value / household income — an affordability proxy),
    and Crime Severity Index. Crime is only published at this geography, so the
    whole batch is built at CMA for consistency. Dwelling value is owner-estimated
    (2021 Census), not sale prices."""
    from pipeline.fetch_statcan import _get_table

    # --- census indicators from the comprehensive CMA profile (GEONO=002) ---
    z = zipfile.ZipFile(io.BytesIO(requests.get(COMP_CMA_URL, timeout=300).content))
    csvn = [n for n in z.namelist() if n.endswith("_data.csv")][0]
    with z.open(csvn) as f:
        prof = pd.read_csv(f, encoding="latin-1", dtype=str,
                           usecols=["ALT_GEO_CODE", "GEO_NAME", "CHARACTERISTIC_NAME", "C1_COUNT_TOTAL"])
    prof["CHARACTERISTIC_NAME"] = prof["CHARACTERISTIC_NAME"].str.strip()
    prof["v"] = pd.to_numeric(prof["C1_COUNT_TOTAL"], errors="coerce")

    def ser(pred):
        s = prof[prof["CHARACTERISTIC_NAME"].apply(pred)].drop_duplicates("ALT_GEO_CODE")
        return s.set_index("ALT_GEO_CODE")["v"]

    une = ser(lambda c: c == "Unemployment rate")
    dv = ser(lambda c: c == "Median value of dwellings ($)")
    inc = ser(lambda c: "edian total income of household" in c.lower() and "2020" in c)
    names = prof.drop_duplicates("ALT_GEO_CODE").set_index("ALT_GEO_CODE")["GEO_NAME"]
    ind = pd.DataFrame({"name": names, "unemployment": une, "dwelling_value": dv, "median_income": inc})
    ind["value_to_income"] = (ind["dwelling_value"] / ind["median_income"]).round(2)
    ind.index.name = "cmauid"
    print(f"CMA indicators: {len(ind)} | unemp {ind.unemployment.notna().sum()} | "
          f"dwelling_val {ind.dwelling_value.notna().sum()} | income {ind.median_income.notna().sum()}")

    # --- Crime Severity Index by CMA (latest year) ---
    crime = _get_table("35-10-0026-01")
    csi = crime[crime["Statistics"] == "Crime severity index"].copy()
    yr = csi["REF_DATE"].max()
    csi = csi[csi["REF_DATE"] == yr]
    csi["cmauid"] = csi["GEO"].astype(str).str.extract(r"(\d{5})")[0].str[-3:]  # prov(2)+cma(3) -> cma
    csi["csi"] = pd.to_numeric(csi["VALUE"], errors="coerce")
    # split CMAs (Ottawa-Gatineau etc.) appear as province "parts" + a combined row;
    # prefer the combined whole-CMA figure over a single part
    csi["_ispart"] = csi["GEO"].str.contains("part", case=False, na=False).astype(int)
    csi = csi.dropna(subset=["cmauid", "csi"]).sort_values("_ispart")
    csi_s = csi.drop_duplicates("cmauid", keep="first").set_index("cmauid")["csi"]
    ind["crime_severity"] = csi_s
    print(f"crime: matched {ind.crime_severity.notna().sum()} CMAs (year {yr})")

    os.makedirs(GEO_DIR, exist_ok=True)
    ind.reset_index().to_csv(f"{GEO_DIR}/statcan_cma_indicators.csv", index=False)

    # --- CMA boundaries ---
    bz = zipfile.ZipFile(io.BytesIO(requests.get(CMA_BND_URL, timeout=300).content))
    bz.extractall("/tmp/cma_bnd")
    shp = [f for f in bz.namelist() if f.endswith(".shp")][0]
    gdf = gpd.read_file(f"/tmp/cma_bnd/{shp}")
    uid = [c for c in gdf.columns if c.upper() == "CMAUID"][0]
    gdf = gdf.to_crs(epsg=4326)
    gdf["cmauid"] = gdf[uid].astype(str)
    # merge cross-province CMAs (Ottawa-Gatineau, Lloydminster, …) that the file
    # splits into same-CMAUID province parts — otherwise duplicate feature ids
    # leave half the CMA uncoloured.
    gdf = gdf.dissolve(by="cmauid", as_index=False)[["cmauid", "geometry"]]
    gdf["geometry"] = gdf["geometry"].simplify(0.004, preserve_topology=True)
    print(f"CMA boundaries: {len(gdf)} | match indicators: {len(set(gdf.cmauid) & set(ind.index))}")
    gj = json.loads(gdf.to_json())

    def rnd(o):
        if isinstance(o, float):
            return round(o, COORD_DECIMALS)
        if isinstance(o, list):
            return [rnd(x) for x in o]
        return o

    for ft in gj["features"]:
        ft["id"] = ft["properties"]["cmauid"]
        ft["geometry"]["coordinates"] = rnd(ft["geometry"]["coordinates"])
    path = f"{GEO_DIR}/cma_2021.geojson"
    with open(path, "w") as f:
        f.write(json.dumps(gj, separators=(",", ":")))
    print(f"wrote {path}: {round(os.path.getsize(path)/1e6, 2)} MB")


# Age structure (100% data) in the 2021 CT profile: 8 = total population; 35–38 are
# StatCan's OWN pre-computed broad-group % distribution (0–14 / 15–64 / 65+ / 85+ —
# use these, not the count rows 9/13/24/29, to avoid share math on randomly-rounded
# counts); 40 = median age. IDs verified against the profile metadata 2026-06.
AGE_CT_CHARS = {8: "population", 35: "share_0_14", 36: "share_15_64",
                37: "share_65_plus", 38: "share_85_plus", 40: "median_age"}

# Language spoken most often at home (100% data; single responses): the top home
# languages nationally. Base 735 = total population excluding institutional
# residents; multiple responses mean the listed shares don't tile to 100% — the
# remainder is published as "Other & multiple responses" rather than hidden.
LANGUAGE_BASE = 735
LANGUAGE_GROUPS = [
    (738, "english", "English"),
    (739, "french", "French"),
    (1028, "mandarin", "Mandarin"),
    (963, "punjabi", "Punjabi"),
    (1032, "cantonese", "Cantonese (Yue)"),
    (985, "spanish", "Spanish"),
    (849, "arabic", "Arabic"),
    (879, "tagalog", "Tagalog (Filipino)"),
    (982, "italian", "Italian"),
    (861, "vietnamese", "Vietnamese"),
    (983, "portuguese", "Portuguese"),
    (967, "urdu", "Urdu"),
]

# Citizenship / nativity (25% sample data, private households). The three
# top-level groups are DERIVED the same way as White/Indigenous on the diversity
# layer: Canadian-born = "Non-immigrants" (StatCan: "Canadian citizens by birth"),
# naturalized = Canadian citizens − non-immigrants (clamped ≥0 for small-area
# rounding), non-citizens direct (1526). NPR (1537) overlaps non-citizens and is
# kept as an extra dropdown option, excluded from the hover composition.
CITIZENSHIP_CHARS = {1522: "base", 1523: "citizens", 1526: "non_citizens",
                     1527: "imm_base", 1528: "non_immigrants", 1537: "npr"}

# Visible-minority groups: (census CHARACTERISTIC_ID in the 2021 profile, column, label).
# Descriptive only — no evaluative direction. "Visible minority" is StatCan's term.
# 1683 is the population base (denominator); 1684 = all VM. The 10 subgroups follow.
VM_GROUPS = [
    (1684, "all_vm", "All visible minorities"),
    (1685, "south_asian", "South Asian"),
    (1686, "chinese", "Chinese"),
    (1687, "black", "Black"),
    (1688, "filipino", "Filipino"),
    (1689, "arab", "Arab"),
    (1690, "latin_american", "Latin American"),
    (1691, "southeast_asian", "Southeast Asian"),
    (1692, "west_asian", "West Asian"),
    (1693, "korean", "Korean"),
    (1694, "japanese", "Japanese"),
]

# Derived "population group" columns appended to every diversity layer so the maps
# carry explicit White and Indigenous categories. StatCan's visible-minority variable
# has no distinct White group and folds Indigenous people into a single "Not a visible
# minority" residual (1697). We split that residual using the single-response
# Indigenous-identity variable (1403), both on the SAME population base — 1683
# (visible minority) ≡ 1402 (Indigenous), verified identical — so the three top-level
# groups tile to exactly 100%:
#     indigenous = Indigenous identity (1403)                       / base
#     white      = (Not a visible minority 1697 − Indigenous 1403)  / base
# White is therefore a DERIVED residual: the non-Indigenous, non-visible-minority
# population (what the Employment Equity framework treats as the Caucasian/white
# group). It is NOT the ethnic-origin "Caucasian (White)" write-in (id 1715 ≈ 1% —
# a multiple-response field that cannot be summed to a clean share). Output columns,
# in dropdown order, are charts.DIVERSITY_MAP_GROUPS.
NOT_VM_ID = 1697          # "Not a visible minority" = White + Indigenous (residual)
INDIGENOUS_ID = 1403      # "Indigenous identity" (single response)
DERIVED_GROUPS = [("white", "White"), ("indigenous", "Indigenous")]


def _derived_population_groups(get, denom):
    """White + Indigenous % shares from a per-geography count accessor `get(cid)`
    (Series indexed like `denom`) and the visible-minority population base `denom`.
    White is the non-Indigenous remainder of "Not a visible minority", clamped at 0
    for the rare small-area case where StatCan's random rounding makes it slightly
    negative. Returns (white_pct, indigenous_pct)."""
    indig = get(INDIGENOUS_ID)
    white = get(NOT_VM_ID) - indig
    return ((white / denom * 100).round(1).clip(lower=0),
            (indig / denom * 100).round(1))

# Religion: top-level 2021-Census groups (CHARACTERISTIC_ID, column, label). 1949 is
# the population base (denominator); the Christian sub-denominations 1952–1966 are
# rolled into the 1951 "Christian" total to keep the dropdown to the major groups.
# Self-reported, descriptive only — handled like the visible-minority layer (neutral,
# no scorecard, no "good/bad"). "No religion and secular perspectives" is the second
# largest group and is included.
RELIGION_BASE = 1949
RELIGION_GROUPS = [
    (1951, "christian", "Christian"),
    (1973, "no_religion", "No religion / secular"),
    (1969, "muslim", "Muslim"),
    (1967, "hindu", "Hindu"),
    (1970, "sikh", "Sikh"),
    (1950, "buddhist", "Buddhist"),
    (1968, "jewish", "Jewish"),
    (1971, "indigenous_spirituality", "Indigenous"),
    (1972, "other_religion", "Other religions & spiritual traditions"),
]


def build_cma_ethnicity():
    """Per-city share of each visible-minority group (2021 Census), for a
    dropdown choropleth. % = group count / population on the visible-minority
    base. Strictly descriptive (where people live); no scorecard, neutral colours."""
    z = zipfile.ZipFile(io.BytesIO(requests.get(COMP_CMA_URL, timeout=300).content))
    csvn = [n for n in z.namelist() if n.endswith("_data.csv")][0]
    with z.open(csvn) as f:
        df = pd.read_csv(f, encoding="latin-1", dtype=str,
                         usecols=["ALT_GEO_CODE", "GEO_NAME", "CHARACTERISTIC_ID",
                                  "CHARACTERISTIC_NAME", "C1_COUNT_TOTAL"])
    df["CHARACTERISTIC_ID"] = pd.to_numeric(df["CHARACTERISTIC_ID"], errors="coerce")
    df["v"] = pd.to_numeric(df["C1_COUNT_TOTAL"], errors="coerce")
    names = df.drop_duplicates("ALT_GEO_CODE").set_index("ALT_GEO_CODE")["GEO_NAME"]
    denom = (df[df["CHARACTERISTIC_NAME"].str.strip().str.startswith(
                 "Total - Visible minority for the population", na=False)]
             .drop_duplicates("ALT_GEO_CODE").set_index("ALT_GEO_CODE")["v"])

    def by_id(cid):
        return (df[df["CHARACTERISTIC_ID"] == cid].drop_duplicates("ALT_GEO_CODE")
                .set_index("ALT_GEO_CODE")["v"])

    out = pd.DataFrame({"name": names})
    for cid, col, _ in VM_GROUPS:
        out[col] = (by_id(cid) / denom * 100).round(1)
    out["white"], out["indigenous"] = _derived_population_groups(by_id, denom)
    out["population"] = denom.round().astype("Int64")   # area head-count (the share base)
    out.index.name = "cmauid"
    out = out.dropna(subset=["all_vm"])
    os.makedirs(GEO_DIR, exist_ok=True)
    out.reset_index().to_csv(f"{GEO_DIR}/statcan_cma_ethnicity.csv", index=False)
    print(f"ethnicity: {len(out)} CMAs | sample Toronto all_vm={out.loc[out['name']=='Toronto','all_vm'].values}")


def build_cma_religion():
    """Per-city share of each major religion (2021 Census), for a dropdown choropleth.
    % = group count / 1949 population base. Same descriptive, neutral treatment as the
    visible-minority layer (self-reported; no scorecard, no evaluative direction)."""
    z = zipfile.ZipFile(io.BytesIO(requests.get(COMP_CMA_URL, timeout=300).content))
    csvn = [n for n in z.namelist() if n.endswith("_data.csv")][0]
    with z.open(csvn) as f:
        df = pd.read_csv(f, encoding="latin-1", dtype=str,
                         usecols=["ALT_GEO_CODE", "GEO_NAME", "CHARACTERISTIC_ID", "C1_COUNT_TOTAL"])
    df["CHARACTERISTIC_ID"] = pd.to_numeric(df["CHARACTERISTIC_ID"], errors="coerce")
    df["v"] = pd.to_numeric(df["C1_COUNT_TOTAL"], errors="coerce")
    names = df.drop_duplicates("ALT_GEO_CODE").set_index("ALT_GEO_CODE")["GEO_NAME"]

    def by_id(cid):
        return (df[df["CHARACTERISTIC_ID"] == cid].drop_duplicates("ALT_GEO_CODE")
                .set_index("ALT_GEO_CODE")["v"])

    denom = by_id(RELIGION_BASE)
    out = pd.DataFrame({"name": names})
    for cid, col, _ in RELIGION_GROUPS:
        out[col] = (by_id(cid) / denom * 100).round(1)
    out["population"] = denom.round().astype("Int64")   # area head-count (the share base)
    out.index.name = "cmauid"
    out = out.dropna(subset=["christian"])
    os.makedirs(GEO_DIR, exist_ok=True)
    out.reset_index().to_csv(f"{GEO_DIR}/statcan_cma_religion.csv", index=False)
    print(f"religion: {len(out)} CMAs | sample Toronto no_religion="
          f"{out.loc[out['name']=='Toronto','no_religion'].values}")


COMP_CT_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/dp-pd/prof/details/"
               "download-telecharger/comp/GetFile.cfm?Lang=E&FILETYPE=CSV&GEONO=007")
DWELLING_VALUE_CHAR = "Median value of dwellings ($)"


def _download_cache(url, path, label, stream=True):
    """Download `url` to `path` once and reuse it (these census files are large and
    static). Returns the cached path."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  using cached {label}: {path} ({os.path.getsize(path)/1e6:.0f} MB)")
        return path
    print(f"  downloading {label} (large, one-time) ...")
    with requests.get(url, stream=stream, timeout=1800) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                f.write(chunk)
    print(f"  saved {label}: {os.path.getsize(path)/1e6:.0f} MB")
    return path


def build_ct_from_profile():
    """Census-tract dwelling value + visible-minority shares from the comprehensive
    2021 Census Profile for census tracts (98-401-X2021007, GEONO=007 — a ~238 MB
    zip, multi-GB uncompressed). One-time/static (census is 5-yearly); NOT in the
    weekly pipeline. Reuses the existing ct_2021.geojson geometry, so this only emits
    the two value CSVs, keyed by CTUID exactly like the income map.

    The big CSV is read in chunks, keeping only census-tract rows (DGUID contains
    'S0507') and only the characteristics we need, so memory stays bounded."""
    cache = "/tmp/census_profile_ct_007.zip"
    _download_cache(COMP_CT_URL, cache, "CT census profile (GEONO=007)")
    z = zipfile.ZipFile(cache)
    csvn = [n for n in z.namelist() if n.endswith("_data.csv")][0]

    needed_ids = ({cid for cid, _, _ in VM_GROUPS} | {1683, NOT_VM_ID, INDIGENOUS_ID}
                  | {cid for cid, _, _ in RELIGION_GROUPS} | {RELIGION_BASE}
                  | set(AGE_CT_CHARS)
                  | {LANGUAGE_BASE} | {cid for cid, _, _ in LANGUAGE_GROUPS}
                  | set(CITIZENSHIP_CHARS))
    cols = ["DGUID", "GEO_NAME", "CHARACTERISTIC_ID", "CHARACTERISTIC_NAME", "C1_COUNT_TOTAL"]
    keep = []
    for chunk in pd.read_csv(z.open(csvn), usecols=cols, dtype=str,
                             encoding="latin-1", chunksize=400_000):
        chunk = chunk[chunk["DGUID"].str.contains("S0507", na=False)]   # CT level only
        if chunk.empty:
            continue
        cid = pd.to_numeric(chunk["CHARACTERISTIC_ID"], errors="coerce")
        mask = cid.isin(needed_ids) | (chunk["CHARACTERISTIC_NAME"].str.strip() == DWELLING_VALUE_CHAR)
        if mask.any():
            keep.append(chunk[mask])
    df = pd.concat(keep, ignore_index=True)
    df["cid"] = pd.to_numeric(df["CHARACTERISTIC_ID"], errors="coerce")
    df["ctuid"] = df["DGUID"].astype(str).str.replace("2021S0507", "", regex=False)
    df["v"] = pd.to_numeric(df["C1_COUNT_TOTAL"], errors="coerce")
    names = df.drop_duplicates("ctuid").set_index("ctuid")["GEO_NAME"].astype(str)
    os.makedirs(GEO_DIR, exist_ok=True)

    def ct_series(cid):
        return df[df["cid"] == cid].drop_duplicates("ctuid").set_index("ctuid")["v"]

    # --- dwelling value (owner-estimated) ---
    dv = (df[df["CHARACTERISTIC_NAME"].str.strip() == DWELLING_VALUE_CHAR]
          .drop_duplicates("ctuid").set_index("ctuid")["v"])
    dval = pd.DataFrame({"name": names, "dwelling_value": dv}).dropna(subset=["dwelling_value"])
    dval.index.name = "ctuid"
    dval.reset_index().to_csv(f"{GEO_DIR}/statcan_ct_dwelling_value_2021.csv", index=False)
    print(f"CT dwelling value: {len(dval)} tracts")

    # --- visible-minority shares (% of population base 1683) ---
    base = ct_series(1683)
    eth = pd.DataFrame({"name": names})
    for cid, col, _ in VM_GROUPS:
        eth[col] = (ct_series(cid) / base * 100).round(1)
    eth["white"], eth["indigenous"] = _derived_population_groups(ct_series, base)
    eth["population"] = base.round().astype("Int64")    # area head-count (the share base)
    eth.index.name = "ctuid"
    eth = eth.dropna(subset=["all_vm"])
    eth.reset_index().to_csv(f"{GEO_DIR}/statcan_ct_ethnicity_2021.csv", index=False)
    print(f"CT ethnicity: {len(eth)} tracts")

    # --- religion shares (% of population base 1949) ---
    rbase = ct_series(RELIGION_BASE)
    rel = pd.DataFrame({"name": names})
    for cid, col, _ in RELIGION_GROUPS:
        rel[col] = (ct_series(cid) / rbase * 100).round(1)
    rel["population"] = rbase.round().astype("Int64")   # area head-count (the share base)
    rel.index.name = "ctuid"
    rel = rel.dropna(subset=["christian"])
    rel.reset_index().to_csv(f"{GEO_DIR}/statcan_ct_religion_2021.csv", index=False)
    print(f"CT religion: {len(rel)} tracts")

    # --- age structure (median age + StatCan's own broad-group % distribution) ---
    age = pd.DataFrame({"name": names})
    for cid, col in AGE_CT_CHARS.items():
        age[col] = ct_series(cid)
    age["population"] = age["population"].round().astype("Int64")
    age.index.name = "ctuid"
    age = age.dropna(subset=["median_age"])
    age.reset_index().to_csv(f"{GEO_DIR}/statcan_ct_age_2021.csv", index=False)
    print(f"CT age: {len(age)} tracts")

    # --- language spoken most often at home (% of population base 735) ---
    lbase = ct_series(LANGUAGE_BASE)
    lang = pd.DataFrame({"name": names})
    for cid, col, _ in LANGUAGE_GROUPS:
        lang[col] = (ct_series(cid) / lbase * 100).round(1)
    listed = lang[[c for _, c, _ in LANGUAGE_GROUPS]].sum(axis=1)
    lang["other"] = (100 - listed).clip(lower=0).round(1)   # other + multiple responses
    lang["population"] = lbase.round().astype("Int64")
    lang.index.name = "ctuid"
    lang = lang.dropna(subset=["english"])
    lang.reset_index().to_csv(f"{GEO_DIR}/statcan_ct_language_2021.csv", index=False)
    print(f"CT language: {len(lang)} tracts")

    # --- citizenship / nativity (% of private-household base 1522; 25% sample) ---
    cz = {col: ct_series(cid) for cid, col in CITIZENSHIP_CHARS.items()}
    czbase = cz["base"]
    cit = pd.DataFrame({"name": names})
    cit["canadian_born"] = (cz["non_immigrants"] / czbase * 100).round(1)
    cit["naturalized"] = ((cz["citizens"] - cz["non_immigrants"]).clip(lower=0)
                          / czbase * 100).round(1)
    cit["non_citizens"] = (cz["non_citizens"] / czbase * 100).round(1)
    cit["npr"] = (cz["npr"] / czbase * 100).round(1)
    cit["population"] = czbase.round().astype("Int64")
    cit.index.name = "ctuid"
    cit = cit.dropna(subset=["canadian_born"])
    cit.reset_index().to_csv(f"{GEO_DIR}/statcan_ct_citizenship_2021.csv", index=False)
    print(f"CT citizenship: {len(cit)} tracts")


# ---- Historical visible-minority composition (by census year) -------------------
# Source: 98-10-0429-01, the one current cube with a Census-year dimension
# (2006/2011/2016/2021) crossed with Visible minority (15) and geography. Its
# universe is the population aged 15+ (no 0-14 age group), so shares here are of the
# 15+ population — labelled as such, and a touch different from the all-ages maps.
# 2011 was the voluntary National Household Survey (lower response, quality caveats):
# kept but flagged on the chart rather than dropped. 2001 is a best-effort extension
# from a legacy product (see _vm_history_2001) and is skipped if it can't be parsed.
VM_HISTORY_TABLE = "98-10-0429-01"
VM_BASE_MEMBER = "Total - Visible minority"
VM_PLOT_MEMBERS = [          # cube member name -> chart label (order = legend order)
    ("Total visible minority population", "All visible minorities"),
    ("Not a visible minority", "Not a visible minority"),
    ("South Asian", "South Asian"), ("Chinese", "Chinese"), ("Black", "Black"),
    ("Filipino", "Filipino"), ("Arab", "Arab"), ("Latin American", "Latin American"),
]
PROV_TERR = ["Newfoundland and Labrador", "Prince Edward Island", "Nova Scotia",
             "New Brunswick", "Quebec", "Ontario", "Manitoba", "Saskatchewan",
             "Alberta", "British Columbia", "Yukon", "Northwest Territories", "Nunavut"]
HISTORY_CMAS = ["Toronto", "Montr", "Vancouver", "Calgary", "Edmonton",
                "Ottawa", "Winnipeg", "Halifax"]


def _vm_geo_match(geo):
    """Map a cube GEO label to (level, clean_label) for our target tiers, else None."""
    g = str(geo)
    if g == "Canada":
        return ("national", "Canada")
    if g in PROV_TERR:
        return ("province", g)
    if "(CMA)" in g:
        for city in HISTORY_CMAS:
            if g.startswith(city):
                return ("cma", g.split(" (CMA)")[0])
    return None


def _detect(cols, *needles):
    for c in cols:
        cl = c.lower()
        if all(n in cl for n in needles):
            return c
    return None


def build_vm_history():
    """Visible-minority composition by census year x geography -> a tidy long CSV
    (geography, geo_level, year, group, count, share) for the historical trend chart.
    Robust core = 2006/2011/2016/2021 from one cube; 2001 added best-effort."""
    parts = VM_HISTORY_TABLE.split("-")
    url = f"https://www150.statcan.gc.ca/n1/tbl/csv/{''.join(parts[:3])}-eng.zip"
    cache = "/tmp/statcan_98100429.zip"
    try:
        _download_cache(url, cache, f"StatCan {VM_HISTORY_TABLE}")
        z = zipfile.ZipFile(cache)
        member = [n for n in z.namelist()
                  if n.endswith(".csv") and "metadata" not in n.lower()][0]
    except Exception as e:
        print(f"  vm_history: cube fetch failed ({e}) — skipping")
        return

    import re
    wanted_members = {VM_BASE_MEMBER} | {m for m, _ in VM_PLOT_MEMBERS}
    rows, colmap, year_cols = [], {}, []
    for chunk in pd.read_csv(z.open(member), dtype=str, low_memory=False, chunksize=200_000):
        if not colmap:
            cols = list(chunk.columns)
            colmap = dict(
                gen=_detect(cols, "generation"), age=_detect(cols, "age"),
                gender=_detect(cols, "gender"), cert=_detect(cols, "certificate"),
                stat=_detect(cols, "statistic"), vm=_detect(cols, "visible minor"))
            # This cube is WIDE in census year: one value column per year, named
            # like "Census year (4):2016[2]" — there is no single VALUE column.
            year_cols = [(c, int(re.search(r":(\d{4})", c).group(1)))
                         for c in cols
                         if c.startswith("Census year") and re.search(r":(\d{4})", c)]
        c = chunk
        for k in ("gen", "age", "gender", "cert"):
            if colmap.get(k):
                c = c[c[colmap[k]].str.startswith("Total", na=False)]
        if colmap.get("stat"):
            c = c[c[colmap["stat"]].str.strip().str.lower() == "count"]
        c = c[c[colmap["vm"]].str.strip().isin(wanted_members)]
        if c.empty:
            continue
        for _, r in c.iterrows():
            m = _vm_geo_match(r["GEO"])
            if not m:
                continue
            mem = r[colmap["vm"]].strip()
            for col, yr in year_cols:
                rows.append((m[1], m[0], yr, mem, r[col]))

    long = pd.DataFrame(rows, columns=["geography", "geo_level", "year", "member", "value"])
    long["year"] = pd.to_numeric(long["year"], errors="coerce").astype("Int64")
    long["value"] = pd.to_numeric(long["value"], errors="coerce")
    long = long.dropna(subset=["year", "value"])

    # best-effort 2001 extension (legacy product); never breaks the build
    try:
        extra = _vm_history_2001()
        if extra is not None and not extra.empty:
            long = pd.concat([long, extra], ignore_index=True)
            print(f"  vm_history: added 2001 ({len(extra)} rows)")
    except Exception as e:
        print(f"  vm_history: 2001 extension skipped ({e})")

    # base (15+ population) per geography x year, then shares
    base = (long[long["member"] == VM_BASE_MEMBER]
            .set_index(["geography", "year"])["value"])
    label = dict(VM_PLOT_MEMBERS)
    plot = long[long["member"].isin(label)].copy()
    plot["base"] = plot.set_index(["geography", "year"]).index.map(base)
    plot["share"] = (plot["value"] / plot["base"] * 100).round(2)
    plot["group"] = plot["member"].map(label)
    out = (plot[["geography", "geo_level", "year", "group", "value", "share"]]
           .rename(columns={"value": "count"})
           .dropna(subset=["share"]).sort_values(["geo_level", "geography", "group", "year"]))
    os.makedirs(GEO_DIR, exist_ok=True)
    out.to_csv(f"{GEO_DIR}/statcan_vm_history.csv", index=False)
    yrs = sorted(out["year"].unique().tolist())
    print(f"vm_history: {len(out)} rows | years {yrs} | geos {out['geography'].nunique()}")


def _vm_history_2001():
    """Best-effort 2001 visible-minority counts (Canada/provinces/CMAs) to extend the
    series back. Legacy census products are XML/IVT-only and format-fragile, so this
    is intentionally isolated: it returns a tidy frame matching build_vm_history's
    schema on success, or None to be skipped. Left as a documented hook for now —
    the robust 2006-2021 series ships regardless."""
    return None


# ---- Historical religion composition (decennial: 2011 + 2021) -------------------
# No religion-by-census-year cube exists, so stitch the two clean censuses on a
# total-population basis: 2021 from the wide cube 98-10-0353 (religion in columns),
# 2011 from the NHS Profile CSVs (Canada/prov = FMT CSV101, CMA = CSV201). Both keyed
# on the population base. 2011 was the voluntary NHS (flagged). 2001 is XML-only and
# omitted (same fragility we defer for VM 2001). Top-level groups only (Christian
# sub-denominations roll into the Christian total).
REL_GROUP_LABELS = {           # output key -> display label (also the chart order)
    "christian": "Christian", "no_religion": "No religion / secular",
    "muslim": "Muslim", "hindu": "Hindu", "sikh": "Sikh", "buddhist": "Buddhist",
    "jewish": "Jewish", "indigenous_spirituality": "Indigenous",
    "other_religion": "Other religions",
}
REL_MAP_2021 = {               # 2021 cube religion column-name -> key
    "Buddhist": "buddhist", "Christian": "christian", "Hindu": "hindu",
    "Jewish": "jewish", "Muslim": "muslim", "Sikh": "sikh",
    "Traditional (North American Indigenous) spirituality": "indigenous_spirituality",
    "Other religions and spiritual traditions": "other_religion",
    "No religion and secular perspectives": "no_religion",
}
REL_MAP_2011 = {               # 2011 NHS Characteristic (stripped) -> key
    "Buddhist": "buddhist", "Christian": "christian", "Hindu": "hindu",
    "Jewish": "jewish", "Muslim": "muslim", "Sikh": "sikh",
    "Traditional (Aboriginal) Spirituality": "indigenous_spirituality",
    "Other religions": "other_religion", "No religious affiliation": "no_religion",
}
REL_2011_BASE = "Total population in private households by religion"
CITY_LABELS = [("Toronto", "Toronto"), ("Montr", "Montréal"), ("Vancouver", "Vancouver"),
               ("Calgary", "Calgary"), ("Edmonton", "Edmonton"), ("Ottawa", "Ottawa–Gatineau"),
               ("Winnipeg", "Winnipeg"), ("Halifax", "Halifax")]
NHS2011_CANPROV = ("https://www12.statcan.gc.ca/nhs-enm/2011/dp-pd/prof/details/download-telecharger/"
                   "comprehensive/comp_download.cfm?LANG=E&CTLG=99-004-XWE2011001&FMT=CSV101")
NHS2011_CMA = ("https://www12.statcan.gc.ca/nhs-enm/2011/dp-pd/prof/details/download-telecharger/"
               "comprehensive/comp_download.cfm?LANG=E&CTLG=99-004-XWE2011001&FMT=CSV201")


def _rel_city_label(name):
    for pref, label in CITY_LABELS:
        if str(name).startswith(pref):
            return label
    return None


def _rel_geo(geo, is_cma_source=False):
    """Map a raw geography label to (level, canonical_label) or None."""
    g = str(geo)
    if g == "Canada":
        return ("national", "Canada")
    if g in PROV_TERR:
        return ("province", g)
    if (is_cma_source or "(CMA)" in g) and "part" not in g.lower():
        lbl = _rel_city_label(g)
        if lbl:
            return ("cma", lbl)
    return None


# --- 2001 religion by geography ---------------------------------------------
# 2001 Census topic-based tabulation 97F0022XCB2001001 ("Religion (95) and Sex (3)
# … for Canada, Provinces, Territories, CMAs and CAs"), as the Generic SDMX product
# on the Open Government download. Lets the by-place chart reach back to 2001 (it was
# previously 2011 + 2021 only). The 95-category religion variable's top-level subtotals
# map cleanly to our nine groups; children are excluded to avoid double-counting.
REL_2001_URL = "https://www12.statcan.gc.ca/open-gc-ouvert/2001/97F0022XCB2001001.ZIP"
REL_2001_GROUPS = {                # ReligWI subtotal code(s) -> our group key
    "christian": {"2", "7", "58", "68"},   # Catholic + Protestant + Orthodox + Christian n.i.e.
    "no_religion": {"90"}, "muslim": {"69"}, "hindu": {"72"}, "sikh": {"73"},
    "buddhist": {"71"}, "jewish": {"70"}, "indigenous_spirituality": {"81"},  # Aboriginal spirituality
    "other_religion": {"74", "82", "83", "84", "85", "86", "87", "88", "89"},
}


def _religion_2001_rows():
    """2001 religion counts for Canada / 13 provinces-territories / the 8 big CMAs,
    parsed from the 2001 Census SDMX product. Returns (geography, geo_level, 2001,
    group_key, count) tuples in the same shape as the 2011/2021 rows so the share
    logic in build_religion_history handles them. The whole Ottawa–Hull CMA (code
    35505) is used, not its province parts; "Yukon Territory" is aliased to "Yukon".
    Verified: 2001 Canada reproduces the published shares (Christian 77%, No religion
    16%, etc.) and the nine groups sum to ~100%."""
    from lxml import etree
    SNS = {"s": "http://www.SDMX.org/resources/SDMXML/schemas/v2_0/structure"}
    G = "http://www.SDMX.org/resources/SDMXML/schemas/v2_0/generic"
    z = zipfile.ZipFile(_download_cache(REL_2001_URL, "/tmp/rel2001.zip",
                                        "2001 Census religion (SDMX)"))
    struct = etree.fromstring(z.read("Structure_97F0022XCB2001001.xml"))
    cl = struct.find(".//s:CodeList[@id='CL_GEO']", SNS)
    code2name = {c.get("value"): c.findtext("s:Description", namespaces=SNS)
                 for c in cl.findall("s:Code", SNS)}
    target = {}                                  # GEO code -> (geo_level, label)
    for code, nm in code2name.items():
        if not nm:
            continue
        en = nm.split(" - ")[0].strip().replace("Yukon Territory", "Yukon")
        if en == "Canada":
            target[code] = ("national", "Canada")
        elif en in PROV_TERR and len(code) <= 2:
            target[code] = ("province", en)
        elif len(code) == 5 and _rel_city_label(en):
            target.setdefault(code, ("cma", _rel_city_label(en)))
    acc = {c: {} for c in target}                # GEO code -> {ReligWI code: count}
    for _, el in etree.iterparse(z.open("Generic_97F0022XCB2001001.xml"),
                                 tag="{%s}Series" % G):
        kv = {v.get("concept"): v.get("value") for v in el.iter("{%s}Value" % G)}
        if kv.get("GEO") in acc and kv.get("Sex") == "1":
            ov = el.find(".//{%s}ObsValue" % G)
            if ov is not None and ov.get("value"):
                acc[kv["GEO"]][kv["ReligWI"]] = float(ov.get("value"))
        el.clear()
    rows = []
    for code, (level, label) in target.items():
        v = acc[code]
        if "1" not in v:                         # "1" = Total - Religion (denominator)
            continue
        rows.append((label, level, 2001, "_base_", v["1"]))
        for key, codes in REL_2001_GROUPS.items():
            rows.append((label, level, 2001, key, sum(v.get(c, 0) for c in codes)))
    print(f"  religion_history 2001: {len(target)} geographies parsed")
    return rows


def build_religion_history():
    rows = []   # (geography, geo_level, year, key, count, base)

    # --- 2021: wide cube 98-10-0353 (religion in columns; total population) ---
    try:
        url21 = "https://www150.statcan.gc.ca/n1/tbl/csv/98100353-eng.zip"
        _download_cache(url21, "/tmp/statcan_98100353.zip", "StatCan 98-10-0353 (2021 religion)")
        z = zipfile.ZipFile("/tmp/statcan_98100353.zip")
        member = [n for n in z.namelist() if n.endswith(".csv") and "metadata" not in n.lower()][0]
        for chunk in pd.read_csv(z.open(member), dtype=str, low_memory=False, chunksize=100_000):
            cols = list(chunk.columns)
            agec, genc, stc = _detect(cols, "age"), _detect(cols, "gender"), _detect(cols, "statistic")
            relcols = {col: col.split(":", 1)[1].rsplit("[", 1)[0].strip()
                       for col in cols if col.startswith("Religion")}
            basecol = next((c for c, nm in relcols.items() if nm.startswith("Total - Religion")), None)
            c = chunk[chunk[agec].str.startswith("Total", na=False)
                      & chunk[genc].str.startswith("Total", na=False)
                      & chunk[stc].str.strip().str.lower().str.startswith("2021 count")]
            for _, r in c.iterrows():
                m = _rel_geo(r["GEO"])
                if not m:
                    continue
                rows.append((m[1], m[0], 2021, "_base_", r[basecol]))
                for col, nm in relcols.items():
                    key = REL_MAP_2021.get(nm)
                    if key:
                        rows.append((m[1], m[0], 2021, key, r[col]))
    except Exception as e:
        print(f"  religion_history 2021 failed: {e}")

    # --- 2011: NHS Profile CSVs (Canada/prov + CMA), total population ---
    def parse_2011(url, cache, label, is_cma):
        _download_cache(url, cache, label)
        z = zipfile.ZipFile(cache)
        member = [n for n in z.namelist() if n.lower().endswith(".csv") and "dq" not in n.lower()][0]
        df = pd.read_csv(z.open(member), dtype=str, encoding="latin-1", skiprows=1, low_memory=False)
        # CSV101 (Canada/prov) name col = Prov_Name; CSV201 (CMA/CA) = CMA_CA_Name
        namecol = "CMA_CA_Name" if "CMA_CA_Name" in df.columns else df.columns[1]
        if "Geo_Type" in df.columns:                  # the CMA file also lists CAs — keep CMAs
            df = df[df["Geo_Type"] == "CMA"]
        df = df[df["Topic"] == "Religion"].copy()
        df["char"] = df["Characteristic"].str.strip()
        df["Total"] = pd.to_numeric(df["Total"], errors="coerce")
        for geo, sub in df.groupby(namecol):
            m = _rel_geo(geo, is_cma_source=is_cma)
            if not m:
                continue
            # emit base + group counts as rows; split-province CMA parts are summed later
            for _, br in sub[sub["char"] == REL_2011_BASE].iterrows():
                rows.append((m[1], m[0], 2011, "_base_", br["Total"]))
            for _, r in sub.iterrows():
                key = REL_MAP_2011.get(r["char"])
                if key:
                    rows.append((m[1], m[0], 2011, key, r["Total"]))

    for url, cache, label, is_cma in [
            (NHS2011_CANPROV, "/tmp/nhs2011_canprov.zip", "2011 NHS Canada/prov", False),
            (NHS2011_CMA, "/tmp/nhs2011_cma.zip", "2011 NHS CMA", True)]:
        try:
            parse_2011(url, cache, label, is_cma)
        except Exception as e:
            print(f"  religion_history {label} failed: {e}")

    # --- 2001: SDMX topic-based tabulation (Canada / provinces / 8 big CMAs) ---
    try:
        rows.extend(_religion_2001_rows())
    except Exception as e:
        print(f"  religion_history 2001 failed: {e}")

    long = pd.DataFrame(rows, columns=["geography", "geo_level", "year", "group_key", "count"])
    long["count"] = pd.to_numeric(long["count"], errors="coerce")
    long = long.dropna(subset=["count"])
    # sum across any split-province parts / duplicate rows, then divide by the base
    agg = long.groupby(["geography", "geo_level", "year", "group_key"],
                       as_index=False)["count"].sum()
    base = agg[agg["group_key"] == "_base_"].set_index(["geography", "year"])["count"]
    g = agg[agg["group_key"] != "_base_"].copy()
    g["base"] = [base.get((geo, yr)) for geo, yr in zip(g["geography"], g["year"])]
    g = g.dropna(subset=["base"])
    g["share"] = (g["count"] / g["base"] * 100).round(2)
    g["group"] = g["group_key"].map(REL_GROUP_LABELS)
    out = (g[["geography", "geo_level", "year", "group", "count", "share"]]
           .dropna(subset=["group"])
           .sort_values(["geo_level", "geography", "group", "year"]))
    os.makedirs(GEO_DIR, exist_ok=True)
    out.to_csv(f"{GEO_DIR}/statcan_religion_history.csv", index=False)
    yrs = sorted(out["year"].unique().tolist())
    print(f"religion_history: {len(out)} rows | years {yrs} | geos {out['geography'].nunique()}")


# ---- Canada long-run religion (1871–2021) -----------------------------------
# Extends the 2-point (2011/2021) trend back to 1871 for CANADA ONLY, on a
# total-population basis, stitched from primary StatCan census products:
#   1871–1971  CANSIM 17-10-0073-01 "Historical statistics, principal religious
#              denominations" (decennial; the ~16 Christian denominations are
#              rolled into "Christian"; Jewish / No religion / Other map direct;
#              "denominations unknown" stays in the base, so groups sum to <100%).
#   1981       95F0303X Table 15 total-population column — the only primary source
#              covering 1981; Christian summed from its detailed denominations.
#   1991/2001  "Major religious denominations, Canada, 1991 and 2001" companion
#              table; its Christian total includes "Christian n.i.e.", matching how
#              2011/2021 roll up Christian (the farm-population product lumps n.i.e.
#              into "Other", understating Christian — so it is NOT used for these two).
#   2011/2021  reused from build_religion_history()'s Canada rows.
# Muslim/Hindu/Sikh/Buddhist were not broken out before 1981 (negligible, inside
# "Other"), so those lines begin in 1981; Christian/No-religion/Jewish/Other span
# the full series. 1981–2001 used the mandatory 20% long form; 2011 the voluntary
# NHS (flagged); 2021 mandatory. Canada-only — the existing dropdown chart keeps the
# 2011-vs-2021 provincial/city comparison.
HIST_REL_CHRISTIAN = {
    "Anglican", "Baptist", "Congregationalist", "Evangelical Church", "Greek Orthodox",
    "Jehovah's Witnesses", "Lutheran", "Mennonite", "Methodist", "Mormon", "Pentecostal",
    "Presbyterian", "Roman Catholic", "Salvation Army", "Ukrainian (Greek) Catholic",
    "United Church of Canada",
}
HIST_REL_DIRECT = {"Jewish": "Jewish", "No religion": "No religion / secular",
                   "Other religious denominations": "Other religions"}
LONGRUN_GROUPS = ["Christian", "No religion / secular", "Muslim", "Hindu", "Sikh",
                  "Buddhist", "Jewish", "Other religions"]
# 1981 from 95F0303X T15 (total-pop col); 1991/2001 from the major-denominations
# companion. Counts verified to sum to each census base; shares derived in code.
# "Other" for 1991/2001 is the residual (base − named groups) so shares ≈ 100%.
REL_GAP_YEARS = {
    1981: {"_base": 24014885, "Christian": 21440115, "No religion / secular": 1781345,
           "Jewish": 296345, "Muslim": 98130, "Hindu": 69480, "Sikh": 67655,
           "Buddhist": 51830, "Other religions": 209975},
    1991: {"_base": 26994045, "Christian": 22371735, "No religion / secular": 3333245,
           "Jewish": 318185, "Muslim": 253265, "Hindu": 157015, "Sikh": 147440,
           "Buddhist": 163415, "Other religions": 249745},
    2001: {"_base": 29639030, "Christian": 22708040, "No religion / secular": 4796325,
           "Jewish": 329995, "Muslim": 579640, "Hindu": 297200, "Sikh": 278415,
           "Buddhist": 300345, "Other religions": 349070},
}
GAP_SRC = {1981: "1981 Census (StatCan 95F0303X, Table 15)",
           1991: "1991 Census (StatCan, major religious denominations)",
           2001: "2001 Census (StatCan, major religious denominations)"}


def build_religion_canada_longrun():
    rows = []   # (year, group, count, share, source)

    # --- 1871–1971: CANSIM 17-10-0073-01 (historical denominations) ---
    try:
        url = "https://www150.statcan.gc.ca/n1/tbl/csv/17100073-eng.zip"
        _download_cache(url, "/tmp/statcan_17100073.zip", "StatCan 17-10-0073 (historical religion)")
        z = zipfile.ZipFile("/tmp/statcan_17100073.zip")
        member = [n for n in z.namelist() if n.endswith(".csv") and "metadata" not in n.lower()][0]
        h = pd.read_csv(z.open(member), dtype=str)
        h["VALUE"] = pd.to_numeric(h["VALUE"], errors="coerce")
        for yr, sub in h.groupby("REF_DATE"):
            vals = sub.dropna(subset=["VALUE"]).set_index("Religious denominations")["VALUE"]
            base = vals.get("Total religious denominations")
            if not base:
                continue
            agg = {"Christian": 0.0, "No religion / secular": 0.0, "Jewish": 0.0, "Other religions": 0.0}
            for den, v in vals.items():
                if den == "Total religious denominations":
                    continue
                if den in HIST_REL_CHRISTIAN:
                    agg["Christian"] += v
                elif den in HIST_REL_DIRECT:
                    agg[HIST_REL_DIRECT[den]] += v
                # "Religious denominations unknown" is intentionally unmapped (non-response)
            for grp, cnt in agg.items():
                rows.append((int(yr), grp, int(cnt), round(cnt / base * 100, 2),
                             "1871–1971 Censuses (StatCan Table 17-10-0073-01)"))
    except Exception as e:
        print(f"  religion_longrun 1871–1971 failed: {e}")

    # --- 1981 / 1991 / 2001: hand-curated from primary StatCan census products ---
    for year, d in REL_GAP_YEARS.items():
        base = d["_base"]
        for grp, cnt in d.items():
            if grp == "_base":
                continue
            rows.append((year, grp, cnt, round(cnt / base * 100, 2), GAP_SRC[year]))

    # --- 2011 / 2021: reuse the Canada rows from build_religion_history() ---
    hist_path = f"{GEO_DIR}/statcan_religion_history.csv"
    if os.path.exists(hist_path):
        rh = pd.read_csv(hist_path)
        ca = rh[(rh["geography"] == "Canada") & (rh["year"].isin([2011, 2021]))
                & (rh["group"].isin(LONGRUN_GROUPS))]
        src = {2011: "2011 National Household Survey (voluntary)", 2021: "2021 Census"}
        for _, r in ca.iterrows():
            rows.append((int(r["year"]), r["group"], int(r["count"]),
                         round(float(r["share"]), 2), src[int(r["year"])]))
    else:
        print("  religion_longrun: statcan_religion_history.csv missing — run build_religion_history first")

    out = pd.DataFrame(rows, columns=["year", "group", "count", "share", "source"])
    out["geography"] = "Canada"
    out["geo_level"] = "national"
    out = out[["geography", "geo_level", "year", "group", "count", "share", "source"]] \
        .sort_values(["group", "year"])
    os.makedirs(GEO_DIR, exist_ok=True)
    out.to_csv(f"{GEO_DIR}/statcan_religion_canada_longrun.csv", index=False)
    yrs = sorted(out["year"].unique().tolist())
    chr_now = out[(out["group"] == "Christian") & (out["year"] == yrs[-1])]["share"].iloc[0]
    print(f"religion_longrun: {len(out)} rows | Canada {yrs[0]}–{yrs[-1]} "
          f"({len(yrs)} census points) | Christian {yrs[0]}→{yrs[-1]} ends {chr_now}%")


def build_ct_income_geojson():
    """Combined census-tract income GeoJSON (geometry + income baked into
    feature properties + id=CTUID), served as a static file and fetched
    client-side when the user opts into neighbourhood detail on the income map."""
    gj = json.load(open(f"{GEO_DIR}/ct_2021.geojson"))
    inc = pd.read_csv(f"{GEO_DIR}/statcan_ct_income_2021.csv", dtype={"ctuid": str})
    vmap = inc.set_index("ctuid")["median_income"].to_dict()
    nmap = inc.set_index("ctuid")["name"].to_dict()
    feats = []
    for f in gj["features"]:
        cid = f.get("id") or f.get("properties", {}).get("ctuid")
        if cid in vmap:
            f["id"] = cid
            f["properties"] = {"v": vmap[cid], "name": nmap.get(cid)}
            feats.append(f)
    gj["features"] = feats
    path = f"{GEO_DIR}/ct_income_2021.geojson"
    with open(path, "w") as fh:
        fh.write(json.dumps(gj, separators=(",", ":")))
    print(f"wrote {path}: {round(os.path.getsize(path)/1e6, 2)} MB, {len(feats)} tracts")


# ---- Dissemination-area diversity (TRIAL: one metro at a time) ------------------
# DAs are StatCan's smallest standard geography (~400–700 people) — finer than CTs.
# National DA files are huge (boundary ~98 MB; DA Census Profile 2.1 GB, or ~293 MB
# for the BC provincial split), so we filter to ONE CMA. Same visible-minority IDs
# (1683–1697) as build_ct_from_profile; only the DGUID prefix differs (2021S0512 for
# DAs vs 2021S0507 for CTs). The CMA is selected spatially with the existing
# cma_2021.geojson polygon, so no extra geographic-attributes download is needed.
DA_BND_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
              "boundary-limites/files-fichiers/lda_000a21a_e.zip")
DA_PROFILE_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/dp-pd/prof/details/"
                  "download-telecharger/comp/GetFile.cfm?Lang=E&FILETYPE=CSV&GEONO={geono}")


def build_da_profile(cmauid="933", pruid="59", geono="006_BC_CB", slug="vancouver",
                     geometry_only=False):
    """Visible-minority AND religion shares + boundaries at dissemination-area level
    for ONE CMA. One pass over the (large) provincial DA Census Profile produces both
    layers (like build_ct_from_profile), filtered to the CMA via the existing
    cma_2021.geojson polygon. `pruid`/`geono` accept a list for split-province CMAs
    (Ottawa–Gatineau: pruid=["35","24"], geono=["006_Ontario","006_Quebec"]).

    `geometry_only=True` rebuilds ONLY the boundary geojson (re-simplified), reusing
    the existing per-CMA ethnicity/religion CSVs to choose which DAs to keep — so the
    multi-GB provincial profile is NOT re-downloaded or re-parsed. Use it to refresh
    the map geometry (e.g. a finer simplify tolerance) without re-fetching the data."""
    pruids = [pruid] if isinstance(pruid, str) else list(pruid)
    geonos = [geono] if isinstance(geono, str) else list(geono)
    cma = gpd.read_file(f"{GEO_DIR}/cma_2021.geojson")
    cma["cmauid"] = cma["cmauid"].astype(str)
    poly = cma[cma["cmauid"] == cmauid].to_crs(epsg=4326).geometry.union_all()

    # --- DA boundaries → province pre-filter → spatial filter to the CMA ---
    bz = zipfile.ZipFile(_download_cache(DA_BND_URL, "/tmp/lda_000a21a_e.zip", "DA boundaries (~98 MB)"))
    bz.extractall("/tmp/da_bnd")
    shp = [f for f in bz.namelist() if f.endswith(".shp")][0]
    g = gpd.read_file(f"/tmp/da_bnd/{shp}")
    uid = [c for c in g.columns if c.upper() == "DAUID"][0]
    prc = [c for c in g.columns if c.upper() == "PRUID"][0]
    g["dauid"] = g[uid].astype(str)
    g = g[g[prc].astype(str).isin(pruids)].to_crs(epsg=4326)
    cmac = [c for c in g.columns if c.upper() == "CMAUID"]
    if cmac:
        g = g[g[cmac[0]].astype(str) == cmauid].copy()
    else:
        g = g[g.geometry.representative_point().within(poly)].copy()
    g = g[["dauid", "geometry"]]
    da_ids = set(g["dauid"])
    print(f"DA boundaries ({slug}): {len(da_ids)} DAs in CMA {cmauid}")

    if geometry_only:
        # reuse the existing per-CMA data CSVs to know which DAs to map (no re-parse)
        keep_ids = set()
        for layer in ("ethnicity", "religion"):
            p = f"{GEO_DIR}/statcan_da_{slug}_{layer}.csv"
            if os.path.exists(p):
                keep_ids |= set(pd.read_csv(p, dtype={"dauid": str})["dauid"])
        print(f"DA geometry-only ({slug}): reusing {len(keep_ids)} DA ids from existing CSVs")
    else:
        # --- DA profile (visible minority + religion) from the provincial split(s),
        #     chunk-read like the CT build; concatenated across provinces for split CMAs ---
        needed = ({cid for cid, _, _ in VM_GROUPS} | {1683, NOT_VM_ID, INDIGENOUS_ID}
                  | {cid for cid, _, _ in RELIGION_GROUPS} | {RELIGION_BASE})
        keep = []
        for gn in geonos:
            pz = zipfile.ZipFile(_download_cache(DA_PROFILE_URL.format(geono=gn),
                                                 f"/tmp/da_profile_{gn}.zip", f"DA profile {gn} (large)"))
            # provincial-split profile names its data file "..._CSV_data_<Province>.csv"
            csvn = [n for n in pz.namelist()
                    if n.lower().endswith(".csv") and "data" in n.lower() and "geo" not in n.lower()][0]
            for chunk in pd.read_csv(pz.open(csvn), usecols=["DGUID", "CHARACTERISTIC_ID", "C1_COUNT_TOTAL"],
                                     dtype=str, encoding="latin-1", chunksize=400_000):
                chunk = chunk[chunk["DGUID"].str.contains("S0512", na=False)]
                if chunk.empty:
                    continue
                cid = pd.to_numeric(chunk["CHARACTERISTIC_ID"], errors="coerce")
                chunk = chunk[cid.isin(needed)]
                if not chunk.empty:
                    keep.append(chunk)
        prof = pd.concat(keep, ignore_index=True)
        prof["cid"] = pd.to_numeric(prof["CHARACTERISTIC_ID"], errors="coerce")
        prof["dauid"] = prof["DGUID"].astype(str).str.replace("2021S0512", "", regex=False)
        prof["v"] = pd.to_numeric(prof["C1_COUNT_TOTAL"], errors="coerce")
        prof = prof[prof["dauid"].isin(da_ids)]

        def ser(cid):
            return prof[prof["cid"] == cid].drop_duplicates("dauid").set_index("dauid")["v"]
        base = ser(1683)
        eth = pd.DataFrame(index=sorted(da_ids))
        eth.index.name = "dauid"
        for cid, col, _ in VM_GROUPS:
            eth[col] = (ser(cid) / base * 100).round(1)
        eth["white"], eth["indigenous"] = _derived_population_groups(ser, base)
        eth["population"] = base.round().astype("Int64")    # area head-count (the share base)
        eth["name"] = "StatCan area #" + eth.index.astype(str)
        eth = eth.dropna(subset=["all_vm"])
        os.makedirs(GEO_DIR, exist_ok=True)
        eth.reset_index().to_csv(f"{GEO_DIR}/statcan_da_{slug}_ethnicity.csv", index=False)
        print(f"DA ethnicity ({slug}): {len(eth)} DAs with data")

        # --- religion shares (same DAs; religion population base 1949) ---
        rbase = ser(RELIGION_BASE)
        rel = pd.DataFrame(index=sorted(da_ids))
        rel.index.name = "dauid"
        for cid, col, _ in RELIGION_GROUPS:
            rel[col] = (ser(cid) / rbase * 100).round(1)
        rel["population"] = rbase.round().astype("Int64")   # area head-count (the share base)
        rel["name"] = "StatCan area #" + rel.index.astype(str)
        rel = rel.dropna(subset=["christian"])
        rel.reset_index().to_csv(f"{GEO_DIR}/statcan_da_{slug}_religion.csv", index=False)
        print(f"DA religion ({slug}): {len(rel)} DAs with data")
        keep_ids = set(eth.index) | set(rel.index)

    # --- geojson (DAs present in either layer), topology-simplified, id = DAUID ---
    g = _toposimplify(g[g["dauid"].isin(keep_ids)], DA_SIMPLIFY_TOL)
    gj = json.loads(g.to_json())

    def rnd(o):
        if isinstance(o, float):
            return round(o, DA_COORD_DECIMALS)
        if isinstance(o, list):
            return [rnd(x) for x in o]
        return o
    for ft in gj["features"]:
        ft["id"] = ft["properties"]["dauid"]
        ft["geometry"]["coordinates"] = rnd(ft["geometry"]["coordinates"])
    path = f"{GEO_DIR}/da_{slug}_2021.geojson"
    with open(path, "w") as f:
        f.write(json.dumps(gj, separators=(",", ":")))
    print(f"wrote {path}: {round(os.path.getsize(path) / 1e6, 2)} MB")


# Canonical per-metro DA build calls (visible minority + religion). Heavy + one-time
# (re-run on a new census), NOT in the weekly pipeline. GEONO=006 is split into 6
# *regional* files, not one per province — single-province names (006_Alberta) 404; AB
# rides in 006_Prairies, BC is 006_BC_CB (Colombie-Britannique). build_da_profile then
# filters each regional file to the target CMA. Ordered so Ontario/Quebec download once
# and Ottawa reuses them from the /tmp cache.
DA_METROS = [
    dict(cmauid="535", pruid="35", geono="006_Ontario", slug="toronto"),
    dict(cmauid="462", pruid="24", geono="006_Quebec", slug="montreal"),
    dict(cmauid="505", pruid=["35", "24"], geono=["006_Ontario", "006_Quebec"], slug="ottawa"),
    dict(cmauid="933", pruid="59", geono="006_BC_CB", slug="vancouver"),
    dict(cmauid="825", pruid="48", geono="006_Prairies", slug="calgary"),
]


def build_all_da():
    """Rebuild every dissemination-area metro layer (see DA_METROS). ~2 GB of
    provincial profile downloads + multi-GB chunked parses — run deliberately, not
    weekly."""
    for j in DA_METROS:
        print(f"\n===== building DA: {j['slug']} =====", flush=True)
        build_da_profile(**j)


if __name__ == "__main__":
    ids = build_income()
    build_boundaries(ids)
    build_cma()
    build_cma_ethnicity()
    build_cma_religion()
    build_ct_income_geojson()
    build_vm_history()         # historical visible-minority composition (cube)
    build_religion_history()   # religion composition 2011 + 2021 (cube + NHS profiles)
    build_religion_canada_longrun()  # Canada religion 1871–2021 (historical census + gap fill)
    build_ct_from_profile()    # CT dwelling value + diversity + religion (~238 MB profile)
