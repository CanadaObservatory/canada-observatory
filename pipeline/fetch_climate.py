"""
Climate-change / temperature custom fetchers (the Environment > Climate Change
page). All Open Government Licence – Canada, from Environment and Climate Change
Canada (ECCC).

1. **CESI national temperature departure** — the headline "Canada is warming"
   series: annual average temperature departure from the 1961–1990 reference,
   1948–. Clean year-stamped CSV (same idiom as the CESI GHG/air-quality files);
   bump CESI_TEMP_RELEASE each year.

2. **CTVB regional trends** — the Climate Trends and Variations Bulletin's
   per-region warming trend (1948–latest) for Canada's 11 climate regions. This
   is the Arctic-amplification story in data: the northern regions warm ~2× the
   southern ones. Year-stamped URL (bump CTVB_YEAR each spring); cp1252-encoded.

3. **Long-run city temperatures (two-trace)** — for a set of cities, the
   *homogenized* AHCCD annual-mean temperature (the scientifically correct trend
   record, back to the 1840s for Toronto) PLUS a separate *raw* recent tail
   computed from current daily observations. Both come from ECCC's GeoMet OGC
   API. The two are kept as DISTINCT series and never spliced — the homogenized
   record ends 2020 (the open AHCCD feed's frontier) and the raw tail carries it
   to the present; the small offset where they meet is real and left visible.
   The Arctic stations (Alert, Eureka) begin only in 1948–51 — the first High
   Arctic weather stations (the 1947–50 Joint Arctic Weather Stations program) —
   so they are ~75-year records, not ~180 like Toronto.
"""

import io
import requests
import pandas as pd
from datetime import date
from pipeline.config import DATA_DIR
from pipeline.metadata import save_metadata
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_UA = {"User-Agent": "Mozilla/5.0 (CanObs pipeline; +https://canadaobservatory.ca)"}
_GEOMET = "https://api.weather.gc.ca/collections"


def _get(url, **kw):
    r = requests.get(url, headers=_UA, timeout=kw.pop("timeout", 90), **kw)
    r.raise_for_status()
    return r


# ---------------------------------------------------------------------------
# 1. CESI national temperature departure (1948–, vs 1961–1990)
# ---------------------------------------------------------------------------
CESI_TEMP_RELEASE = "2025"   # CESI release year; bump when /<year>/ advances
CESI_TEMP_URL = ("https://www.canada.ca/content/dam/eccc/documents/csv/cesindicators/"
                 f"temperature-change/{CESI_TEMP_RELEASE}/Temperature-change-annual-en.csv")


def fetch_cesi_temperature():
    """National annual average temperature departure from the 1961–1990 reference
    (°C), 1948–. Output: year, departure_c."""
    logger.info("Fetching ECCC CESI national temperature departure...")
    try:
        r = _get(CESI_TEMP_URL, timeout=60)
    except Exception as e:
        logger.error(f"  CESI temperature fetch failed: {e}")
        return None
    df = pd.read_csv(io.StringIO(r.content.decode("latin-1")), skiprows=2)
    df = df.rename(columns={df.columns[0]: "year", df.columns[1]: "departure_c"})
    df["year"] = pd.to_numeric(df["year"], errors="coerce")
    df["departure_c"] = pd.to_numeric(df["departure_c"], errors="coerce")
    df = df.dropna(subset=["year", "departure_c"])
    df["year"] = df["year"].astype(int)
    df = df[["year", "departure_c"]].sort_values("year").reset_index(drop=True)
    if df.empty:
        return None
    out = DATA_DIR / "environment" / "cesi_temperature.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    save_metadata(out, df=df, latest_observation_date=str(int(df["year"].max())),
        source="Environment and Climate Change Canada",
        source_table="ECCC Canadian Environmental Sustainability Indicators — temperature change",
        frequency="annual", unit="°C departure from the 1961–1990 average",
        transformations=["national annual temperature departure vs 1961–1990 reference"])
    logger.info(f"  saved {len(df)} rows -> {out.name}")
    return df


# ---------------------------------------------------------------------------
# 2. CTVB regional warming trends (Arctic amplification)
# ---------------------------------------------------------------------------
CTVB_YEAR = "2024"   # Climate Trends & Variations Bulletin annual edition; bump each spring
CTVB_URL = ("https://www.canada.ca/content/dam/eccc/documents/csv/climate-trends-variations/"
            f"annual{CTVB_YEAR}/Annual_{CTVB_YEAR}_summary_temp_table_e.csv")


def fetch_ctvb_regional():
    """Per-region temperature trend (°C over 1948–latest) and the most recent
    year's departure, for Canada's climate regions + the national value, from the
    Climate Trends and Variations Bulletin summary table. Output: region, trend_c,
    departure_latest."""
    logger.info("Fetching ECCC CTVB regional temperature trends...")
    try:
        r = _get(CTVB_URL, timeout=60)
    except Exception as e:
        logger.error(f"  CTVB regional fetch failed: {e}")
        return None
    # Multi-row header: title, blank, an "Extreme years" super-header, then the
    # real header (skiprows=3). cp1252-encoded (the °C degree sign). Columns by
    # position: 0=Region, 1=Trend(°C), 6=Rank, 7=latest-year departure.
    raw = pd.read_csv(io.StringIO(r.content.decode("cp1252")), skiprows=3, header=0)
    rows = []
    for _, row in raw.iterrows():
        region = str(row.iloc[0]).strip()
        trend = pd.to_numeric(row.iloc[1], errors="coerce")
        if not region or region.startswith("*") or pd.isna(trend):
            continue
        dep = pd.to_numeric(row.iloc[-1], errors="coerce")
        rows.append({"region": region, "trend_c": float(trend),
                     "departure_latest": (float(dep) if pd.notna(dep) else None)})
    out_df = pd.DataFrame(rows)
    if out_df.empty:
        return None
    out = DATA_DIR / "environment" / "cesi_ctvb_regional.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out, index=False)
    save_metadata(out, df=out_df, latest_observation_date=CTVB_YEAR,
        source="Environment and Climate Change Canada",
        source_table=f"ECCC Climate Trends and Variations Bulletin (Annual {CTVB_YEAR})",
        frequency="annual", unit="°C trend over 1948–latest (vs 1961–1990 reference)",
        transformations=["per-region warming trend + latest-year departure"])
    logger.info(f"  saved {len(out_df)} rows -> {out.name}")
    return out_df


# ---------------------------------------------------------------------------
# 3. Long-run city temperatures: homogenized AHCCD spine + raw recent tail
# ---------------------------------------------------------------------------
# AHCCD composite climate-ID per city. The SAME id is a currently-active station
# in the daily archive (climate-monthly was discontinued ~2018, so the raw tail
# uses climate-daily). Arctic stations begin 1948–51 (JAWS program).
CITY_STATIONS = {
    "Toronto":   "6158355",   # 1841–2020 (the long composite)
    "Vancouver": "1108380",   # 1897–2020
    "Winnipeg":  "502S001",   # 1873–2020
    "Calgary":   "3031094",   # 1885–2020
    "Alert":     "2400305",   # 1951–2020 (High Arctic)
    "Eureka":    "2401199",   # 1948–2020 (High Arctic)
}
_AHCCD_MISSING = -9999.9      # AHCCD no-data sentinel
_RAW_TAIL_START = 2018        # raw tail begins here (overlaps AHCCD's 2020 end)
_MIN_DAYS = 350               # min valid days for a raw annual mean
_SEASON_MAP = {"Win": "Winter", "Spr": "Spring", "Smr": "Summer", "Fal": "Autumn"}
_MONTH_MAP = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
              "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}


def fetch_city_temperatures():
    """Per-city annual mean temperature (°C): the homogenized AHCCD record
    (series='Homogenized (AHCCD)', to 2020) plus a raw recent tail aggregated
    from daily observations (series='Recent (raw)'). Output tidy long:
    city, year, temp, series."""
    logger.info("Fetching long-run city temperatures (AHCCD + raw tail)...")
    end_year = date.today().year
    rows = []
    for city, sid in CITY_STATIONS.items():
        # --- homogenized AHCCD annual mean ---
        try:
            url = (f"{_GEOMET}/ahccd-annual/items?station_id__id_station={sid}"
                   "&f=json&limit=500&properties=year__annee,temp_mean__temp_moyenne")
            feats = _get(url).json().get("features", [])
            for f in feats:
                p = f["properties"]
                y, t = p.get("year__annee"), p.get("temp_mean__temp_moyenne")
                if y is not None and t is not None and t > _AHCCD_MISSING + 1:
                    rows.append({"city": city, "year": int(y), "temp": round(float(t), 2),
                                 "series": "Homogenized (AHCCD)"})
        except Exception as e:
            logger.warning(f"  AHCCD fetch failed for {city}: {e}")
        # --- raw recent tail from daily observations ---
        try:
            url = (f"{_GEOMET}/climate-daily/items?CLIMATE_IDENTIFIER={sid}"
                   f"&datetime={_RAW_TAIL_START}-01-01%2000:00:00/{end_year}-12-31%2000:00:00"
                   "&f=json&limit=10000&properties=LOCAL_YEAR,MEAN_TEMPERATURE")
            feats = _get(url, timeout=120).json().get("features", [])
            by_year = {}
            for f in feats:
                p = f["properties"]
                t = p.get("MEAN_TEMPERATURE")
                if t is not None:
                    by_year.setdefault(p.get("LOCAL_YEAR"), []).append(float(t))
            for y, temps in by_year.items():
                if y is not None and len(temps) >= _MIN_DAYS:
                    rows.append({"city": city, "year": int(y),
                                 "temp": round(sum(temps) / len(temps), 2),
                                 "series": "Recent (raw)"})
        except Exception as e:
            logger.warning(f"  raw daily fetch failed for {city}: {e}")

    out_df = pd.DataFrame(rows).sort_values(["city", "series", "year"]).reset_index(drop=True)
    if out_df.empty:
        return None
    out = DATA_DIR / "environment" / "city_temperatures.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out, index=False)
    save_metadata(out, df=out_df, latest_observation_date=str(int(out_df["year"].max())),
        source="Environment and Climate Change Canada",
        source_table="ECCC AHCCD (homogenized) + MSC daily observations (raw), via GeoMet",
        frequency="annual", unit="°C annual mean temperature",
        transformations=["homogenized AHCCD annual mean (to 2020) + raw daily→annual tail",
                         "tidy long: city, year, temp, series (homogenized vs raw)"])
    logger.info(f"  saved {len(out_df)} rows ({out_df.city.nunique()} cities) -> {out.name}")
    return out_df


def fetch_city_temperatures_seasonal():
    """Per-city *seasonal* homogenized temperature (°C), from ECCC's AHCCD
    seasonal collection — the data behind the 'which season is warming fastest'
    view (winter warms fastest). Homogenized record only (to 2020). Output tidy
    long: city, season, year, temp (seasonal mean), temp_max (seasonal mean of
    daily-high temperatures)."""
    logger.info("Fetching seasonal city temperatures (AHCCD seasonal)...")
    rows = []
    for city, sid in CITY_STATIONS.items():
        try:
            url = (f"{_GEOMET}/ahccd-seasonal/items?station_id__id_station={sid}"
                   "&f=json&limit=1200"
                   "&properties=year__annee,period_value__valeur_periode,"
                   "temp_mean__temp_moyenne,temp_max__temp_max")
            feats = _get(url).json().get("features", [])
        except Exception as e:
            logger.warning(f"  seasonal fetch failed for {city}: {e}")
            continue
        for f in feats:
            p = f["properties"]
            season = _SEASON_MAP.get(p.get("period_value__valeur_periode"))
            y, t = p.get("year__annee"), p.get("temp_mean__temp_moyenne")
            tx = p.get("temp_max__temp_max")
            if season and y is not None and t is not None and t > _AHCCD_MISSING + 1:
                rows.append({"city": city, "season": season, "year": int(y),
                             "temp": round(float(t), 2),
                             "temp_max": (round(float(tx), 2)
                                          if tx is not None and tx > _AHCCD_MISSING + 1
                                          else None)})
    out_df = pd.DataFrame(rows).sort_values(["city", "season", "year"]).reset_index(drop=True)
    if out_df.empty:
        return None
    out = DATA_DIR / "environment" / "city_temperatures_seasonal.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out, index=False)
    save_metadata(out, df=out_df, latest_observation_date=str(int(out_df["year"].max())),
        source="Environment and Climate Change Canada",
        source_table="ECCC AHCCD seasonal (homogenized), via GeoMet",
        frequency="annual", unit="°C seasonal mean / mean daily-high (homogenized)",
        transformations=["homogenized seasonal mean + mean daily-high per city/season/year (Winter/Spring/Summer/Autumn)"])
    logger.info(f"  saved {len(out_df)} rows ({out_df.city.nunique()} cities) -> {out.name}")
    return out_df


def fetch_city_temperatures_monthly():
    """Per-city homogenized MONTHLY mean temperature (AHCCD monthly) — the input
    for the temperature 'climate spiral' (months around the circle, years
    spiralling outward). Homogenized record only (to 2020). Output tidy long:
    city, year, month, temp."""
    logger.info("Fetching monthly city temperatures (AHCCD monthly)...")
    rows = []
    for city, sid in CITY_STATIONS.items():
        try:
            # NB: the `properties=` filter drops year__annee on ahccd-monthly
            # (an API quirk), so request full features and read year robustly.
            url = (f"{_GEOMET}/ahccd-monthly/items?station_id__id_station={sid}"
                   "&f=json&limit=2600")
            feats = _get(url).json().get("features", [])
        except Exception as e:
            logger.warning(f"  monthly fetch failed for {city}: {e}")
            continue
        for f in feats:
            p = f["properties"]
            m = _MONTH_MAP.get(p.get("period_value__valeur_periode"))
            y = p.get("year__annee")
            if y is None:   # fall back to the identifier "<station>.<year>.<month>"
                parts = str(p.get("identifier__identifiant", "")).split(".")
                y = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else None
            t = p.get("temp_mean__temp_moyenne")
            if m and y is not None and t is not None and t > _AHCCD_MISSING + 1:
                rows.append({"city": city, "year": int(y), "month": m,
                             "temp": round(float(t), 2)})
    out_df = pd.DataFrame(rows).sort_values(["city", "year", "month"]).reset_index(drop=True)
    if out_df.empty:
        return None
    out = DATA_DIR / "environment" / "city_temperatures_monthly.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out, index=False)
    save_metadata(out, df=out_df, latest_observation_date=str(int(out_df["year"].max())),
        source="Environment and Climate Change Canada",
        source_table="ECCC AHCCD monthly (homogenized), via GeoMet",
        frequency="monthly", unit="°C monthly mean temperature (homogenized)",
        transformations=["homogenized monthly mean per city/year/month — input for the climate spiral"])
    logger.info(f"  saved {len(out_df)} rows ({out_df.city.nunique()} cities) -> {out.name}")
    return out_df


if __name__ == "__main__":
    fetch_cesi_temperature()
    fetch_ctvb_regional()
    fetch_city_temperatures()
    fetch_city_temperatures_seasonal()
    fetch_city_temperatures_monthly()
