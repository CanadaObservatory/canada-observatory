"""Indigenous lands of Canada — Indian reserves + land-claim settlement lands.

Authoritative legal boundaries from Natural Resources Canada / Canada Lands Survey
System (CLSS): the **"Aboriginal Lands of Canada Legislative Boundaries"** dataset
(Open Government Licence – Canada) — the same CLSS service as the federal-parks
legislative boundaries (build_parks.py), layer 0. One-time static build (NOT in the
weekly registry), like build_geography.py: fetch the live REST layer at build time,
categorise, validity-repair, and write a display-ready GeoJSON to data/geo/.

These are the **legal/administrative** boundaries of lands set apart as Indian
reserves and of lands recognised under modern comprehensive land-claim and
self-government agreements. They are NOT traditional or treaty territories (which
have no single authoritative boundary and are deliberately not mapped here).
Descriptive only — the official boundaries, plotted, with no commentary.

GEOMETRY GOTCHA (shared with build_parks): MapLibre GL silently draws a BLANK layer if
any polygon self-intersects → repair with buffer(0) → topology-preserving simplify →
buffer(0); assert validity before writing; never round coordinates after the repair.
"""
import os
import json
from collections import Counter

import geopandas as gpd

from pipeline.build_parks import _fetch_arcgis_geojson, _repair, _title, GEO_DIR

URL = ("https://proxyinternet.nrcan-rncan.gc.ca/arcgis/rest/services/"
       "CLSS-SATC/CLSS_Administrative_Boundaries/MapServer/0/query")
DATASET = "Aboriginal Lands of Canada Legislative Boundaries"

# distributionTypeEng -> display category. "Indian Reserve" is the bulk; the named
# lands (Cree and Naskapi, Gwich'in, Inuit Owned, Inuvialuit, Sahtu, Sechelt, Tlicho,
# Yukon First Nations Settlement, Salt River) are modern comprehensive land-claim /
# self-government settlement lands; "Indian Land" is a small separate legal category.
RESERVE = "First Nations reserve"
SETTLEMENT = "Land-claim settlement land"
OTHER = "First Nations land"
_CATEGORY = {"Indian Reserve": RESERVE, "Indian Land": OTHER}
# The source distributionType uses the Indian Act's legal term "Indian"; relabel the
# DISPLAY type to the modern "First Nations" form (the named settlement-land types are
# already current terms, so they pass through unchanged).
_FULL_TYPE = {"Indian Reserve": "First Nations reserve", "Indian Land": "First Nations land"}
CATEGORY_ORDER = [RESERVE, SETTLEMENT, OTHER]


def build_indigenous_lands(simplify_tol=0.0015, server_offset=0.001):
    """Indian reserves + land-claim settlement lands → data/geo/indigenous_lands.geojson."""
    print("Building Indigenous lands (CLSS legislative boundaries) ...")
    g = _fetch_arcgis_geojson(
        URL, "1=1",
        "adminAreaId,adminAreaNameEng,distributionTypeEng,jurisdictionEng",
        server_offset)
    print(f"  fetched {len(g)} raw features")
    g = _repair(g, simplify_tol)
    dt = g["distributionTypeEng"].fillna("")
    g["category"] = [_CATEGORY.get(d, SETTLEMENT) for d in dt]
    g["full_type"] = [_FULL_TYPE.get(d, d) for d in dt]
    g["name"] = [_title(n) for n in g["adminAreaNameEng"].fillna("")]
    g["province"] = g["jurisdictionEng"].fillna("")
    g["land_id"] = g["adminAreaId"].astype(str)
    g = g[["land_id", "name", "category", "full_type", "province", "geometry"]].copy()
    # A land stored as several parcels shares one adminAreaId → dissolve to one identity
    # (non-contiguous parts kept as a MultiPolygon), so the hover reads as one place.
    if g["land_id"].duplicated().any():
        g = g.dissolve(by="land_id", aggfunc="first", as_index=False)
        g["geometry"] = g.geometry.buffer(0)
    g = g[~g.geometry.is_empty & g.geometry.notna()].sort_values("name").reset_index(drop=True)

    gj = json.loads(g.to_json())
    for ft in gj["features"]:
        ft["id"] = ft["properties"]["land_id"]   # top-level id for featureidkey
    out = f"{GEO_DIR}/indigenous_lands.geojson"
    os.makedirs(GEO_DIR, exist_ok=True)
    with open(out, "w") as f:
        json.dump(gj, f, separators=(",", ":"))
    chk = gpd.read_file(out)
    cc = Counter(ft["properties"]["category"] for ft in gj["features"])
    print(f"  wrote {out}: {len(gj['features'])} lands {dict(cc)}, "
          f"invalid={int((~chk.is_valid).sum())}, "
          f"{round(os.path.getsize(out) / 1e6, 2)} MB")


if __name__ == "__main__":
    build_indigenous_lands()
