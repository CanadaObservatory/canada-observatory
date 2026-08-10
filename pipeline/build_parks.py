"""Authoritative agency PARK BOUNDARIES — a clean, recognisable "parks people know"
layer, kept DISTINCT from the broader Canadian Protected and Conserved Areas
(CPCAD) geometry on geography/parks.qmd.

This is the *official* boundary layer: where CPCAD maps reported/illustrative
protected ZONES (so e.g. Algonquin shows only its protected core, and Rouge can
look parcel-based), this layer maps the boundaries defined by the administering
agency — legislative for the federal parks, regulated/official for the provinces.
The two are shown together so the reader can compare the reported conservation
footprint with the familiar park.

Modular by jurisdiction so provinces/territories are added one at a time. Each
build writes its own data/geo/parks_<jur>.geojson with a SHARED harmonised schema;
geography/parks.qmd merges whatever files exist into one "Parks — official boundary"
overlay. Built so far: federal, Ontario, Alberta, British Columbia.

One-time static build (NOT in the weekly registry), like build_geography.py: fetch
raw from the live agency REST service at build time (the authoritative, versioned
source is the archive — we don't commit raw), then write the display-ready,
validity-repaired GeoJSON to data/geo/.

GEOMETRY GOTCHA (shared with build_parks_detailed): MapLibre GL SILENTLY draws a
BLANK layer if any polygon self-intersects. So repair with buffer(0) → topology-
preserving simplify → buffer(0), assert .is_valid before writing, and do NOT round
coordinates after the repair.
"""
import io
import os
import re
import json
from collections import Counter

import pandas as pd
import geopandas as gpd
import requests

GEO_DIR = "data/geo"
_UA = {"User-Agent": "CanadaObservatory/parks-build"}

# Harmonised, jurisdiction-agnostic schema. `park_id` keeps each source's
# authoritative identifier (never key on name); the map merges files and
# re-indexes, so cross-file id collisions don't matter.
HARMONISED_COLS = [
    "park_id", "name", "name_fr", "jurisdiction", "admin_level", "park_type",
    "province", "admin_agency", "boundary_kind", "source_agency", "source_dataset",
    "geometry_quality", "display_status", "geometry",
]


# --- shared fetch / repair / write ----------------------------------------
def _fetch_arcgis_geojson(query_url, where, out_fields, server_offset, page=1000):
    """Page through an ArcGIS REST query as GeoJSON (WGS84). `server_offset` is
    maxAllowableOffset in degrees (coarse server-side simplification to bound the
    payload); pagination handles layers above the server's maxRecordCount."""
    feats, offset = [], 0
    while True:
        params = {"where": where, "outFields": out_fields, "returnGeometry": "true",
                  "maxAllowableOffset": str(server_offset), "outSR": "4326",
                  "resultOffset": offset, "resultRecordCount": page, "f": "geojson"}
        r = requests.get(query_url, params=params, timeout=300, headers=_UA)
        r.raise_for_status()
        gj = r.json()
        fs = gj.get("features", [])
        feats += fs
        # Stop when the server returns a short page and isn't flagging more.
        if not fs or (len(fs) < page and not gj.get("exceededTransferLimit", False)):
            break
        offset += len(fs)
        if offset > 50000:   # safety stop
            break
    return gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")


def _repair(g, simplify_tol):
    """buffer(0) → topology-preserving simplify → buffer(0). Never round coords."""
    g = g[~g.geometry.is_empty & g.geometry.notna()].copy()
    g["geometry"] = g.geometry.buffer(0)
    g["geometry"] = g.geometry.simplify(simplify_tol, preserve_topology=True).buffer(0)
    return g[~g.geometry.is_empty & g.geometry.notna()].copy()


def _slug(s):
    """Normalised key from a park name (for jurisdictions with no stable id)."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _write_layer(g, out_path):
    """Validate + write the harmonised display GeoJSON, with a report line.

    Records sharing a `park_id` are dissolved into one (multi)polygon — so a park
    stored as several parcels (e.g. NS Blomidon) gets ONE shared hover identity,
    its non-contiguous parts preserved as a MultiPolygon (no forced contiguity)."""
    if g["park_id"].duplicated().any():
        g = g.dissolve(by="park_id", aggfunc="first", as_index=False)
        g["geometry"] = g.geometry.buffer(0)
    g = g.sort_values("name").reset_index(drop=True)
    gj = json.loads(g[HARMONISED_COLS].to_json())
    for ft in gj["features"]:
        ft["id"] = ft["properties"]["park_id"]   # top-level id for featureidkey
    os.makedirs(GEO_DIR, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    chk = gpd.read_file(out_path)
    tc = Counter(f["properties"]["park_type"] for f in gj["features"])
    print(f"  wrote {out_path}: {len(gj['features'])} parks {dict(tc)}, "
          f"invalid={int((~chk.is_valid).sum())}, "
          f"{round(os.path.getsize(out_path) / 1e6, 2)} MB")


_MINOR = {"and", "or", "of", "the", "de", "du", "des", "la", "le", "les", "aux"}


def _title(s):
    """Title-case, but keep minor words (and, of, de…) lower unless first."""
    words = (s or "").strip().split()
    return " ".join(w.lower() if i and w.lower() in _MINOR else w.title()
                    for i, w in enumerate(words))


# --- Federal: NRCan / Canada Lands Survey System --------------------------
FEDERAL_URL = ("https://proxyinternet.nrcan-rncan.gc.ca/arcgis/rest/services/"
               "CLSS-SATC/CLSS_Administrative_Boundaries/MapServer/1/query")
FEDERAL_DATASET = ("National Parks and National Park Reserves of Canada "
                   "Legislative Boundaries")
_FED_TYPE = {
    "National Park of Canada": "National Park",
    "National Park Reserve of Canada": "National Park Reserve",
    "Rouge National Urban Park": "National Urban Park",
    "Saguenay-St. Lawrence Marine Park": "Marine Park",
}
_PROV_FIX = {"Saskatwechan": "Saskatchewan"}


def build_federal_parks(simplify_tol=0.0006, server_offset=0.0004):
    """Federal parks (legal boundaries) → data/geo/parks_federal.geojson (46)."""
    print("Building federal parks (CLSS legislative boundaries) ...")
    g = _fetch_arcgis_geojson(
        FEDERAL_URL, "1=1",
        ("adminAreaId,adminAreaNameEng,adminAreaNameFra,distributionTypeEng,"
         "jurisdictionEng"), server_offset)
    g = _repair(g, simplify_tol)
    dtype = g["distributionTypeEng"]
    g["park_id"] = g["adminAreaId"].astype(str)
    g["park_type"] = dtype.map(_FED_TYPE).fillna("National Park")
    fancy = dtype.isin(["Rouge National Urban Park", "Saguenay-St. Lawrence Marine Park"])
    g["name"] = [d if f else _title(n).replace(" Of Canada", "")
                 for n, d, f in zip(g["adminAreaNameEng"], dtype, fancy)]
    g["name_fr"] = [(_title(n).replace(" Du Canada", "")) for n in g["adminAreaNameFra"]]
    g["province"] = g["jurisdictionEng"].replace(_PROV_FIX)
    g["jurisdiction"] = "Canada"
    g["admin_level"] = "federal"
    g["admin_agency"] = "Parks Canada"
    g["boundary_kind"] = "legal boundary"
    g["source_agency"] = "Natural Resources Canada / Parks Canada"
    g["source_dataset"] = FEDERAL_DATASET
    g["geometry_quality"] = "authoritative legislative boundary"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_federal.geojson")


# --- Ontario: Land Information Ontario ------------------------------------
ONTARIO_URL = ("https://ws.lioservices.lrc.gov.on.ca/arcgis2/rest/services/"
               "LIO_OPEN_DATA/LIO_Open03/MapServer/4/query")
ONTARIO_DATASET = "Provincial Park Regulated (Land Information Ontario)"


def build_ontario_parks(simplify_tol=0.0009, server_offset=0.0006):
    """Ontario regulated provincial parks → data/geo/parks_on.geojson (~347). The
    regulated boundary is the FULL legal park, so it shows e.g. all of Algonquin —
    unlike CPCAD, which records only the protected core of zoned parks."""
    print("Building Ontario parks (LIO Provincial Park Regulated) ...")
    import re
    g = _fetch_arcgis_geojson(
        ONTARIO_URL, "1=1",
        "OGF_ID,PROTECTED_AREA_NAME_ENG,PROTECTED_AREA_NAME_FR,PROVINCIAL_PARK_CLASS_ENG",
        server_offset)
    g = _repair(g, simplify_tol)
    g["park_id"] = "ON-" + g["OGF_ID"].astype(str)
    g["name"] = [_title(re.sub(r"\s*\([^)]*\)\s*$", "", n or "")) for n in g["PROTECTED_AREA_NAME_ENG"]]
    g["name_fr"] = [_title(re.sub(r"\s*\([^)]*\)\s*$", "", n or "")) for n in g["PROTECTED_AREA_NAME_FR"]]
    g["park_type"] = "Provincial Park"
    g["province"] = "Ontario"
    g["jurisdiction"] = "Ontario"
    g["admin_level"] = "provincial"
    g["admin_agency"] = "Ontario Parks"
    g["boundary_kind"] = "official boundary"
    g["source_agency"] = "Ontario Parks / Land Information Ontario"
    g["source_dataset"] = ONTARIO_DATASET
    g["geometry_quality"] = "regulated boundary"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_on.geojson")


# --- Alberta: Environment and Protected Areas -----------------------------
ALBERTA_URL = ("https://geospatial.alberta.ca/titan/rest/services/boundary/"
               "parks_protected_areas_alberta/FeatureServer/0/query")
ALBERTA_DATASET = "Parks and Protected Areas of Alberta"
# The recognisable PARK designations only. Excludes Provincial Recreation Areas
# (193 mostly-tiny day-use sites), Natural Areas, Ecological Reserves, Heritage
# Rangelands, and the 5 National Parks (already in the federal layer).
_AB_TYPE = {"PP": "Provincial Park", "WPP": "Wildland Provincial Park",
            "WA": "Wilderness Area", "WP": "Wilderness Park"}


def build_alberta_parks(simplify_tol=0.0009, server_offset=0.0006):
    """Alberta provincial parks → data/geo/parks_ab.geojson (~116). Boundaries
    interpreted from the legal descriptions in Orders-in-Council."""
    print("Building Alberta parks (AB Parks & Protected Areas) ...")
    where = "TYPE IN ('PP','WPP','WA','WP')"
    g = _fetch_arcgis_geojson(ALBERTA_URL, where, "PASITES_ID,NAME,OC_NAME,TYPE", server_offset)
    g = _repair(g, simplify_tol)
    g["park_id"] = ["AB-" + (str(int(i)) if pd.notna(i) else n)
                    for i, n in zip(g["PASITES_ID"], g["NAME"].astype(str))]
    g["name"] = [(_title(s) if (s or "").isupper() else (s or "").strip()) or _title(nm)
                 for s, nm in zip(g["OC_NAME"], g["NAME"].astype(str))]
    g["name_fr"] = ""
    g["park_type"] = g["TYPE"].map(_AB_TYPE).fillna("Provincial Park")
    g["province"] = "Alberta"
    g["jurisdiction"] = "Alberta"
    g["admin_level"] = "provincial"
    g["admin_agency"] = "Alberta Parks"
    g["boundary_kind"] = "official boundary"
    g["source_agency"] = "Alberta Environment and Protected Areas"
    g["source_dataset"] = ALBERTA_DATASET
    g["geometry_quality"] = "official boundary (Order-in-Council)"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_ab.geojson")


# --- British Columbia: BC Parks / DataBC ----------------------------------
BC_URL = ("https://delivery.maps.gov.bc.ca/arcgis/rest/services/mpcm/bcgwpub/"
          "MapServer/512/query")
BC_DATASET = "BC Parks, Ecological Reserves, and Protected Areas"
_BC_TYPE = {"PROVINCIAL PARK": "Provincial Park", "PROTECTED AREA": "Protected Area",
            "RECREATION AREA": "Recreation Area"}


def build_bc_parks(simplify_tol=0.0015, server_offset=0.001):
    """BC provincial parks → data/geo/parks_bc.geojson (~782). The biggest layer
    (huge coastal parks), so simplified more aggressively. Excludes the 148
    Ecological Reserves (research-only, not visited parks)."""
    print("Building BC parks (DataBC BC Parks/Protected Areas) ...")
    where = "PROTECTED_LANDS_DESIGNATION IN ('PROVINCIAL PARK','PROTECTED AREA','RECREATION AREA')"
    g = _fetch_arcgis_geojson(
        BC_URL, where, "ADMIN_AREA_SID,PROTECTED_LANDS_NAME,PROTECTED_LANDS_DESIGNATION",
        server_offset)
    g = _repair(g, simplify_tol)
    g["park_id"] = "BC-" + g["ADMIN_AREA_SID"].astype("Int64").astype(str)
    g["name"] = [_title(n) for n in g["PROTECTED_LANDS_NAME"]]
    g["name_fr"] = ""
    g["park_type"] = g["PROTECTED_LANDS_DESIGNATION"].map(_BC_TYPE).fillna("Provincial Park")
    g["province"] = "British Columbia"
    g["jurisdiction"] = "British Columbia"
    g["admin_level"] = "provincial"
    g["admin_agency"] = "BC Parks"
    g["boundary_kind"] = "official boundary"
    g["source_agency"] = "BC Parks / DataBC"
    g["source_dataset"] = BC_DATASET
    g["geometry_quality"] = "official boundary"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_bc.geojson")


# --- Québec: MELCCFP Registre des aires protégées -------------------------
QUEBEC_URL = ("https://geo.environnement.gouv.qc.ca/donnees/rest/services/"
              "Biodiversite/Aires_protegees/MapServer/4/query")
QUEBEC_DATASET = "Registre des aires protégées et des AMCE au Québec"
# Layer 4 is pre-isolated to the parcs nationaux (no réserves écologiques/fauniques).
_QC_TYPE = {"Parc national": "Provincial park (Québec)",
            "Réserve de parc national du Québec": "Provincial park reserve (Québec)"}


def build_quebec_parks(simplify_tol=0.0009, server_offset=0.0006):
    """Québec parcs nationaux → data/geo/parks_qc.geojson (~34). Québec calls its
    PROVINCIAL parks "parcs nationaux"; layer 4 of the MELCCFP registry already
    isolates them (no réserves écologiques/fauniques). French toponyms."""
    print("Building Québec parks (MELCCFP parcs nationaux) ...")
    g = _fetch_arcgis_geojson(QUEBEC_URL, "1=1", "MACODE,TOPONYME,DESIGNOM", server_offset)
    g = _repair(g, simplify_tol)
    g["park_id"] = "QC-" + g["MACODE"].astype("Int64").astype(str)
    g["name"] = g["TOPONYME"].astype(str).str.strip()
    g["name_fr"] = g["name"]
    g["park_type"] = g["DESIGNOM"].map(_QC_TYPE).fillna("Provincial park (Québec)")
    g["province"] = "Québec"
    g["jurisdiction"] = "Québec"
    g["admin_level"] = "provincial"
    g["admin_agency"] = "Québec / SÉPAQ"
    g["boundary_kind"] = "official boundary"
    g["source_agency"] = "MELCCFP (Registre des aires protégées)"
    g["source_dataset"] = QUEBEC_DATASET
    g["geometry_quality"] = "official boundary"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_qc.geojson")


# --- Manitoba: Manitoba Parks ---------------------------------------------
MANITOBA_URL = ("https://services.arcgis.com/mMUesHYPkXjaFGfS/arcgis/rest/services/"
                "Manitoba_Parks/FeatureServer/0/query")
MANITOBA_DATASET = "Manitoba Parks"


def build_manitoba_parks(simplify_tol=0.0009, server_offset=0.0006):
    """Manitoba provincial parks → data/geo/parks_mb.geojson (~93)."""
    print("Building Manitoba parks ...")
    g = _fetch_arcgis_geojson(MANITOBA_URL, "TYPE_E IN ('Provincial Park','Park Reserve')",
                              "NAME_E,NOM_F,TYPE_E", server_offset)
    g = _repair(g, simplify_tol)
    g["name"] = [_title(n) if (n or "").isupper() else (n or "").strip() for n in g["NAME_E"]]
    g["park_id"] = "MB-" + g["name"].map(_slug)
    g["name_fr"] = g["NOM_F"].astype(str).str.strip()
    g["park_type"] = g["TYPE_E"].fillna("Provincial Park")
    g["province"] = "Manitoba"
    g["jurisdiction"] = "Manitoba"
    g["admin_level"] = "provincial"
    g["admin_agency"] = "Manitoba Parks"
    g["boundary_kind"] = "official boundary"
    g["source_agency"] = "Manitoba Parks (Manitoba Government)"
    g["source_dataset"] = MANITOBA_DATASET
    g["geometry_quality"] = "official boundary"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_mb.geojson")


# --- Saskatchewan: Ministry of Parks, Culture and Sport -------------------
SASK_URL = ("https://gis.saskatchewan.ca/arcgis/rest/services/ParksAsLegislated/"
            "FeatureServer/0/query")
SASK_DATASET = "Saskatchewan Provincial Parks (as legislated)"


def build_saskatchewan_parks(simplify_tol=0.0009, server_offset=0.0006):
    """Saskatchewan provincial parks → data/geo/parks_sk.geojson (~36). Layer 0
    is pre-isolated to provincial parks (excludes recreation/historic sites)."""
    print("Building Saskatchewan parks ...")
    g = _fetch_arcgis_geojson(SASK_URL, "1=1", "PARKNM,PARKTYPE,PPID", server_offset)
    g = _repair(g, simplify_tol)
    g["park_id"] = "SK-" + g["PPID"].astype(str)
    g["name"] = [_title(n) if (n or "").isupper() else (n or "").strip() for n in g["PARKNM"]]
    g["name_fr"] = ""
    g["park_type"] = "Provincial Park"
    g["province"] = "Saskatchewan"
    g["jurisdiction"] = "Saskatchewan"
    g["admin_level"] = "provincial"
    g["admin_agency"] = "Saskatchewan Parks"
    g["boundary_kind"] = "official boundary"
    g["source_agency"] = "Saskatchewan Ministry of Parks, Culture and Sport"
    g["source_dataset"] = SASK_DATASET
    g["geometry_quality"] = "official boundary (as legislated)"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_sk.geojson")


# --- New Brunswick: GeoNB --------------------------------------------------
NB_URL = ("https://geonb.snb.ca/arcgis/rest/services/GeoNB_DNR_ProvincialParks/"
          "MapServer/0/query")
NB_DATASET = "GeoNB — Provincial Parks"


def build_nb_parks(simplify_tol=0.0006, server_offset=0.0004):
    """New Brunswick provincial parks → data/geo/parks_nb.geojson (~24). Single-
    purpose provincial-parks layer (includes a few provincial historic sites
    administered as parks)."""
    print("Building New Brunswick parks ...")
    g = _fetch_arcgis_geojson(NB_URL, "1=1", "NAME,Nom", server_offset)
    g = _repair(g, simplify_tol)
    g["name"] = [_title(n) if (n or "").isupper() else (n or "").strip() for n in g["NAME"]]
    g["park_id"] = "NB-" + g["name"].map(_slug)
    g["name_fr"] = g["Nom"].astype(str).str.strip()
    g["park_type"] = "Provincial Park"
    g["province"] = "New Brunswick"
    g["jurisdiction"] = "New Brunswick"
    g["admin_level"] = "provincial"
    g["admin_agency"] = "NB Parks"
    g["boundary_kind"] = "official boundary"
    g["source_agency"] = "GeoNB / NB Department of Natural Resources and Energy Development"
    g["source_dataset"] = NB_DATASET
    g["geometry_quality"] = "official boundary"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_nb.geojson")


# --- Nova Scotia: NS Protected Areas System -------------------------------
NS_URL = ("https://nsgiwa.novascotia.ca/arcgis/rest/services/ENV/"
          "ENV_NS_Prot_Area_Sys_UT83/MapServer/0/query")
NS_DATASET = "Nova Scotia Protected Areas System"


def build_ns_parks(simplify_tol=0.0006, server_offset=0.0004):
    """Nova Scotia provincial parks → data/geo/parks_ns.geojson (~24). Filtered to
    the Provincial Park designation (the system also holds wilderness areas /
    nature reserves / land trusts, which are not parks)."""
    print("Building Nova Scotia parks ...")
    g = _fetch_arcgis_geojson(NS_URL, "Protect1='Provincial Park'",
                              "Pro_Name,Protect1", server_offset)
    g = _repair(g, simplify_tol)
    g["name"] = [_title(n) if (n or "").isupper() else (n or "").strip() for n in g["Pro_Name"]]
    g["park_id"] = "NS-" + g["name"].map(_slug)
    g["name_fr"] = ""
    g["park_type"] = "Provincial Park"
    g["province"] = "Nova Scotia"
    g["jurisdiction"] = "Nova Scotia"
    g["admin_level"] = "provincial"
    g["admin_agency"] = "Nova Scotia Parks"
    g["boundary_kind"] = "official boundary"
    g["source_agency"] = "Nova Scotia Environment and Climate Change"
    g["source_dataset"] = NS_DATASET
    g["geometry_quality"] = "official boundary"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_ns.geojson")


# --- Yukon: Geomatics Yukon ------------------------------------------------
YUKON_URL = ("https://mapservices.gov.yk.ca/arcgis/rest/services/GeoYukon/"
             "GY_ParksProtectedAreas/MapServer/4/query")
YUKON_DATASET = "GeoYukon — Parks and Protected Areas"


def build_yukon_parks(simplify_tol=0.0012, server_offset=0.0008):
    """Yukon territorial parks → data/geo/parks_yt.geojson (4: Herschel Island/
    Qikiqtaruk, Tombstone, Kusawa, Chasàn Chùa). Excludes national parks (federal)
    and the non-park protected-area designations."""
    print("Building Yukon parks ...")
    g = _fetch_arcgis_geojson(YUKON_URL, "PARK_TYPE='Territorial Park'",
                              "PARK_NAME,PARK_TYPE,PARK1M_ID", server_offset)
    g = _repair(g, simplify_tol)
    g["park_id"] = "YT-" + g["PARK1M_ID"].astype("Int64").astype(str)
    g["name"] = [_title(n) if (n or "").isupper() else (n or "").strip() for n in g["PARK_NAME"]]
    g["name_fr"] = ""
    g["park_type"] = "Territorial Park"
    g["province"] = "Yukon"
    g["jurisdiction"] = "Yukon"
    g["admin_level"] = "territorial"
    g["admin_agency"] = "Yukon Parks"
    g["boundary_kind"] = "official boundary"
    g["source_agency"] = "Government of Yukon (Geomatics Yukon)"
    g["source_dataset"] = YUKON_DATASET
    g["geometry_quality"] = "official boundary (1:1M)"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_yt.geojson")


# --- National Capital Commission: Gatineau Park ---------------------------
NCC_URL = ("https://services2.arcgis.com/WLyMuW006nKOfa5Z/ArcGIS/rest/services/"
           "Gatineau_Park_Land_Designation_2021_view2/FeatureServer/0/query")
NCC_DATASET = "NCC Gatineau Park Land Use Designations 2021"


def build_ncc_parks(simplify_tol=0.0006, server_offset=0.0004):
    """Gatineau Park (federal, National Capital Commission) → data/geo/parks_ncc.geojson.
    Gatineau is administered by the NCC — neither a national park (CLSS) nor a Québec
    parc national — so it falls outside both agency datasets. Its boundary here is the
    dissolve of the NCC's 2021 land-use-designation zones (≈363 km²)."""
    print("Building Gatineau Park (NCC) ...")
    g = _fetch_arcgis_geojson(NCC_URL, "1=1", "GPMP_LABEL_EN", server_offset)
    g = g[~g.geometry.is_empty & g.geometry.notna()].copy()
    g["geometry"] = g.geometry.buffer(0)      # repair zones before the union
    g = g.dissolve().reset_index(drop=True)   # land-use zones → one park boundary
    g = _repair(g, simplify_tol)
    g["park_id"] = "NCC-gatineau"
    g["name"] = "Gatineau Park"
    g["name_fr"] = "Parc de la Gatineau"
    g["park_type"] = "Conservation Park"
    g["province"] = "Québec"
    g["jurisdiction"] = "Canada"
    g["admin_level"] = "federal"
    g["admin_agency"] = "National Capital Commission"
    g["boundary_kind"] = "official boundary"
    g["source_agency"] = "National Capital Commission (NCC)"
    g["source_dataset"] = NCC_DATASET
    g["geometry_quality"] = "official boundary (land-use plan extent)"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/parks_ncc.geojson")


# --- National Capital Commission: Ottawa Greenbelt ------------------------
GREENBELT_URL = ("https://services2.arcgis.com/WLyMuW006nKOfa5Z/ArcGIS/rest/services/"
                 "Greenbelt_Land_Designations_2013/FeatureServer/0/query")
GREENBELT_DATASET = "NCC Greenbelt Land Use Designations 2013"


def build_greenbelt(simplify_tol=0.0006, server_offset=0.0004):
    """Ottawa Greenbelt (NCC) → data/geo/greenbelt_ncc.geojson. NOT a park — a band
    of NCC conservation land, forest, wetland and farmland around Ottawa; the page
    draws it as a SEPARATE overlay from the parks. Boundary = dissolve of the NCC's
    2013 land-use designations (~224 km²)."""
    print("Building Ottawa Greenbelt (NCC) ...")
    g = _fetch_arcgis_geojson(GREENBELT_URL, "1=1", "DESGTN_EN", server_offset)
    g = g[~g.geometry.is_empty & g.geometry.notna()].copy()
    g["geometry"] = g.geometry.buffer(0)      # repair zones before the union
    g = g.dissolve().reset_index(drop=True)
    g = _repair(g, simplify_tol)
    g["park_id"] = "NCC-greenbelt"
    g["name"] = "Ottawa Greenbelt"
    g["name_fr"] = "Ceinture de verdure de la capitale"
    g["park_type"] = "Greenbelt (conservation land)"
    g["province"] = "Ontario"
    g["jurisdiction"] = "Canada"
    g["admin_level"] = "federal"
    g["admin_agency"] = "National Capital Commission"
    g["boundary_kind"] = "conservation land"
    g["source_agency"] = "National Capital Commission (NCC)"
    g["source_dataset"] = GREENBELT_DATASET
    g["geometry_quality"] = "official boundary (land-use plan extent)"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/greenbelt_ncc.geojson")


# --- Ontario Greenbelt: Land Information Ontario --------------------------
GREENBELT_ON_URL = ("https://ws.lioservices.lrc.gov.on.ca/arcgis1071a/rest/services/"
                    "LIO_OPEN_DATA/LIO_Open06/MapServer/17/query")
GREENBELT_ON_DATASET = "Greenbelt Outer Boundary (Land Information Ontario)"


def build_ontario_greenbelt(simplify_tol=0.0005, server_offset=0.0004):
    """Ontario Greenbelt → data/geo/greenbelt_on.geojson. NOT a park — the ~810,000 ha
    band of permanently protected countryside, farmland and natural areas across the
    Greater Golden Horseshoe (the Niagara Escarpment and Oak Ridges Moraine plan areas,
    specialty-crop land and urban river valleys), established under Ontario's Greenbelt
    Act, 2005. Drawn on the page as its own overlay, distinct from both the parks and
    the federal Ottawa Greenbelt. The LIO layer is already ONE assembled outer boundary
    whose interior rings cut out the excluded towns — so NO dissolve (that would fill
    the holes); just repair + simplify, which preserve the rings."""
    print("Building Ontario Greenbelt (LIO outer boundary) ...")
    g = _fetch_arcgis_geojson(GREENBELT_ON_URL, "1=1", "OGF_ID", server_offset)
    g = _repair(g, simplify_tol)
    g["park_id"] = "ON-greenbelt"
    g["name"] = "Ontario Greenbelt"
    g["name_fr"] = "Ceinture de verdure de l'Ontario"
    g["park_type"] = "Greenbelt (land-use designation)"
    g["province"] = "Ontario"
    g["jurisdiction"] = "Ontario"
    g["admin_level"] = "provincial"
    g["admin_agency"] = "Government of Ontario"
    g["boundary_kind"] = "protected countryside"
    g["source_agency"] = "Land Information Ontario"
    g["source_dataset"] = GREENBELT_ON_DATASET
    g["geometry_quality"] = "official boundary (Greenbelt Act, 2005)"
    g["display_status"] = "include"
    _write_layer(g, f"{GEO_DIR}/greenbelt_on.geojson")


# --- provenance -----------------------------------------------------------
_SOURCES = {
    "Canada": dict(admin_level="federal",
        source_agency="Natural Resources Canada (Canada Lands Survey System)",
        source_dataset=FEDERAL_DATASET,
        source_url="https://open.canada.ca/data/en/dataset/9e1507cd-f25c-4c64-995b-6563bf9d65bd",
        licence="Open Government Licence – Canada", boundary_type="legislative (legal)"),
    "Ontario": dict(admin_level="provincial",
        source_agency="Ontario Parks / Land Information Ontario",
        source_dataset=ONTARIO_DATASET,
        source_url="https://geohub.lio.gov.on.ca/datasets/lio::provincial-park-regulated",
        licence="Open Government Licence – Ontario", boundary_type="regulated (official)"),
    "Alberta": dict(admin_level="provincial",
        source_agency="Alberta Environment and Protected Areas",
        source_dataset=ALBERTA_DATASET,
        source_url="https://open.alberta.ca/opendata/gda-6b96341f-2e19-4885-98af-66d12ed4f8dd",
        licence="Open Government Licence – Alberta", boundary_type="official (Order-in-Council)"),
    "British Columbia": dict(admin_level="provincial",
        source_agency="BC Parks / DataBC",
        source_dataset=BC_DATASET,
        source_url="https://catalogue.data.gov.bc.ca/dataset/1130248f-f1a3-4956-8b2e-38d29d3e4af7",
        licence="Open Government Licence – British Columbia", boundary_type="official"),
    "Québec": dict(admin_level="provincial",
        source_agency="Ministère de l'Environnement, de la Lutte contre les changements climatiques, de la Faune et des Parcs (MELCCFP)",
        source_dataset=QUEBEC_DATASET,
        source_url="https://www.donneesquebec.ca/recherche/dataset/aires-protegees-au-quebec",
        licence="Creative Commons Attribution 4.0 (CC-BY) – Québec", boundary_type="official"),
    "Manitoba": dict(admin_level="provincial",
        source_agency="Manitoba Parks (Manitoba Government)", source_dataset=MANITOBA_DATASET,
        source_url="https://geoportal.gov.mb.ca/",
        licence="Manitoba Open Data Licence", boundary_type="official"),
    "Saskatchewan": dict(admin_level="provincial",
        source_agency="Saskatchewan Ministry of Parks, Culture and Sport", source_dataset=SASK_DATASET,
        source_url="https://gis.saskatchewan.ca/",
        licence="Government of Saskatchewan Standard Unrestricted Use Data License v2.0",
        boundary_type="official (as legislated)"),
    "New Brunswick": dict(admin_level="provincial",
        source_agency="GeoNB / NB Department of Natural Resources and Energy Development",
        source_dataset=NB_DATASET, source_url="https://geonb.snb.ca/",
        licence="Open Government Licence – New Brunswick", boundary_type="official"),
    "Nova Scotia": dict(admin_level="provincial",
        source_agency="Nova Scotia Environment and Climate Change", source_dataset=NS_DATASET,
        source_url="https://data.novascotia.ca/", licence="Open Government Licence – Nova Scotia",
        boundary_type="official"),
    "Yukon": dict(admin_level="territorial",
        source_agency="Government of Yukon (Geomatics Yukon)", source_dataset=YUKON_DATASET,
        source_url="https://mapservices.gov.yk.ca/", licence="Open Government Licence – Yukon",
        boundary_type="official (1:1M)"),
    "Gatineau Park (NCC)": dict(admin_level="federal",
        source_agency="National Capital Commission (NCC)", source_dataset=NCC_DATASET,
        source_url="https://open.canada.ca/data/en/dataset/706bf577-0603-4bef-85da-d5c3736081a0",
        licence="Open Government Licence – Canada", boundary_type="official (land-use plan extent)"),
    "Ottawa Greenbelt (NCC)": dict(admin_level="federal",
        source_agency="National Capital Commission (NCC)", source_dataset=GREENBELT_DATASET,
        source_url="https://open.canada.ca/data/en/dataset/420259e4-304f-4b46-a7f8-ffc76a66c2fa",
        licence="Open Government Licence – Canada", boundary_type="conservation land (not a park)"),
    "Ontario Greenbelt": dict(admin_level="provincial",
        source_agency="Land Information Ontario / Government of Ontario",
        source_dataset=GREENBELT_ON_DATASET,
        source_url="https://geohub.lio.gov.on.ca/datasets/lio::greenbelt-outer-boundary",
        licence="Open Government Licence – Ontario",
        boundary_type="protected countryside (land-use designation, not a park)"),
}

# Jurisdictions with NO usable authoritative open boundary (documented gaps; these
# parks show only as the reported CPCAD fill on the map):
#   Prince Edward Island — provincial-parks GIS exists but under a custom licence
#     that forbids redistribution (not on the open hub).
#   Newfoundland & Labrador — shapefile exists but its licence prohibits
#     redistribution; would need CREA-style written permission.
#   Northwest Territories — territorial parks are published only as POINTS, no
#     open polygon boundaries.
#   Nunavut — no GN open GIS; only the federal CPCAD carries the 9 territorial
#     parks (already our base layer, so no independent comparison to add).


def write_sources():
    path = f"{GEO_DIR}/parks_sources.csv"
    rows = [dict(jurisdiction=j, **v) for j, v in _SOURCES.items()]
    pd.DataFrame(rows).to_csv(path, index=False)
    print(f"  wrote {path}: {len(rows)} jurisdiction(s)")


if __name__ == "__main__":
    build_federal_parks()
    build_ontario_parks()
    build_alberta_parks()
    build_bc_parks()
    build_quebec_parks()
    build_manitoba_parks()
    build_saskatchewan_parks()
    build_nb_parks()
    build_ns_parks()
    build_yukon_parks()
    build_ncc_parks()
    build_greenbelt()
    build_ontario_greenbelt()
    write_sources()
