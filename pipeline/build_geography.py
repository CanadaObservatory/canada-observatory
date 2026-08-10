"""
One-time builder for the Geography section's STATIC map assets.

Like build_census_geo.py, these are generated once and committed — they are
physical-geography facts (boundaries, ecological zones, permafrost) or 5-yearly
census measures, NOT weekly indicators. The weekly time series for this section
(wildfire area burned, sea-ice extent) live in pipeline/fetch_geography.py and run
in the normal registry pipeline.

Outputs (committed to data/geo/):
  prov_2021.geojson                 13 province/territory boundaries (simplified, id=PRUID)
  statcan_prov_geography.csv        per-province population density + % freshwater area
  nef_ecozones.geojson              15 terrestrial ecozones (25 polygons, id=fid, props.ecozone)
  statcan_landcover_ecozone.csv     per-polygon land-cover composition (% of each class)
  permafrost_zones.geojson          4 permafrost zones (dissolved, id=fid, props.zone)
  permafrost_zones.csv              fid -> zone label (drives the categorical map)

Run:  /opt/anaconda3/bin/python -m pipeline.build_geography
      (any interpreter with geopandas + pyogrio + rasterio; not in the weekly pipeline.
       rasterio is needed only by build_elevation, which streams the MRDEM-30 DTM mosaic.)
"""

import os
import json
import zipfile
import requests
import pandas as pd
import geopandas as gpd

GEO_DIR = "data/geo"
COORD_DECIMALS = 3      # ~110 m; national-scale maps don't need finer
SIMPLIFY_TOL = 0.02     # ~2 km generalisation for whole-country polygons


def _rnd(o):
    if isinstance(o, float):
        return round(o, COORD_DECIMALS)
    if isinstance(o, list):
        return [_rnd(x) for x in o]
    return o


def _download_cache(url, path, label):
    """Download once to /tmp and reuse (these source files are large/static)."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        print(f"  using cached {label}: {path}")
        return path
    print(f"  downloading {label} ...")
    r = requests.get(url, timeout=600)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)
    return path


def _write_geojson(gj, path):
    with open(path, "w") as f:
        f.write(json.dumps(gj, separators=(",", ":")))
    print(f"  wrote {path}: {round(os.path.getsize(path) / 1e6, 2)} MB")


# ---------------------------------------------------------------------------
# A. Provinces: boundaries + population density + % freshwater area
# ---------------------------------------------------------------------------
PROV_BND_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
                "boundary-limites/files-fichiers/lpr_000a21a_e.zip")
CENSUS001_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/dp-pd/prof/details/"
                 "download-telecharger/comp/GetFile.cfm?Lang=E&FILETYPE=CSV&GEONO=001")

# Land and freshwater area by province/territory (km²) — the canonical
# NRCan / Statistics Canada figures (Canada Year Book; stable geographic facts,
# not a weekly series, so kept here with the source cited rather than fetched).
# (total_area, land_area, freshwater_area) in km².
PROV_AREA = {
    "Newfoundland and Labrador": (405212, 373872, 31340),
    "Prince Edward Island":      (5660, 5660, 0),
    "Nova Scotia":               (55284, 53338, 1946),
    "New Brunswick":             (72908, 71450, 1458),
    "Quebec":                    (1542056, 1365128, 176928),
    "Ontario":                   (1076395, 917741, 158654),
    "Manitoba":                  (647797, 553556, 94241),
    "Saskatchewan":              (651036, 591670, 59366),
    "Alberta":                   (661848, 642317, 19531),
    "British Columbia":          (944735, 925186, 19549),
    "Yukon":                     (482443, 474391, 8052),
    "Northwest Territories":     (1346106, 1183085, 163021),
    "Nunavut":                   (2093190, 1936113, 157077),
}
PROV_AREA_SRC = "Natural Resources Canada / Statistics Canada (Canada Year Book), land & freshwater area"


def build_provinces():
    """Province/territory choropleth assets: boundaries + a small indicators CSV
    (population, population density per km², and % freshwater area). Density comes
    from the 2021 Census Profile (GEONO=001, characteristic 6); % freshwater from
    the canonical NRCan/StatCan area table above."""
    z = zipfile.ZipFile(_download_cache(CENSUS001_URL, "/tmp/geo_census001.zip",
                                        "Census Profile GEONO=001"))
    csvn = [n for n in z.namelist() if n.endswith("_data.csv")][0]
    d = pd.read_csv(z.open(csvn), dtype=str, encoding="latin-1",
                    usecols=["ALT_GEO_CODE", "GEO_NAME", "CHARACTERISTIC_ID", "C1_COUNT_TOTAL"])
    d["cid"] = pd.to_numeric(d["CHARACTERISTIC_ID"], errors="coerce")
    d["v"] = pd.to_numeric(d["C1_COUNT_TOTAL"], errors="coerce")

    def by(cid):  # ALT_GEO_CODE -> value for a characteristic id
        return (d[d["cid"] == cid].drop_duplicates("ALT_GEO_CODE")
                .set_index("ALT_GEO_CODE"))

    dens, pop, land7 = by(6), by(1), by(7)
    names = dens["GEO_NAME"]
    rows = []
    for code in dens.index:
        if str(code) == "01":            # skip the Canada total — this is the province map
            continue
        name = names.loc[code]
        total, land, fresh = PROV_AREA.get(name, (None, None, None))
        popv = pop.loc[code, "v"] if code in pop.index else None
        landv = land7.loc[code, "v"] if code in land7.index else None
        # The published density (characteristic 6) rounds the territories to 0.0;
        # recompute it unrounded from population / census land area so the
        # log-scaled choropleth can still place them.
        density = round(popv / landv, 4) if (popv and landv) else dens.loc[code, "v"]
        rows.append(dict(
            pruid=str(code), name=name, population=popv, density=density,
            land_area_km2=land, total_area_km2=total, freshwater_km2=fresh,
            pct_freshwater=round(fresh / total * 100, 2) if total else None,
        ))
    out = pd.DataFrame(rows)
    os.makedirs(GEO_DIR, exist_ok=True)
    out.to_csv(f"{GEO_DIR}/statcan_prov_geography.csv", index=False)
    print(f"provinces: {len(out)} rows | density range "
          f"{out.density.min()}–{out.density.max()} | freshwater {out.pct_freshwater.min()}–{out.pct_freshwater.max()}%")

    # boundaries
    bz = zipfile.ZipFile(_download_cache(PROV_BND_URL, "/tmp/lpr_000a21a_e.zip",
                                         "province boundaries"))
    bz.extractall("/tmp/prov_bnd")
    shp = [f for f in bz.namelist() if f.endswith(".shp")][0]
    g = gpd.read_file(f"/tmp/prov_bnd/{shp}").to_crs(epsg=4326)
    uid = [c for c in g.columns if c.upper() == "PRUID"][0]
    g["pruid"] = g[uid].astype(str)
    g["geometry"] = g["geometry"].simplify(SIMPLIFY_TOL, preserve_topology=True)
    g = g[["pruid", "geometry"]]
    gj = json.loads(g.to_json())
    for ft in gj["features"]:
        ft["id"] = ft["properties"]["pruid"]
        ft["geometry"]["coordinates"] = _rnd(ft["geometry"]["coordinates"])
    _write_geojson(gj, f"{GEO_DIR}/prov_2021.geojson")


# Natural Earth 1:50m provinces/states — the standard lightweight, generalised
# (smooth-coastline, islands-as-islands) source for this kind of national choropleth.
# Public domain; 2.3 MB for the whole world vs StatCan's 133 MB cartographic file.
NE_PROV_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
               "master/geojson/ne_50m_admin_1_states_provinces.geojson")
_NE_POSTAL_TO_PRUID = {"NL": "10", "PE": "11", "NS": "12", "NB": "13", "QC": "24",
                       "ON": "35", "MB": "46", "SK": "47", "AB": "48", "BC": "59",
                       "YT": "60", "NT": "61", "NU": "62"}


def build_canada_provinces():
    """Canada province/territory boundaries for the CLEAN static maps — the classic
    Canada shape (Arctic archipelago, Newfoundland, Vancouver Island as real islands;
    bays as water). From **Natural Earth 1:50m** (public domain): pre-generalised, so
    coastlines are smooth (not feathered), and only ~0.25 MB. Keyed by PRUID so the
    StatCan/ECCC indicators join straight on. The interactive maps keep the
    generalised prov_2021.geojson (their tiled basemap hides the over-water boundary)."""
    import io
    r = requests.get(NE_PROV_URL, timeout=120)
    r.raise_for_status()
    g = gpd.read_file(io.BytesIO(r.content))
    g = g[g["iso_a2"] == "CA"].copy()
    g["pruid"] = g["postal"].map(_NE_POSTAL_TO_PRUID)
    g = g.dropna(subset=["pruid"])
    gj = json.loads(g[["pruid", "geometry"]].to_json())
    for ft in gj["features"]:
        ft["id"] = ft["properties"]["pruid"]
        ft["geometry"]["coordinates"] = _rnd(ft["geometry"]["coordinates"])
    _write_geojson(gj, f"{GEO_DIR}/canada_provinces.geojson")
    print(f"  wrote canada_provinces.geojson: {len(gj['features'])} provinces, "
          f"{round(os.path.getsize(GEO_DIR + '/canada_provinces.geojson') / 1e3)} KB")


COMP_CMA_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/dp-pd/prof/details/"
                "download-telecharger/comp/GetFile.cfm?Lang=E&FILETYPE=CSV&GEONO=002")


def build_cma_density():
    """Population density per km² by metro area (CMA/CA), 2021 Census profile
    (GEONO=002, characteristic 6). Reuses the existing cma_2021.geojson geometry,
    so this only emits a small value CSV keyed by CMAUID like the other CMA maps."""
    z = zipfile.ZipFile(_download_cache(COMP_CMA_URL, "/tmp/geo_cma002.zip",
                                        "CMA profile GEONO=002"))
    csvn = [n for n in z.namelist() if n.endswith("_data.csv")][0]
    df = pd.read_csv(z.open(csvn), dtype=str, encoding="latin-1",
                     usecols=["ALT_GEO_CODE", "GEO_NAME", "CHARACTERISTIC_ID", "C1_COUNT_TOTAL"])
    df["cid"] = pd.to_numeric(df["CHARACTERISTIC_ID"], errors="coerce")
    df["v"] = pd.to_numeric(df["C1_COUNT_TOTAL"], errors="coerce")
    names = df.drop_duplicates("ALT_GEO_CODE").set_index("ALT_GEO_CODE")["GEO_NAME"]
    dens = df[df["cid"] == 6].drop_duplicates("ALT_GEO_CODE").set_index("ALT_GEO_CODE")["v"]
    out = pd.DataFrame({"name": names, "density": dens}).dropna(subset=["density"])
    out.index.name = "cmauid"
    os.makedirs(GEO_DIR, exist_ok=True)
    out.reset_index().to_csv(f"{GEO_DIR}/statcan_cma_density.csv", index=False)
    print(f"CMA density: {len(out)} metros | density max {out.density.max()}/km²")


# ---------------------------------------------------------------------------
# B. Ecozones (categorical) + land cover by ecozone (% composition)
# ---------------------------------------------------------------------------
# AAFC National Ecological Framework for Canada — terrestrial ecozones, served as
# GeoJSON in WGS84 with server-side generalisation (maxAllowableOffset), so no
# reprojection/simplification step is needed locally. 15 zones across 25 polygons.
ECOZONE_URL = ("https://services.arcgis.com/lGOekm0RsNxYnT3j/arcgis/rest/services/"
               "National_ecological_framework_of_Canada_ecozones/FeatureServer/0/query"
               "?where=1%3D1&outFields=ECOZONE_ID,ECOZONE_NAME_EN&outSR=4326"
               "&maxAllowableOffset=0.02&f=geojson")
LANDCOVER_TABLE = "38-10-0177-01"        # StatCan: land cover by class, by ecozone (2020)

# (raw "Land cover class" value -> (slug, display label)); order = dropdown order.
# "Total area" is the denominator (excluded from the mapped classes).
LC_CLASSES = [
    ("Treed (including treed wetlands)", "treed", "Forest / treed"),
    ("Cropland", "cropland", "Cropland"),
    ("Inland water bodies", "water", "Inland water"),
    ("Wetland (non-treed)", "wetland", "Wetland (non-treed)"),
    ("Grassland and shrubland", "grass_shrub", "Grassland & shrubland"),
    ("Barren land", "barren", "Barren land"),
    ("Sparsely vegetated land", "sparse_veg", "Sparsely vegetated"),
    ("Permanent snow and ice", "snow_ice", "Snow & ice"),
    ("Built-up and artificial surfaces", "built_up", "Built-up"),
    ("Treed area disturbance", "treed_disturb", "Recent treed disturbance"),
]


def build_ecozones():
    """Ecozone boundaries (categorical map) + a per-polygon land-cover composition
    CSV (% of each land-cover class within the polygon's ecozone) that drives the
    land-cover dropdown choropleth. Each polygon gets a unique feature id (`fid`)
    so non-contiguous zones don't collide on a shared id."""
    gj = requests.get(ECOZONE_URL, timeout=180).json()
    feats = [f for f in gj.get("features", []) if f.get("geometry")]
    gj["features"] = feats
    rows = []
    for i, ft in enumerate(feats):
        fid = str(i)
        p = ft.get("properties", {})
        rows.append(dict(fid=fid, ecozone_id=p.get("ECOZONE_ID"),
                         ecozone=p.get("ECOZONE_NAME_EN")))
        ft["id"] = fid
        ft["properties"] = {"ecozone": p.get("ECOZONE_NAME_EN")}
        ft["geometry"]["coordinates"] = _rnd(ft["geometry"]["coordinates"])
    os.makedirs(GEO_DIR, exist_ok=True)
    _write_geojson(gj, f"{GEO_DIR}/nef_ecozones.geojson")
    feat_df = pd.DataFrame(rows)
    print(f"ecozones: {len(feat_df)} polygons across {feat_df.ecozone.nunique()} zones")

    # land cover by ecozone (38-10-0177-01) -> % of each class within the ecozone
    from pipeline.fetch_statcan import _get_table
    lc = _get_table(LANDCOVER_TABLE)
    lc = lc[lc["GEO"].str.endswith("(ecozone)", na=False)].copy()
    lc["ecozone"] = lc["GEO"].str.replace(r"\s*\(ecozone\)$", "", regex=True)
    lc["VALUE"] = pd.to_numeric(lc["VALUE"], errors="coerce")
    piv = lc.pivot_table(index="ecozone", columns="Land cover class",
                         values="VALUE", aggfunc="first")
    total = piv["Total area"]
    shares = pd.DataFrame(index=piv.index)
    for raw, slug, _ in LC_CLASSES:
        if raw in piv.columns:
            shares[slug] = (piv[raw] / total * 100).round(2)
    wide = feat_df.merge(shares.reset_index(), on="ecozone", how="left")
    wide.to_csv(f"{GEO_DIR}/statcan_landcover_ecozone.csv", index=False)
    missing = set(feat_df["ecozone"]) - set(shares.index)
    print(f"land cover: {wide.shape[0]} polygons x {len(shares.columns)} classes | "
          f"ecozones unmatched: {missing or 'none'}")


# ---------------------------------------------------------------------------
# C. Permafrost zones (categorical)
# ---------------------------------------------------------------------------
# NRCan Atlas of Canada, 5th edition permafrost map (Heginbottom et al., ~1995).
# Categorical: continuous / extensive discontinuous / sporadic discontinuous /
# isolated patches. The "No permafrost (O)" and "Subsea (U)" classes are dropped
# so the coloured polygons ARE the permafrost (the uncoloured south reads as none).
PERMAFROST_URL = ("https://ftp.cartes.canada.ca/pub/nrcan_rncan/"
                  "Surficial-geology_Geologie-des-depots-meubles/permafrost-pergelisol/"
                  "permafrost_atlas_of_canada_en.shp.zip")
PMF_LABELS = {   # PMF_CLASS -> clean label (source label spacing is inconsistent)
    "C": "Continuous (90–100%)",
    "E": "Extensive discontinuous (50–90%)",
    "S": "Sporadic discontinuous (10–50%)",
    "I": "Isolated patches (0–10%)",
}


def build_permafrost():
    """Permafrost-zone boundaries for the categorical map. Polygons are dissolved
    by zone (4 features) then simplified, keeping the file small."""
    zf = zipfile.ZipFile(_download_cache(PERMAFROST_URL, "/tmp/permafrost.shp.zip",
                                         "permafrost (NRCan Atlas 5th ed.)"))
    zf.extractall("/tmp/pf")
    shp = [f for f in zf.namelist() if f.endswith(".shp")][0]
    g = gpd.read_file(f"/tmp/pf/{shp}").to_crs(epsg=4326)
    g = g[g["PMF_CLASS"].isin(PMF_LABELS)].copy()       # drop No-permafrost (O) + Subsea (U)
    g["zone"] = g["PMF_CLASS"].map(PMF_LABELS)
    g = g.dissolve(by="zone", as_index=False)[["zone", "geometry"]]
    g["geometry"] = g["geometry"].simplify(SIMPLIFY_TOL, preserve_topology=True).buffer(0)
    gj = json.loads(g.to_json())
    rows = []
    for i, ft in enumerate(gj["features"]):
        fid = str(i)
        zone = ft["properties"]["zone"]
        rows.append(dict(fid=fid, zone=zone))
        ft["id"] = fid
        ft["properties"] = {"zone": zone}
        ft["geometry"]["coordinates"] = _rnd(ft["geometry"]["coordinates"])
    os.makedirs(GEO_DIR, exist_ok=True)
    _write_geojson(gj, f"{GEO_DIR}/permafrost_zones.geojson")
    pd.DataFrame(rows).to_csv(f"{GEO_DIR}/permafrost_zones.csv", index=False)
    print(f"permafrost: {len(rows)} zones")


# ---------------------------------------------------------------------------
# D. Wildfire ignition POINTS for one record year (2023) — NRCan NFDB
#    The "where the fires actually were" map that replaces the coarse by-province
#    % view: every recorded wildfire that year as a point sized by area burned.
# ---------------------------------------------------------------------------
NFDB_PNT_URL = ("https://cwfis.cfs.nrcan.gc.ca/downloads/nfdb/fire_pnt/"
                "current_version/NFDB_point.zip")
WILDFIRE_YEAR = 2023        # the record 17.6-million-hectare season
_FIRE_AGENCY = {
    "AB": "Alberta", "BC": "British Columbia", "MB": "Manitoba", "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador", "NS": "Nova Scotia", "NT": "Northwest Territories",
    "NU": "Nunavut", "ON": "Ontario", "PC": "Parks Canada", "PE": "Prince Edward Island",
    "QC": "Quebec", "SK": "Saskatchewan", "YT": "Yukon"}


def build_wildfire_points(year=WILDFIRE_YEAR):
    """Individual wildfire points for one year (default 2023, the record season) from
    NRCan's National Fire Database point archive — same authoritative NFDB source
    (OGL-Canada) as the weekly area-burned series, just the per-fire locations rather
    than provincial totals. Writes a light CSV (lat, lon, size_ha, province, cause);
    the page plots them as a Scattermap sized by area burned, so the few enormous
    boreal fires that drove the 17.6 Mha total stand out from the thousands of small
    ones. Point geometry is reprojected from the Atlas-Lambert source to WGS84."""
    print(f"Building {year} wildfire points (NRCan NFDB) ...")
    z = zipfile.ZipFile(_download_cache(NFDB_PNT_URL, "/tmp/nfdb_point.zip", "NFDB point archive"))
    z.extractall("/tmp/nfdb_point")
    shp = [n for n in z.namelist() if n.lower().endswith(".shp")][0]
    g = gpd.read_file(f"/tmp/nfdb_point/{shp}")
    g = g[(g["YEAR"] == year) & g["SIZE_HA"].notna() & (g["SIZE_HA"] > 0)].to_crs(epsg=4326)
    cause = {"L": "Lightning", "H": "Human", "H-PB": "Prescribed burn", "U": "Unknown"}
    out = pd.DataFrame({
        "lat": g.geometry.y.round(4), "lon": g.geometry.x.round(4),
        "size_ha": g["SIZE_HA"].round(1),
        "province": g["SRC_AGENCY"].map(_FIRE_AGENCY).fillna(g["SRC_AGENCY"]),
        "cause": g["CAUSE"].map(cause).fillna("Other / unknown") if "CAUSE" in g else "Unknown",
        "year": int(year),
    })
    out = out[out["lat"].between(40, 84) & out["lon"].between(-141, -52)]   # sane CA bounds
    out = out.sort_values("size_ha")          # big fires drawn last → on top
    path = f"{GEO_DIR}/nfdb_fire_points_{year}.csv"
    out.to_csv(path, index=False)
    print(f"  wrote {path}: {len(out)} fires, {round(os.path.getsize(path) / 1e6, 2)} MB; "
          f"total area {out['size_ha'].sum() / 1e6:.1f} Mha")


# Parks Canada "Places administered by Parks Canada" ArcGIS FeatureServer (OGL-Canada).
NATIONAL_PARKS_URL = ("https://services2.arcgis.com/wCOMu5IS7YdSyPNx/arcgis/rest/"
                      "services/vw_Places_Public_lieux_public_APCA/FeatureServer/0/query")


def build_national_parks():
    """Canada's national parks & park reserves as points (centroid + real area) for
    a proportional-symbol map, from the Parks Canada 'Places administered by Parks
    Canada' service (OGL-Canada). The ~46 National Park + National Park Reserve
    polygons are far too heavy to inline (~15 MB even server-simplified, because of
    the complex northern coastlines), and at national zoom their outlines read as
    dots anyway — so we keep only the centroid and the area (computed in an
    equal-area projection), and the page draws a bubble map sized by area. Marine
    conservation areas (which dwarf the land parks) and national historic sites (a
    different story) are excluded. One-time build, like the other static assets."""
    print("Building national parks (Parks Canada) ...")
    params = {"where": "PLACE_TYPE_E IN ('National Park','National Park Reserve')",
              "outFields": "PLACE_TYPE_E,DESC_EN", "returnGeometry": "true",
              "maxAllowableOffset": "0.02", "outSR": "4326", "f": "geojson"}
    r = requests.get(NATIONAL_PARKS_URL, params=params, timeout=180)
    r.raise_for_status()
    with open("/tmp/national_parks.geojson", "wb") as f:
        f.write(r.content)
    g = gpd.read_file("/tmp/national_parks.geojson")
    eq = g.to_crs("ESRI:102001")               # Canada Albers equal-area
    g["area_km2"] = (eq.area / 1e6).round(0)
    cent = eq.centroid.to_crs(epsg=4326)
    out = pd.DataFrame({
        "name": g["DESC_EN"].str.replace(" of Canada", "", regex=False),
        "type": g["PLACE_TYPE_E"],
        "lat": cent.y.round(4), "lon": cent.x.round(4),
        "area_km2": g["area_km2"].astype("Int64"),
    }).dropna(subset=["lat", "lon"]).sort_values("area_km2").reset_index(drop=True)
    path = f"{GEO_DIR}/national_parks.csv"
    out.to_csv(path, index=False)
    print(f"  wrote {path}: {len(out)} parks; largest {int(out['area_km2'].max()):,} km² "
          f"({round(os.path.getsize(path) / 1e3, 1)} KB)")


# CPCAD (Canadian Protected and Conserved Areas Database, ECCC) — the comprehensive
# national/provincial/territorial protected-areas layer. Used only for the detailed
# park-boundaries page (the headline %-conserved figures come from CESI).
CPCAD_URL = "https://maps-cartes.ec.gc.ca/arcgis/rest/services/CWS_SCF/CPCAD/MapServer/0"
# The recognisable park designations only — national + provincial + territorial parks
# (and Québec's "parcs nationaux", which are provincial). Excludes municipal/regional/
# urban/recreation/nature/historic/marine designations and non-park conservation areas.
_PARK_TYPES = ["National Park", "National Park Reserve", "National Urban Park",
               "Provincial Park", "Wildland Provincial Park", "Wilderness Park",
               "Territorial Park", "Quebec's national park", "Quebec's national park reserve",
               "Conservation Park"]   # Conservation Park = Gatineau Park only (NCC, federal)
_PARK_JUR = {"National Park": "Federal", "National Park Reserve": "Federal",
             "National Urban Park": "Federal", "Territorial Park": "Territorial",
             "Provincial Park": "Provincial", "Wildland Provincial Park": "Provincial",
             "Wilderness Park": "Provincial", "Quebec's national park": "Provincial",
             "Quebec's national park reserve": "Provincial", "Conservation Park": "Federal"}


def build_parks_detailed():
    """The actual SHAPES of Canada's national, provincial and territorial parks
    (terrestrial only) from CPCAD — for the dedicated, heavier park-boundaries page
    (linked from Protected Areas, the same light-index / heavy-detail split the
    census tract maps use). ~743 park polygons, server-simplified then repaired to
    ~1.1 MB of VALID geometry, each tagged with its jurisdiction (the page colours by
    it). Marine areas (which dwarf the land parks) and non-park designations are
    excluded. One-time build, like the other static geo assets.

    NOTE: the geometry MUST be valid — MapLibre GL silently fails to draw a layer that
    contains self-intersecting polygons (the map comes up blank). Two traps: (1)
    naive coordinate rounding introduces self-intersections — so we do NOT round; (2)
    grid-snapping (shapely set_precision) repairs validity but shatters the fragmented
    zoned parks into spiky shards — so we use TOPOLOGY-PRESERVING simplify instead.
    CPCAD maps protected ZONES, so some parks (e.g. Algonquin) are only their
    protected core, fragmented — that's the data, caveated on the page, not a bug."""
    print("Building detailed park boundaries (CPCAD terrestrial parks) ...")
    inlist = ",".join("'" + t.replace("'", "''") + "'" for t in _PARK_TYPES)
    # Exclude only MARINE biomes (not "filter to terrestrial") — the latter also drops
    # parks coded "Not included in statistics", which are real land parks.
    where = f"PA_BIOME NOT LIKE 'Marine%' AND TYPE_E IN ({inlist})"
    params = {"where": where, "outFields": "NAME_E,TYPE_E,O_AREA_HA",
              "returnGeometry": "true", "maxAllowableOffset": "0.002",
              "outSR": "4326", "f": "geojson"}
    import io
    from collections import Counter
    r = requests.get(CPCAD_URL + "/query", params=params, timeout=300)
    g = gpd.read_file(io.BytesIO(r.content))
    g = g[~g.geometry.is_empty & g.geometry.notna()].copy()
    # The ArcGIS output carries self-intersections, and MapLibre GL SILENTLY fails to
    # draw a layer with invalid polygons (blank map). Grid-snapping (set_precision)
    # repairs validity but shatters the fragmented zoned parks into spiky shards, so
    # use TOPOLOGY-PRESERVING simplification instead: buffer(0) to repair, then
    # simplify (which never introduces self-intersections), then buffer(0) again.
    g["geometry"] = g.geometry.buffer(0)
    g["geometry"] = g.geometry.simplify(0.0015, preserve_topology=True).buffer(0)
    g = g[~g.geometry.is_empty & g.geometry.notna()].copy()
    g["name"] = g["NAME_E"].fillna("").str.strip()
    g["type"] = g["TYPE_E"]
    g["jurisdiction"] = g["TYPE_E"].map(_PARK_JUR).fillna("Provincial")
    g["area_km2"] = (pd.to_numeric(g["O_AREA_HA"], errors="coerce").fillna(0) / 100).round().astype(int)
    g = g.sort_values("area_km2").reset_index(drop=True)
    g["fid"] = g.index.astype(str)
    gj = json.loads(g[["fid", "name", "type", "jurisdiction", "area_km2", "geometry"]].to_json())
    for ft in gj["features"]:
        ft["id"] = ft["properties"].pop("fid")
        # NB: do NOT round coordinates here — rounding after the repair re-introduces
        # self-intersections (and a blank MapLibre layer). The simplify(0.004) above
        # already controls vertex count / file size; buffer(0) must stay the last op.
    os.makedirs(GEO_DIR, exist_ok=True)
    _write_geojson(gj, f"{GEO_DIR}/parks_detailed.geojson")
    chk = gpd.read_file(f"{GEO_DIR}/parks_detailed.geojson")
    jc = Counter(f["properties"]["jurisdiction"] for f in gj["features"])
    print(f"  wrote parks_detailed.geojson: {len(gj['features'])} parks {dict(jc)}, "
          f"invalid={int((~chk.is_valid).sum())}, "
          f"{round(os.path.getsize(GEO_DIR + '/parks_detailed.geojson') / 1e6, 2)} MB")


# ---------------------------------------------------------------------------
# E. Major lakes — NRCan Atlas of Canada 1:1,000,000 waterbodies (OGL-Canada)
#    Canada has more lake area than any country; this highlights the largest named
#    lakes with their surface area (computed from the official outlines).
# ---------------------------------------------------------------------------
LAKES_URL = ("https://ftp.geogratis.gc.ca/pub/nrcan_rncan/vector/framework_cadre/"
             "Atlas_of_Canada_1M/hydrology/AC_1M_Waterbodies.shp.zip")


def build_lakes(top_n=40):
    """The largest lakes of Canada as highlighted polygons with surface-area stats,
    from NRCan's Atlas of Canada 1:1,000,000 waterbodies (OGL-Canada). Keeps the
    `top_n` largest **named, natural** lakes (reservoirs excluded) that touch Canada —
    so the shared Great Lakes are kept but wholly-US Lake Michigan is dropped — and
    computes each one's surface area in an equal-area projection. Writes a GeoJSON
    (id=lakeid, props.name/area_km2) the Water page maps as a blue choropleth shaded
    by area, plus a CSV. Areas are approximate (1:1M-scale outlines)."""
    print("Building major lakes (NRCan Atlas 1:1M waterbodies) ...")
    z = zipfile.ZipFile(_download_cache(LAKES_URL, "/tmp/ac1m_water.shp.zip", "1:1M waterbodies"))
    z.extractall("/tmp/ac1m_water")
    shp = [n for n in z.namelist() if n.lower().endswith(".shp") and "Waterbodies" in n][0]
    g = gpd.read_file(f"/tmp/ac1m_water/{shp}")
    g = g[g["NAME"].notna() & (g["RESERVOIR"].astype(str).str.lower() != "yes")].copy()

    def _fix(s):     # the dbf carries UTF-8 bytes tagged latin-1 → "RÃ©servoir" etc.
        try:
            return s.encode("latin-1").decode("utf-8")
        except Exception:
            return s
    g["NAME"] = g["NAME"].map(_fix)
    g = g.dissolve(by="NAME", as_index=False)                     # merge multi-part lakes
    g["area_km2"] = g.to_crs("ESRI:102001").area / 1e6            # Canada Albers equal-area
    cand = g.sort_values("area_km2", ascending=False).head(top_n + 15).to_crs(epsg=4326)
    canada = gpd.read_file(f"{GEO_DIR}/prov_2021.geojson").to_crs(epsg=4326).union_all()
    cand = cand[cand.intersects(canada)].head(top_n).reset_index(drop=True)
    cand["geometry"] = cand["geometry"].simplify(0.01, preserve_topology=True).buffer(0)
    cand["lakeid"] = cand.index.astype(str)
    cand["area_km2"] = cand["area_km2"].round(0)
    keep = cand[["lakeid", "NAME", "area_km2", "geometry"]].rename(columns={"NAME": "name"})
    gj = json.loads(keep.to_json())
    for ft in gj["features"]:
        ft["id"] = ft["properties"]["lakeid"]
        ft["geometry"]["coordinates"] = _rnd(ft["geometry"]["coordinates"])
    _write_geojson(gj, f"{GEO_DIR}/lakes_major.geojson")
    keep.drop(columns="geometry").to_csv(f"{GEO_DIR}/lakes_major.csv", index=False)
    print(f"  {len(keep)} lakes; largest {keep.iloc[0]['name']} {keep.iloc[0]['area_km2']:,.0f} km²")


CLIMATE_NORMALS_API = "https://api.weather.gc.ca/collections/climate-normals/items"


def build_climate_normals():
    """ECCC Canadian Climate Normals 1981-2010 — station POINT data (OGL-Canada), pulled
    from the MSC GeoMet **OGC API – Features** endpoint (decimal lat/lon, no DMS parsing).
    Mean daily temperature is NORMAL_ID=1 and total precipitation NORMAL_ID=56; MONTH 1=Jan,
    7=Jul, 13=annual. Writes a tidy point CSV (climate_id, station, province, lat, lon,
    jan_temp, jul_temp, annual_temp, annual_precip) for the proportional/coloured-point
    climate map. ~660 stations carry a temperature normal. Decennial — NOT weekly."""
    print("Building climate normals (ECCC 1981-2010, GeoMet OGC API) ...")

    def fetch(normal_id, month):
        url = f"{CLIMATE_NORMALS_API}?NORMAL_ID={normal_id}&MONTH={month}&limit=10000&f=json"
        r = requests.get(url, timeout=120)
        r.raise_for_status()
        return r.json()["features"]

    rows = {}
    for f in fetch(1, 1):                     # Jan mean temp = base (geometry + name)
        p = f["properties"]
        cid = p["CLIMATE_IDENTIFIER"]
        lon, lat = f["geometry"]["coordinates"]
        rows[cid] = dict(climate_id=cid, station=str(p["STATION_NAME"]).title(),
                         province=p["PROVINCE_CODE"], lat=round(lat, 4), lon=round(lon, 4),
                         jan_temp=p["VALUE"])

    def add(normal_id, month, col):
        for f in fetch(normal_id, month):
            p = f["properties"]
            cid = p["CLIMATE_IDENTIFIER"]
            if cid in rows:
                rows[cid][col] = p["VALUE"]

    add(1, 7, "jul_temp")
    add(1, 13, "annual_temp")
    add(56, 13, "annual_precip")
    df = pd.DataFrame(rows.values()).dropna(subset=["jan_temp", "jul_temp"])
    os.makedirs(GEO_DIR, exist_ok=True)
    df.to_csv(f"{GEO_DIR}/climate_normals.csv", index=False)
    print(f"  {len(df)} stations | Jan {df.jan_temp.min():.1f}..{df.jan_temp.max():.1f}°C "
          f"| Jul {df.jul_temp.min():.1f}..{df.jul_temp.max():.1f}°C")


CAR_BND_URL = ("https://www12.statcan.gc.ca/census-recensement/2021/geo/sip-pis/"
               "boundary-limites/files-fichiers/lcar000a21a_e.zip")
AG_FARMTYPE_URL = "https://www150.statcan.gc.ca/n1/tbl/csv/32100231-eng.zip"
AG_LANDUSE_URL = "https://www150.statcan.gc.ca/n1/tbl/csv/32100249-eng.zip"
# Farm-type categories by NAICS code. Cattle [1121] is split into its 6-digit children so
# dairy (112120) reads distinctly from beef (112110): at the 4-digit level [1121] lumps the
# two, which would mislabel dairy regions (Ontario/Quebec) as "cattle ranching".
FARM_CODES = {
    "1111": "Oilseed & grain", "1112": "Vegetable & melon", "1113": "Fruit & tree nut",
    "1114": "Greenhouse & nursery", "1119": "Other crop",
    "112110": "Beef cattle", "112120": "Dairy",
    "1122": "Hog & pig", "1123": "Poultry & egg", "1124": "Sheep & goat",
    "1129": "Other animal",
}


def build_agriculture():
    """Census of Agriculture 2021 (StatCan, OGL) by Census Agricultural Region (CAR, the
    natural agricultural geography, 72 regions). Writes car_2021.geojson (id=CARUID) +
    statcan_car_agriculture.csv with the **dominant farm type** (NAICS 4-digit farm type
    with the most farms; table 32-10-0231-01) and **cropland share** (land in crops ÷ CAR
    land area; 32-10-0249-01, hectares). Census = 5-yearly, NOT weekly."""
    print("Building agriculture (Census of Ag 2021 by CAR) ...")
    naics = "North American Industry Classification System (NAICS)"
    z = zipfile.ZipFile(_download_cache(AG_FARMTYPE_URL, "/tmp/ag_farmtype.zip", "Census of Ag farm type"))
    ft = pd.read_csv(z.open("32100231.csv"), dtype=str)
    ft = ft[ft["DGUID"].str.contains("S0501", na=False)].copy()   # CAR rows only
    ft["caruid"] = ft["DGUID"].str[9:]
    ft["code"] = ft[naics].str.extract(r"\[(\d+)\]")
    name_by_car = (ft.drop_duplicates("caruid").set_index("caruid")["GEO"]
                   .str.replace(r"\s*\[[^\]]*\]", "", regex=True))
    fc = ft[ft["code"].isin(FARM_CODES)].copy()                   # curated farm-type set
    fc["VALUE"] = pd.to_numeric(fc["VALUE"], errors="coerce")
    fc = fc.dropna(subset=["VALUE"])
    dom = (fc.loc[fc.groupby("caruid")["VALUE"].idxmax()]
           .set_index("caruid")["code"].map(FARM_CODES))
    total = (ft[ft[naics].str.startswith("Total number of farms")]
             .assign(v=lambda x: pd.to_numeric(x["VALUE"], errors="coerce"))
             .drop_duplicates("caruid").set_index("caruid")["v"])

    # Per-region farm-type breakdown for the hover: each type's share of total farms, sorted
    # high→low, top 6 above 2% (the dominant type colours the map; the hover gives the full
    # mix, like the diversity/religion maps). One pre-formatted string per CAR.
    fc["lbl"] = fc["code"].map(FARM_CODES)
    detail = {}
    for cid, grp in fc.groupby("caruid"):
        tf = total.get(cid)
        denom = tf if (pd.notna(tf) and tf > 0) else grp["VALUE"].sum()
        parts = [f"{r.lbl} {r.VALUE / denom * 100:.0f}%"
                 for r in grp.sort_values("VALUE", ascending=False).itertuples()
                 if r.VALUE / denom * 100 >= 2][:6]
        nf = int(tf) if pd.notna(tf) else int(grp["VALUE"].sum())
        detail[cid] = f"{nf:,} farms<br>" + " · ".join(parts)

    z2 = zipfile.ZipFile(_download_cache(AG_LANDUSE_URL, "/tmp/ag_landuse.zip", "Census of Ag land use"))
    lu = pd.read_csv(z2.open("32100249.csv"), dtype=str)
    lu = lu[lu["DGUID"].str.contains("S0501", na=False)
            & lu["Land use"].str.startswith("Land in crops")
            & (lu["Unit of measure"] == "Hectares")].copy()
    lu["caruid"] = lu["DGUID"].str[9:]
    crop_ha = (lu.assign(v=lambda x: pd.to_numeric(x["VALUE"], errors="coerce"))
               .drop_duplicates("caruid").set_index("caruid")["v"])

    bz = zipfile.ZipFile(_download_cache(CAR_BND_URL, "/tmp/lcar.zip", "CAR boundaries"))
    bz.extractall("/tmp/car_bnd")
    shp = [f for f in bz.namelist() if f.endswith(".shp")][0]
    g = gpd.read_file(f"/tmp/car_bnd/{shp}").to_crs(epsg=4326)
    g["caruid"] = g["CARUID"].astype(str)

    out = pd.DataFrame({"caruid": g["caruid"]})
    out["name"] = out["caruid"].map(name_by_car)
    out["province"] = out["name"].str.split(",").str[-1].str.strip()
    out["dominant_ftype"] = out["caruid"].map(dom)
    out["total_farms"] = out["caruid"].map(total)
    out["hover_detail"] = out["caruid"].map(detail)
    out["land_area_km2"] = out["caruid"].map(g.set_index("caruid")["LANDAREA"]).round(0)
    out["cropland_ha"] = out["caruid"].map(crop_ha)
    out["cropland_share"] = (out["cropland_ha"] / out["land_area_km2"]).round(2)  # %, 1 km²=100 ha
    out.to_csv(f"{GEO_DIR}/statcan_car_agriculture.csv", index=False)

    g["geometry"] = g["geometry"].simplify(SIMPLIFY_TOL, preserve_topology=True)
    gj = json.loads(g[["caruid", "geometry"]].to_json())
    for f in gj["features"]:
        f["id"] = f["properties"]["caruid"]
        f["geometry"]["coordinates"] = _rnd(f["geometry"]["coordinates"])
    _write_geojson(gj, f"{GEO_DIR}/car_2021.geojson")
    print(f"  {len(out)} CARs | {out['dominant_ftype'].dropna().nunique()} farm types | "
          f"cropland share {out['cropland_share'].min():.0f}–{out['cropland_share'].max():.0f}%")


DRAINAGE_URL = "https://www150.statcan.gc.ca/n1/en/pub/16-201-x/2017000/sec-1/m-c/m-c-1.1.zip"


def build_drainage():
    """Canada's drainage regions (Statistics Canada 16-201-X, OGL): 25 drainage regions
    nested under the 5 **ocean drainage areas** — Pacific, Arctic, Hudson Bay, Atlantic and
    the Gulf of Mexico. Writes drainage_2017.geojson (id=dr_code, props dr_name/oda_name) +
    a small CSV, for a 'Canada by watershed, not province' categorical map. Static geographic
    facts — NOT weekly."""
    print("Building drainage regions (StatCan 16-201-X) ...")
    z = zipfile.ZipFile(_download_cache(DRAINAGE_URL, "/tmp/drainage.zip", "drainage regions"))
    z.extractall("/tmp/drainage")
    shp = [n for n in z.namelist() if n.lower().endswith(".shp")][0]
    g = gpd.read_file(f"/tmp/drainage/{shp}").to_crs(epsg=4326)
    g["dr_code"] = g["DR_Code"].astype(str)
    g = g.rename(columns={"DR_Name": "dr_name", "ODA_Name": "oda_name"})
    g[["dr_code", "dr_name", "oda_name"]].to_csv(f"{GEO_DIR}/statcan_drainage.csv", index=False)
    # The file is dominated by the *count* of tiny Arctic islands, not vertices per polygon.
    # For a national overview, explode to single polygons in an equal-area CRS, keep parts
    # ≥ 50 km² (mainland + major islands), dissolve back, then generalise — keeps it light.
    parts = g.to_crs("ESRI:102001").explode(index_parts=False)
    parts = parts[parts.geometry.area / 1e6 >= 50]
    g = parts.dissolve(by="dr_code", aggfunc="first").reset_index().to_crs(epsg=4326)
    g["geometry"] = g["geometry"].simplify(0.05, preserve_topology=True).buffer(0)
    gj = json.loads(g[["dr_code", "dr_name", "oda_name", "geometry"]].to_json())
    for f in gj["features"]:
        f["id"] = f["properties"]["dr_code"]
        f["geometry"]["coordinates"] = _rnd(f["geometry"]["coordinates"])
    _write_geojson(gj, f"{GEO_DIR}/drainage_2017.geojson")
    print(f"  {len(g)} drainage regions / {g['oda_name'].nunique()} ocean basins")


CESI_CONS_BASE = ("https://www.canada.ca/content/dam/eccc/documents/csv/cesindicators/"
                  "canada-conserved-areas/2025")   # year-stamped folder — bump each release
PRUID_BY_NAME = {
    "Newfoundland and Labrador": "10", "Prince Edward Island": "11", "Nova Scotia": "12",
    "New Brunswick": "13", "Quebec": "24", "Ontario": "35", "Manitoba": "46",
    "Saskatchewan": "47", "Alberta": "48", "British Columbia": "59", "Yukon": "60",
    "Northwest Territories": "61", "Nunavut": "62",
}


def build_protected_areas():
    """Conserved areas (ECCC Canadian Environmental Sustainability Indicators, OGL) — the
    series Canada's **30%-by-2030 ("30 by 30")** target is tracked against. Writes a national
    time series (% of terrestrial + marine area conserved, 1990–) and a by-province snapshot
    (% of each province/territory's land conserved, latest year) → protected_areas_national.csv
    + protected_areas_by_prov.csv. Annual; the source URL is year-stamped (bump CESI_CONS_BASE).
    CSVs are latin-1 with a 2-row banner."""
    print("Building protected/conserved areas (ECCC CESI) ...")
    nat = pd.read_csv(_download_cache(f"{CESI_CONS_BASE}/1-conserved-areas-proportion.csv",
                                      "/tmp/cons_national.csv", "CESI conserved (national)"),
                      skiprows=2, encoding="latin-1")
    out_nat = pd.DataFrame({
        "year": pd.to_numeric(nat.iloc[:, 0], errors="coerce"),
        "terrestrial_pct": pd.to_numeric(nat.iloc[:, 4], errors="coerce"),
        "marine_pct": pd.to_numeric(nat.iloc[:, 8], errors="coerce"),
    }).dropna(subset=["year"])
    out_nat["year"] = out_nat["year"].astype(int)
    out_nat.to_csv(f"{GEO_DIR}/protected_areas_national.csv", index=False)

    prov = pd.read_csv(_download_cache(f"{CESI_CONS_BASE}/4-conserved-areas-terrestrial-province-territory.csv",
                                       "/tmp/cons_prov.csv", "CESI conserved (by province)"),
                       skiprows=2, encoding="latin-1").iloc[:, [0, 6]]
    prov.columns = ["name", "pct_conserved"]
    prov = prov[prov["name"].isin(PRUID_BY_NAME)].copy()
    prov["pruid"] = prov["name"].map(PRUID_BY_NAME)
    prov["pct_conserved"] = pd.to_numeric(prov["pct_conserved"], errors="coerce")
    prov[["pruid", "name", "pct_conserved"]].to_csv(f"{GEO_DIR}/protected_areas_by_prov.csv", index=False)
    print(f"  national {out_nat['year'].min()}–{out_nat['year'].max()}: terrestrial "
          f"{out_nat['terrestrial_pct'].iloc[-1]:.1f}% conserved | {len(prov)} provinces "
          f"({prov['pct_conserved'].min():.1f}–{prov['pct_conserved'].max():.1f}%)")


MRDEM_VRT = "/vsicurl/https://canelevation-dem.s3.ca-central-1.amazonaws.com/mrdem-30/mrdem-30-dtm.vrt"


def build_elevation(width=1100):
    """Elevation/terrain from NRCan's Medium-Resolution DEM (MRDEM-30, CanElevation, OGL).
    The national grid is ~30 m / multi-TB and "designed for streaming, not downloading", so
    we never download it — we open the DTM mosaic VRT through GDAL `/vsicurl/` and read only a
    COARSE downsampled overview of the whole country. Writes elevation_distribution.csv (share
    of land area by elevation band + cumulative 'below X m'). The province rasterize is the
    land mask. Static — NOT weekly. Needs rasterio."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.features import rasterize
    print("Building elevation (MRDEM-30 coarse national read) ...")
    os.environ.update(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", VSI_CACHE="TRUE",
                      GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES")
    with rasterio.open(MRDEM_VRT) as src:
        h = int(src.height * width / src.width)
        elev = src.read(1, out_shape=(h, width), resampling=Resampling.average).astype("float64")
        transform = src.transform * rasterio.Affine.scale(src.width / width, src.height / h)
        nodata = src.nodata
    prov = gpd.read_file(f"{GEO_DIR}/prov_2021.geojson").to_crs(3979)
    pr = rasterize([(g, int(u)) for g, u in zip(prov.geometry, prov["pruid"])],
                   out_shape=(h, width), transform=transform, fill=0, dtype="int32")
    land = (pr > 0) & np.isfinite(elev) & (elev != nodata) & (elev > -100) & (elev < 7000)

    edges = [0, 200, 500, 1000, 1500, 2000, 3000, 6000]
    counts, _ = np.histogram(np.clip(elev[land], 0, None), bins=edges)
    tot = int(counts.sum())
    rows, cum = [], 0.0
    for i in range(len(edges) - 1):
        pct = counts[i] / tot * 100
        cum += pct
        lab = f"{edges[i]:,}–{edges[i+1]:,} m" if edges[i + 1] < 6000 else f"over {edges[i]:,} m"
        rows.append(dict(band=lab, lower=edges[i], upper=edges[i + 1],
                         pct=round(pct, 2), cumulative_below=round(cum, 2)))
    pd.DataFrame(rows).to_csv(f"{GEO_DIR}/elevation_distribution.csv", index=False)

    print(f"  below 500 m: {rows[0]['pct'] + rows[1]['pct']:.0f}% of land | {len(rows)} bands")


RIVERS_URL = ("https://ftp.geogratis.gc.ca/pub/nrcan_rncan/vector/framework_cadre/"
              "Atlas_of_Canada_15M/hydrology/AC_15M_Rivers.shp.zip")


def build_rivers():
    """Major rivers — NRCan Atlas of Canada 1:15,000,000 'sparse' rivers (~150 named major
    rivers; the line companion to the 1:1M waterbodies the lakes come from, OGL-Canada).
    Clipped to Canada and written as rivers_major.geojson (props: name), to overlay on the
    watershed map so the flow toward each ocean basin is visible. Static — NOT weekly."""
    print("Building major rivers (NRCan Atlas 1:15M sparse) ...")
    z = zipfile.ZipFile(_download_cache(RIVERS_URL, "/tmp/ac15m_rivers.shp.zip", "1:15M rivers"))
    z.extractall("/tmp/ac15m_rivers")
    shp = [n for n in z.namelist() if n.endswith(".shp") and "Rivers_sparse" in n][0]
    g = gpd.read_file(f"/tmp/ac15m_rivers/{shp}").to_crs(epsg=4326)
    g = g.rename(columns={"NAME": "name"})[["name", "geometry"]]

    def _fix(s):     # Atlas DBF carries UTF-8 bytes tagged latin-1 → "RiviÃ¨re" etc.
        try:
            return s.encode("latin-1").decode("utf-8")
        except Exception:
            return s
    g["name"] = g["name"].map(lambda s: _fix(s) if isinstance(s, str) else s)
    canada = gpd.read_file(f"{GEO_DIR}/prov_2021.geojson").to_crs(epsg=4326).union_all()
    g = gpd.clip(g, canada)                                  # drop US-only rivers; trim transboundary
    g = g[~g.geometry.is_empty & g.geometry.notna()].copy()
    g["geometry"] = g["geometry"].simplify(0.01, preserve_topology=True)
    gj = json.loads(g.to_json())
    for f in gj["features"]:
        f["geometry"]["coordinates"] = _rnd(f["geometry"]["coordinates"])
    _write_geojson(gj, f"{GEO_DIR}/rivers_major.geojson")
    print(f"  {len(g)} river features (clipped to Canada)")


def build_elevation_relief(width=2800):
    """National relief image for the elevation page. Streams a COARSE overview of MRDEM-30,
    reprojects it from EPSG:3979 to **Web-Mercator (3857)** so it aligns with Plotly's
    basemap, applies a muted hypsometric tint (green low → tan/brown → near-white peaks),
    masks to Canada (transparent elsewhere), and writes elevation_relief.webp + the image's
    lon/lat **corner coordinates** (clockwise TL,TR,BR,BL — what a Plotly map `image`
    layer expects). The image is rendered in Mercator; the corners passed to the page are
    WGS84. Static; needs rasterio + matplotlib."""
    import numpy as np
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import reproject, transform as warp_transform, transform_bounds
    from rasterio.features import rasterize
    from matplotlib.colors import LinearSegmentedColormap
    from PIL import Image
    print("Building elevation relief (MRDEM-30 → 3857 hypsometric PNG) ...")
    os.environ.update(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", VSI_CACHE="TRUE",
                      GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES")
    with rasterio.open(MRDEM_VRT) as src:           # read coarse in the source CRS (3979)
        h0 = int(src.height * width / src.width)
        elev0 = src.read(1, out_shape=(h0, width), resampling=Resampling.average).astype("float32")
        st = src.transform * rasterio.Affine.scale(src.width / width, src.height / h0)
        scrs = src.crs
        nd = src.nodata if src.nodata is not None else -32767.0
    # Target a TIGHT Canada extent in 3857 — the full 3979 bbox reprojects to a huge,
    # mostly-empty box that wastes resolution; use the province bounds instead.
    prov = gpd.read_file(f"{GEO_DIR}/prov_2021.geojson")
    minx, miny, maxx, maxy = prov.to_crs(epsg=4326).total_bounds
    # The province polygons carry Arctic "sector" boundary lines to the North Pole, so
    # total_bounds maxy ≈ 90°; cap at the northernmost land (Ellesmere, ~83.1°N) or the
    # Mercator image becomes absurdly tall.
    maxy = min(maxy, 83.5)
    l3857, b3857, r3857, t3857 = transform_bounds("EPSG:4326", "EPSG:3857", minx, miny, maxx, maxy)
    dw = width
    dh = int(dw * (t3857 - b3857) / (r3857 - l3857))
    dt = rasterio.transform.from_bounds(l3857, b3857, r3857, t3857, dw, dh)
    dst = np.full((dh, dw), nd, dtype="float32")     # warp the small array into that 3857 grid
    reproject(elev0, dst, src_transform=st, src_crs=scrs, dst_transform=dt, dst_crs="EPSG:3857",
              resampling=Resampling.bilinear, src_nodata=nd, dst_nodata=nd)
    canada = rasterize([(g, 1) for g in prov.to_crs(epsg=3857).geometry], out_shape=(dh, dw),
                       transform=dt, fill=0, dtype="uint8").astype(bool)
    valid = canada & np.isfinite(dst) & (dst != nd) & (dst > -100) & (dst < 7000)
    e = np.sqrt(np.clip(np.where(valid, dst, 0.0), 0, 2500) / 2500.0)   # sqrt → more low-land range
    cmap = LinearSegmentedColormap.from_list("relief", [
        (0.00, "#6c8b66"), (0.18, "#8aa06b"), (0.42, "#c2b280"),
        (0.65, "#a98c6b"), (0.85, "#cabfb2"), (1.00, "#f2efe9")])
    rgba = cmap(e)
    rgba[..., 3] = np.where(valid, 1.0, 0.0)         # transparent off Canada
    # WebP (lossy, with alpha) keeps a high-resolution relief small enough to inline —
    # far lighter than PNG at the same size; smooth hypsometric gradients compress well.
    Image.fromarray((rgba * 255).astype("uint8"), "RGBA").save(
        f"{GEO_DIR}/elevation_relief.webp", "WEBP", quality=85, method=6)
    lons, lats = warp_transform("EPSG:3857", "EPSG:4326",   # 3857 bounds → lon/lat corners
                                [l3857, r3857, r3857, l3857], [t3857, t3857, b3857, b3857])
    coords = [[round(lons[i], 5), round(lats[i], 5)] for i in range(4)]   # TL, TR, BR, BL
    json.dump({"coordinates": coords}, open(f"{GEO_DIR}/elevation_relief.json", "w"))
    print(f"  3857 grid {dw}×{dh} | WebP {os.path.getsize(f'{GEO_DIR}/elevation_relief.webp')/1e3:.0f} KB "
          f"| land px {int(valid.sum()):,} | corners {coords}")


CGN_NATIONAL = ("https://ftp.cartes.canada.ca/pub/nrcan_rncan/vector/"
                "geobase_cgn_toponyme/prov_csv_eng/cgn_canada_csv_eng.zip")
PEAK_TERMS = {"Mount", "Mountain", "Peak", "Summit"}


def build_peaks(relevance_min=1_000_000, win=33):
    """Named-peaks vector layer for the elevation page ("Canada's mountains").

    Names/coords from NRCan's Canadian Geographical Names national CSV (Generic Term in
    Mount/Mountain/Peak/Summit, kept where 'Relevance at Scale' >= 1,000,000 -> ~757 prominent,
    nationally distributed summits). Elevation is NOT in CGN, so it is sampled from MRDEM-30 as a
    WINDOW-MAX (+/-33 cells ~ 1 km, take the max valid) -- a single-point read undershoots summits
    badly (Robson reads ~3556 vs 3954 m; window-max recovers ~3900). Writes
    data/geo/peaks_canada.geojson (+ .csv for download). Static one-time build (not weekly)."""
    import io

    import rasterio
    from rasterio.warp import transform as warp_transform
    from rasterio.windows import Window
    cache = "/tmp/cgn_canada_csv_eng.csv"
    if not os.path.exists(cache):
        print("Downloading Canadian Geographical Names (national CSV) ...")
        z = zipfile.ZipFile(io.BytesIO(requests.get(CGN_NATIONAL, timeout=180).content))
        nm = [n for n in z.namelist() if n.lower().endswith(".csv")][0]
        open(cache, "wb").write(z.read(nm))
    d = pd.read_csv(cache, encoding="latin-1", low_memory=False)
    m = d[d["Generic Term"].isin(PEAK_TERMS) & (d["Relevance at Scale"] >= relevance_min)]
    m = m.dropna(subset=["Latitude", "Longitude"]).reset_index(drop=True)
    print(f"Building peaks: sampling MRDEM window-max for {len(m)} summits ...")
    os.environ.update(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", VSI_CACHE="TRUE",
                      GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES")
    elevs = []
    with rasterio.open(MRDEM_VRT) as src:
        nd = src.nodata if src.nodata is not None else -32767.0
        xs, ys = warp_transform("EPSG:4326", src.crs,
                                m["Longitude"].tolist(), m["Latitude"].tolist())
        for i, (x, y) in enumerate(zip(xs, ys)):
            r, c = src.index(x, y)
            a = src.read(1, window=Window(c - win, r - win, 2 * win + 1, 2 * win + 1),
                         boundless=True, fill_value=nd).astype("float32")
            v = a[(a != nd) & (a > -100) & (a < 7000)]
            elevs.append(round(float(v.max()), 1) if v.size else None)
            if (i + 1) % 100 == 0:
                print(f"  {i + 1}/{len(m)}")
    m["elevation_m"] = elevs
    m = m.dropna(subset=["elevation_m"]).sort_values("elevation_m", ascending=False)
    feats = [{
        "type": "Feature",
        "geometry": {"type": "Point",
                     "coordinates": [round(r["Longitude"], 5), round(r["Latitude"], 5)]},
        "properties": {"name": r["Geographical Name"], "elevation_m": r["elevation_m"],
                       "province": r["Province - Territory"],
                       "relevance": int(r["Relevance at Scale"])},
    } for _, r in m.iterrows()]
    json.dump({"type": "FeatureCollection", "features": feats},
              open(f"{GEO_DIR}/peaks_canada.geojson", "w"))
    m.rename(columns={"Geographical Name": "name", "Province - Territory": "province"})[
        ["name", "province", "Latitude", "Longitude", "elevation_m"]].to_csv(
        f"{GEO_DIR}/peaks_canada.csv", index=False)
    print(f"Wrote {len(feats)} peaks -> {GEO_DIR}/peaks_canada.geojson")
    print(m[["Geographical Name", "elevation_m"]].head(8).to_string(index=False))


ARCTIC_ECOZONES = ["Arctic Cordillera", "Northern Arctic", "Southern Arctic"]


def build_treeline():
    """Arctic tree line for the elevation map, derived as the taiga-tundra boundary of the
    National Ecological Framework ecozones. Standard interpretation: the treeless Arctic/tundra
    ecozones meet the forested Taiga zones at the northern limit of trees, so the shared edge of
    the dissolved Arctic zones with the rest of the country IS the tree line (the Arctic-Ocean
    coast is excluded because it is not shared with a forested zone). Source: AAFC/ECCC National
    Ecological Framework (same as the ecozone map). Writes data/geo/treeline.geojson (one line)."""
    from shapely.ops import unary_union
    ez = gpd.read_file(f"{GEO_DIR}/nef_ecozones.geojson")
    ez["geometry"] = ez.geometry.buffer(0)               # repair invalid/simplified rings
    arctic = unary_union(ez[ez["ecozone"].isin(ARCTIC_ECOZONES)].geometry)
    rest = unary_union(ez[~ez["ecozone"].isin(ARCTIC_ECOZONES)].geometry)
    line = arctic.boundary.intersection(rest.buffer(0.03))   # shared edge; buffer bridges simplify gaps
    line = line.simplify(0.02)
    gpd.GeoSeries([line], crs=ez.crs).to_file(f"{GEO_DIR}/treeline.geojson", driver="GeoJSON")
    km = 0
    try:
        km = int(gpd.GeoSeries([line], crs=ez.crs).to_crs(3979).length.iloc[0] / 1000)
    except Exception:
        pass
    print(f"Wrote tree line ({km:,} km, taiga-tundra ecozone boundary) -> {GEO_DIR}/treeline.geojson")


LANDFORM_URL = ("https://ws.lioservices.lrc.gov.on.ca/arcgis1071a/rest/services/"
                "LIO_OPEN_DATA/LIO_Open06/MapServer")


def build_landforms():
    """Notable southern-Ontario landforms for the elevation map (review §437): the
    Niagara Escarpment Plan boundary (layer 25) and the Oak Ridges Moraine planning
    area (layer 29), from Land Information Ontario (OGL-Ontario). Each feature is
    tagged with `name`; written as one FeatureCollection (outlines, server-simplified)."""
    feats = []
    for lid, name in [(25, "Niagara Escarpment"), (29, "Oak Ridges Moraine")]:
        r = requests.get(f"{LANDFORM_URL}/{lid}/query",
            params={"where": "1=1", "outFields": "OGF_ID", "returnGeometry": "true",
                    "maxAllowableOffset": "0.002", "outSR": "4326", "f": "geojson"}, timeout=120)
        r.raise_for_status()
        for f in r.json().get("features", []):
            f["properties"] = {"name": name}
            feats.append(f)
    os.makedirs(GEO_DIR, exist_ok=True)
    _write_geojson({"type": "FeatureCollection", "features": feats}, f"{GEO_DIR}/landforms.geojson")
    print(f"  wrote landforms.geojson: {len(feats)} features, "
          f"{round(os.path.getsize(GEO_DIR + '/landforms.geojson') / 1e3)} KB")


if __name__ == "__main__":
    build_provinces()
    build_cma_density()
    build_ecozones()
    build_permafrost()
    build_wildfire_points()
    build_lakes()
    build_climate_normals()
    build_agriculture()
    build_drainage()
    build_rivers()
    build_protected_areas()
    build_elevation()
    build_elevation_relief()
    build_peaks()
    build_treeline()
    build_landforms()
    print("build_geography complete.")
