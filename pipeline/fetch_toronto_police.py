"""
Toronto Police Service open data (ArcGIS FeatureServer) — the municipal /
neighbourhood "texture" layer beneath the StatCan CMA comparison.

Toronto is the one big-city portal worth a deep dive: official, point/neighbourhood
data, OGL-Ontario, one ArcGIS org. We deliberately do NOT build a cross-city
comparison from city portals — offence taxonomies, geographies and counting rules
differ (VPD even states its data is "not cross comparable" with StatCan), so the
honest cross-city layer stays the StatCan UCR (35-10-0177-01). This module is the
"drill into your own city" companion.

Two reproducible pulls:
  * fetch_toronto_neighbourhood_crime() — the keystone. The Neighbourhood Crime
    Rates layer is a 158-neighbourhood POLYGON already carrying counts + rates per
    100k + population for 9 crime types × 2014–latest, pre-aggregated to TPS's
    official counting basis. One request → a simplified boundary GeoJSON
    (id = HOOD_ID) + a latest-year rate CSV → a dropdown choropleth.
  * fetch_toronto_bike_thefts() — server-side aggregation of the Bicycle Thefts
    point set into month-of-year seasonality + recovery status (≈1% recovered) +
    a yearly count, without paging ~40k points.

Attribution (required): "Contains information licensed under the Open Government
Licence – Ontario."
"""

import json
import logging
from pathlib import Path

import requests

from pipeline.config import DATA_DIR

logger = logging.getLogger(__name__)

TPS_ROOT = ("https://services.arcgis.com/S9th0jAJ7bqgIRjw/arcgis/rest/services")
ATTRIB = ("Source: Toronto Police Service Public Safety Data Portal. Contains "
          "information licensed under the Open Government Licence – Ontario.")

# Neighbourhood Crime Rates crime stems -> (slug, display label).
NCR_STEMS = {
    "AUTOTHEFT":   ("auto_theft",     "Auto theft"),
    "BIKETHEFT":   ("bike_theft",     "Bike theft"),
    "BREAKENTER":  ("break_enter",    "Break & enter"),
    "THEFTFROMMV": ("theft_from_mv",  "Theft from vehicle"),
    "ROBBERY":     ("robbery",        "Robbery"),
    "ASSAULT":     ("assault",        "Assault"),
    "THEFTOVER":   ("theft_over",     "Theft over $5,000"),
    "SHOOTING":    ("shooting",       "Shootings & discharges"),
    "HOMICIDE":    ("homicide",       "Homicide"),
}


def _arcgis_query(service, layer, params):
    """One ArcGIS FeatureServer /query call → parsed JSON."""
    url = f"{TPS_ROOT}/{service}/FeatureServer/{layer}/query"
    r = requests.get(url, params=params, timeout=120)
    r.raise_for_status()
    return r.json()


def _round_coords(obj, ndigits=5):
    """Recursively round GeoJSON coordinate floats (shrinks the inlined boundary)."""
    if isinstance(obj, float):
        return round(obj, ndigits)
    if isinstance(obj, list):
        return [_round_coords(x, ndigits) for x in obj]
    return obj


def fetch_toronto_neighbourhood_crime():
    """TPS Neighbourhood Crime Rates → simplified boundary GeoJSON + latest-year
    rate CSV (rate per 100,000), one row per neighbourhood, a column per crime type."""
    logger.info("Fetching Toronto Police neighbourhood crime rates...")
    try:
        gj = _arcgis_query("Neighbourhood_Crime_Rates_Open_Data", 0,
                           {"where": "1=1", "outFields": "*",
                            "outSR": "4326", "f": "geojson"})
    except Exception as e:
        logger.error(f"  failed to fetch neighbourhood crime rates: {e}")
        return None
    feats = gj.get("features", [])
    if not feats:
        return None

    # Latest year present with populated rate data (try newest first).
    sample = feats[0]["properties"]
    years = sorted({int(k.split("_")[-1]) for k in sample
                    if k.startswith("AUTOTHEFT_RATE_")}, reverse=True)
    year = next((y for y in years
                 if sum(1 for f in feats
                        if f["properties"].get(f"AUTOTHEFT_RATE_{y}") is not None) > 100),
                years[0])

    import geopandas as gpd
    gdf = gpd.GeoDataFrame.from_features(feats, crs="EPSG:4326")
    gdf["geometry"] = gdf.geometry.simplify(0.0003, preserve_topology=True)

    rows, out_features = [], []
    for geom, props in zip(gdf.geometry, (f["properties"] for f in feats)):
        hood_id = str(int(props["HOOD_ID"]))
        row = {"hood_id": hood_id,
               "name": props.get("AREA_NAME"),
               "population": props.get("POPULATION_2025")}
        for stem, (slug, _) in NCR_STEMS.items():
            row[slug] = props.get(f"{stem}_RATE_{year}")
        rows.append(row)
        out_features.append({
            "type": "Feature", "id": hood_id,
            "properties": {"name": props.get("AREA_NAME")},
            "geometry": _round_coords(json.loads(
                gpd.GeoSeries([geom]).to_json())["features"][0]["geometry"])})

    import pandas as pd
    df = pd.DataFrame(rows)
    geo_path = DATA_DIR / "geo" / "toronto_neighbourhoods.geojson"
    csv_path = DATA_DIR / "geo" / "toronto_neighbourhood_crime.csv"
    geo_path.parent.mkdir(parents=True, exist_ok=True)
    with open(geo_path, "w") as f:
        json.dump({"type": "FeatureCollection", "features": out_features},
                  f, separators=(",", ":"))
    df["year"] = year
    df.to_csv(csv_path, index=False)
    _save_meta(csv_path, df, year,
               "Toronto Police Service Neighbourhood Crime Rates (Open Data)",
               "rate per 100,000 population",
               ["per-neighbourhood rate per 100k for 9 crime types, latest year; "
                "boundary simplified to ~30 m and inlined (id = HOOD_158)."])
    logger.info(f"  saved {len(df)} neighbourhoods ({year}); "
                f"geojson {geo_path.stat().st_size/1e6:.2f} MB")
    return df


def fetch_toronto_bike_thefts():
    """Bicycle Thefts point set, server-side aggregated → month-of-year seasonality,
    recovery status, and yearly count. Avoids paging ~40k points."""
    logger.info("Fetching Toronto Police bicycle thefts (aggregated)...")
    import pandas as pd

    def group_count(field):
        stat = ('[{"statisticType":"count","onStatisticField":"OBJECTID",'
                '"outStatisticFieldName":"n"}]')
        try:
            js = _arcgis_query("Bicycle_Thefts_Open_Data", 0,
                               {"where": "1=1", "groupByFieldsForStatistics": field,
                                "outStatistics": stat, "f": "json"})
        except Exception as e:
            logger.error(f"  bike-theft groupBy {field} failed: {e}")
            return None
        return [(a["attributes"].get(field), a["attributes"]["n"])
                for a in js.get("features", [])]

    months = group_count("OCC_MONTH")
    status = group_count("STATUS")
    years = group_count("OCC_YEAR")
    if not (months and status and years):
        return None

    frames = []
    MONTH_ORDER = ["January", "February", "March", "April", "May", "June", "July",
                   "August", "September", "October", "November", "December"]
    for view, data in (("month", months), ("status", status), ("year", years)):
        for label, n in data:
            frames.append({"view": view, "label": label, "count": n})
    df = pd.DataFrame(frames).dropna(subset=["label"])
    # Order months chronologically where present.
    df["order"] = df.apply(
        lambda r: MONTH_ORDER.index(r["label"]) if r["view"] == "month"
        and r["label"] in MONTH_ORDER else 0, axis=1)

    out = DATA_DIR / "geo" / "toronto_bike_thefts.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    total = int(df[df["view"] == "status"]["count"].sum())
    recovered = int(df[(df["view"] == "status") &
                       (df["label"].astype(str).str.upper() == "RECOVERED")]["count"].sum())
    _save_meta(out, df, None,
               "Toronto Police Service Bicycle Thefts (Open Data)", "incident counts",
               [f"server-side counts by month-of-year, recovery status, and year; "
                f"{recovered:,}/{total:,} recovered ({recovered/total*100:.1f}%)."])
    logger.info(f"  saved bike-theft summary; recovery {recovered/total*100:.1f}%")
    return df


SEASONALITY_CRIMES = [
    ("Auto_Theft_Open_Data", "Auto theft"),
    ("Theft_From_Motor_Vehicle_Open_Data", "Theft from vehicle"),
    ("Break_and_Enter_Open_Data", "Break & enter"),
    ("Assault_Open_Data", "Assault"),
    ("Robbery_Open_Data", "Robbery"),
    ("Bicycle_Thefts_Open_Data", "Bike theft"),
]


def fetch_toronto_seasonality():
    """Month-of-year incident counts for several Toronto crime types (server-side
    groupBy OCC_MONTH, occurrences 2014–) → data/geo/toronto_crime_seasonality.csv
    (crime, month, count). Lets a normalized chart compare WHICH crimes are seasonal —
    bike theft and theft-from-vehicle peak hard in summer; others are much flatter."""
    logger.info("Fetching Toronto Police crime seasonality (by month)...")
    import pandas as pd
    MONTHS = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    stat = ('[{"statisticType":"count","onStatisticField":"OBJECTID",'
            '"outStatisticFieldName":"n"}]')
    rows = []
    for svc, label in SEASONALITY_CRIMES:
        try:
            js = _arcgis_query(svc, 0, {"where": "OCC_YEAR>=2014",
                "groupByFieldsForStatistics": "OCC_MONTH", "outStatistics": stat, "f": "json"})
        except Exception as e:
            logger.error(f"  seasonality {label} failed: {e}")
            continue
        for a in js.get("features", []):
            mo, n = a["attributes"].get("OCC_MONTH"), a["attributes"].get("n")
            num = (MONTHS.index(mo) + 1 if mo in MONTHS
                   else int(mo) if str(mo).isdigit() and 1 <= int(mo) <= 12 else None)
            if num and n is not None:
                rows.append({"crime": label, "month": num, "count": int(n)})
    if not rows:
        return None
    df = pd.DataFrame(rows).sort_values(["crime", "month"]).reset_index(drop=True)
    out = DATA_DIR / "geo" / "toronto_crime_seasonality.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    _save_meta(out, df, None, "Toronto Police Service (multiple open datasets)",
               "incident counts by month of year",
               ["server-side count by OCC_MONTH, occurrences 2014–, for 6 crime types"])
    logger.info(f"  saved seasonality: {df['crime'].nunique()} crimes × ~12 months")
    return df


def _save_meta(path, df, year, source_table, unit, transforms):
    from pipeline.metadata import save_metadata
    save_metadata(path, df=df,
                  latest_observation_date=str(year) if year else None,
                  source="Toronto Police Service", source_table=source_table,
                  frequency="annual", unit=unit, transformations=transforms,
                  notes=ATTRIB)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    fetch_toronto_neighbourhood_crime()
    fetch_toronto_bike_thefts()
    fetch_toronto_seasonality()
