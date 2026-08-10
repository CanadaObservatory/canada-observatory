#!/usr/bin/env python3
"""ONE-TIME heavy pull (NOT weekly): ERA5-Land daily-MAXIMUM 2 m temperature → the 1991–2020
monthly **average daily high** normal, for the temperature-maps page (the max-temp layers — the
mean hides daytime extremes that drive heat impact on health and environment).

Streams **per year** and is **resumable**: each year is pulled, reduced to its 12 monthly means of
daily-max (cached as /tmp/_high_<year>.npz), and the ~195 MB raw file is deleted — so peak disk stays
~one year, and a re-run skips years already reduced. After 30 years it averages them →
**/tmp/era5land_monthly_high_normal.npz** (12 monthly fields, °C, + lat/lon), which build_era5_climate
reads to render the average-daily-high maps. ~30 CDS requests, slow (needs ~/.cdsapirc).
    python -m pipeline.pull_era5_dailymax
"""
import logging
import os

import numpy as np
import xarray as xr

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

AREA = [84, -141, 41, -52]                 # Canada bbox N,W,S,E
YEARS = range(1991, 2021)                  # the WMO 1991–2020 normal
OUT = "/tmp/era5land_monthly_high_normal.npz"


def _reduce_year(year):
    """Pull one year of daily-max, cache its 12 monthly means (Kelvin) to /tmp/_high_<year>.npz,
    delete the raw file. No-op if the year is already reduced (resumable)."""
    cache = f"/tmp/_high_{year}.npz"
    if os.path.exists(cache):
        return
    raw = f"/tmp/_dmax_{year}.nc"
    if not os.path.exists(raw):
        import cdsapi
        log.info(f"pulling daily-max {year}…")
        cdsapi.Client().retrieve("derived-era5-land-daily-statistics", {
            "variable": ["2m_temperature"], "year": str(year),
            "month": [f"{m:02d}" for m in range(1, 13)],
            "day": [f"{d:02d}" for d in range(1, 32)],
            "daily_statistic": "daily_maximum", "time_zone": "utc+00:00",
            "frequency": "1_hourly", "area": AREA,
            "data_format": "netcdf", "download_format": "unarchived"}, raw)
    ds = xr.open_dataset(raw)
    months = ds["valid_time"].dt.month.values
    t2m = ds["t2m"].values.astype("float32")            # Kelvin; (days, lat, lon)
    highs = {f"m{m:02d}": np.nanmean(t2m[months == m], axis=0) for m in range(1, 13)}
    np.savez(cache, lat=ds.latitude.values, lon=ds.longitude.values, **highs)
    ds.close()
    os.remove(raw)
    log.info(f"reduced {year} -> {cache}")


def main():
    if os.path.exists(OUT):
        log.info(f"{OUT} already exists — nothing to do")
        return
    for y in YEARS:
        _reduce_year(y)
    acc, lat, lon = None, None, None
    for y in YEARS:
        z = np.load(f"/tmp/_high_{y}.npz")
        lat, lon = z["lat"], z["lon"]
        if acc is None:
            acc = {k: np.zeros_like(z[k]) for k in z.files if k.startswith("m")}
        for k in acc:
            acc[k] += z[k]
    normal = {k: (acc[k] / len(YEARS) - 273.15).astype("float32") for k in acc}   # → °C
    np.savez(OUT, lat=lat, lon=lon, **normal)
    log.info(f"wrote {OUT}: 12 monthly average-daily-high fields (1991–2020 normal)")


if __name__ == "__main__":
    main()
