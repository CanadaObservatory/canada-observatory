#!/usr/bin/env python3
"""ANNUAL/periodic manual builder (NOT in the weekly registry): ERA5-Land temperature maps.

Pulls ERA5-Land monthly-mean 2 m temperature for Canada from the Copernicus Climate Data
Store (needs ~/.cdsapirc), then builds, for the Climate Change page (environment/climate-change.qmd):

  * **Current mean temperature** — the 1991–2020 climate normal (annual + the 4 seasons,
    kept in the manifest for a season toggle).
  * **Warming** — the change in annual-mean temperature, recent (2016–2025, the latest ten
    full years) minus the mid-century baseline (1951–1980).

Each field is reprojected to **Web-Mercator (EPSG:3857)** and rendered to a transparent WebP
positioned by WGS84 corner coordinates, for the `charts.relief_map` map overlay. A small
PNG preview (lat-lon + province outlines + colourbar) is written alongside for review.

ERA5-Land monthly means lag only ~1 month. Re-run after each refresh and **bump
LATEST_YEAR/LATEST_MONTH** to the newest available month:
    python -m pipeline.build_era5_climate
Heavy CDS pull (75 yr × 12 mo) → not weekly. Deps: cdsapi, xarray, rasterio, matplotlib, Pillow.
Attribution required on every map: "Generated using Copernicus Climate Change Service information".
"""
import calendar
import json
import logging
from pathlib import Path

import numpy as np
import xarray as xr
import matplotlib
matplotlib.use("Agg")
import matplotlib.colors as mcolors
import matplotlib.pyplot as plt
from PIL import Image
import rasterio
from rasterio.transform import from_origin, array_bounds
from rasterio.warp import reproject, Resampling, transform_bounds, calculate_default_transform, transform_geom
from rasterio.features import rasterize
from rasterio.fill import fillnodata

from pipeline.config import DATA_DIR

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NC = "/tmp/era5land_monthly_canada.nc"           # cached raw pull (gitignored /tmp)
OUT = DATA_DIR / "geo"                            # committed assets live here
AREA = [84, -141, 41, -52]                        # Canada bbox N,W,S,E
LATEST_YEAR, LATEST_MONTH = 2026, 5               # newest available ERA5-Land month — BUMP each refresh
NORMAL = range(1991, 2021)                        # WMO current normal (fixed)
RECENT, BASE = range(2016, 2026), range(1961, 1991)   # warming: latest 10 full years vs the 1961–1990 reference (matches the page baseline)
HOVER_STEP = 4                                    # hover-point grid downsample (~0.4° ≈ 43 km on visible land)
MONTHS = [(f"{m:02d}", calendar.month_name[m]) for m in range(1, 13)]   # 12 calendar months → monthly normals


def pull():
    """Download the monthly-means netCDF (idempotent; skip if cached).

    Two requests because the current year is partial: full years 1950..LATEST_YEAR-1 (all
    months) + LATEST_YEAR months 1..LATEST_MONTH."""
    if Path(NC).exists():
        logger.info(f"using cached {NC}")
        return
    import cdsapi
    c = cdsapi.Client()
    base = {"product_type": ["monthly_averaged_reanalysis"], "variable": ["2m_temperature"],
            "time": ["00:00"], "area": AREA, "data_format": "netcdf", "download_format": "unarchived"}
    logger.info(f"pulling ERA5-Land monthly means 1950–{LATEST_YEAR}-{LATEST_MONTH:02d}…")
    c.retrieve("reanalysis-era5-land-monthly-means", {**base,
        "year": [str(y) for y in range(1950, LATEST_YEAR)],
        "month": [f"{m:02d}" for m in range(1, 13)]}, "/tmp/_era5_full.nc")
    c.retrieve("reanalysis-era5-land-monthly-means", {**base,
        "year": [str(LATEST_YEAR)],
        "month": [f"{m:02d}" for m in range(1, LATEST_MONTH + 1)]}, "/tmp/_era5_cur.nc")
    xr.concat([xr.open_dataset("/tmp/_era5_full.nc").load(),
               xr.open_dataset("/tmp/_era5_cur.nc").load()], dim="valid_time").to_netcdf(NC)


def _mean_c(ds, years, months=None):
    """Mean 2 m temperature (°C) over the given years (and months), as a (lat, lon) array."""
    yr, mo = ds.valid_time.dt.year, ds.valid_time.dt.month
    mask = yr.isin(list(years))
    if months:
        mask = mask & mo.isin(months)
    sub = ds.sel(valid_time=ds.valid_time[mask])
    return sub["t2m"].mean("valid_time").values.astype("float32") - 273.15


def _reproject_3857(field, lon, lat, F=6):
    """4326 (lat desc, lon asc) → 3857; returns (array, dst_transform, (W, S, E, N) corners).
    F = ×refine: bilinear-upsample the coarse 0.1° field to a smooth raster (lower F = lighter file)."""
    dx, dy = abs(lon[1] - lon[0]), abs(lat[1] - lat[0])
    west, north, south, east = lon.min() - dx/2, lat.max() + dy/2, lat.min() - dy/2, lon.max() + dx/2
    src_t = from_origin(west, north, dx, dy)
    h, w = field.shape
    dst_t, dw, dh = calculate_default_transform("EPSG:4326", "EPSG:3857", w, h, west, south, east, north)
    dst_t = rasterio.Affine(dst_t.a / F, dst_t.b, dst_t.c, dst_t.d, dst_t.e / F, dst_t.f)
    dw, dh = dw * F, dh * F
    dst = np.full((dh, dw), np.nan, "float32")
    # Fill the source NaN (land-sea gaps + the strip between the 0.1° land cells and the true coast)
    # BEFORE upsampling, so every cell of the detailed land mask gets a smooth value — the ocean
    # extrapolation is hidden by the land-minus-lakes clip. Done on the small source grid → cheap.
    src = fillnodata(field.copy(), mask=np.isfinite(field).astype("uint8"), max_search_distance=400.0)
    reproject(src, dst, src_transform=src_t, src_crs="EPSG:4326", dst_transform=dst_t,
              dst_crs="EPSG:3857", resampling=Resampling.bilinear, dst_nodata=np.nan)
    W, S, E, N = transform_bounds("EPSG:3857", "EPSG:4326", *array_bounds(dh, dw, dst_t))
    return dst, dst_t, (W, S, E, N)


def _rasterize(gj, shape, dst_t, all_touched):
    geoms = [transform_geom("EPSG:4326", "EPSG:3857", f["geometry"]) for f in gj["features"]]
    return rasterize(geoms, out_shape=shape, transform=dst_t, fill=0, default_value=1,
                     dtype="uint8", all_touched=all_touched)


def _land_mask(land_gj, lakes_gj, shape, dst_t):
    """Land mask on the dst grid = detailed coastline polygons MINUS major lakes.

    Clipping the coloured field to this gives crisp real coastlines with islands separated (the
    generalized prov_2021 merged Vancouver Island / Newfoundland into the mainland), and renders the
    big lakes as transparent water — ERA5-Land carries lake-surface temperatures, so otherwise the
    lakes get coloured. `all_touched=True` for land keeps thin coastal cells + small islands;
    `False` for lakes keeps clean lake shores (no eroded ring)."""
    land = _rasterize(land_gj, shape, dst_t, all_touched=True)
    if lakes_gj:
        land = land & (1 - _rasterize(lakes_gj, shape, dst_t, all_touched=False))
    return land


def _webp(field3857, cmap, vmin, vmax, path, mask=None):
    f = field3857                                                   # already gap-free (filled pre-reproject)
    alpha = (mask.astype(bool) & np.isfinite(f)) if mask is not None else np.isfinite(f)
    rgba = matplotlib.colormaps[cmap](mcolors.Normalize(vmin, vmax)(np.ma.masked_invalid(f)))
    rgba[..., 3] = alpha.astype("float32")                          # transparent off-land / over lakes
    Image.fromarray((rgba * 255).astype("uint8")).save(path, "WEBP", quality=88, method=6)


def _draw_prov(ax, gj):
    for feat in gj.get("features", []):
        g = feat["geometry"]
        polys = g["coordinates"] if g["type"] == "MultiPolygon" else [g["coordinates"]]
        for poly in polys:
            for ring in poly:
                a = np.array(ring)
                ax.plot(a[:, 0], a[:, 1], color="#666", lw=0.4)


def _preview(field, lon, lat, cmap, vmin, vmax, title, path, prov=None, ticksuffix="°C"):
    fig, ax = plt.subplots(figsize=(8, 6.4), dpi=120)
    m = ax.pcolormesh(lon, lat, field, cmap=cmap, vmin=vmin, vmax=vmax, shading="auto")
    if prov:
        _draw_prov(ax, prov)
    ax.set_title(title, fontsize=12)
    ax.set_aspect(1 / np.cos(np.deg2rad(60)))
    ax.set_xlim(lon.min(), lon.max()); ax.set_ylim(lat.min(), lat.max())
    ax.set_xticks([]); ax.set_yticks([])
    fig.colorbar(m, ax=ax, shrink=0.8, label=ticksuffix)
    fig.tight_layout(); fig.savefig(path, bbox_inches="tight"); plt.close(fig)


def _nice(v, step, fn):
    return fn(v / step) * step


def _corners(c):
    W, S, E, N = c
    return [[W, N], [E, N], [E, S], [W, S]]          # TL, TR, BR, BL (lon, lat)


def _source_landmask(land_gj, lakes_gj, lat, lon):
    """Land-minus-lakes mask on the SOURCE lat/lon grid — keeps hover points on visible land."""
    dx, dy = abs(lon[1] - lon[0]), abs(lat[1] - lat[0])
    t = from_origin(lon.min() - dx / 2, lat.max() + dy / 2, dx, dy)   # 4326, rows north→south
    shape = (len(lat), len(lon))
    land = rasterize([f["geometry"] for f in land_gj["features"]], out_shape=shape, transform=t,
                     fill=0, default_value=1, all_touched=False, dtype="uint8")
    if lakes_gj:
        land = land & (1 - rasterize([f["geometry"] for f in lakes_gj["features"]], out_shape=shape,
                                     transform=t, fill=0, default_value=1, all_touched=False, dtype="uint8"))
    return land


def _hover_grid(src_mask, lat, lon, field0, step):
    """Shared downsampled point grid (indices + lon/lat) on visible land where field0 is finite.
    Every layer samples the SAME points, so the sidecar stores lon/lat ONCE + per-layer values —
    which keeps a dense grid (and many seasonal layers) affordable."""
    idx, plon, plat = [], [], []
    for j in range(0, len(lat), step):
        for i in range(0, len(lon), step):
            if src_mask[j, i] and np.isfinite(field0[j, i]):
                idx.append((j, i))
                plon.append(round(float(lon[i]), 3))
                plat.append(round(float(lat[j]), 3))
    return idx, plon, plat


def _sample(field, idx):
    """Values at the shared grid points (°C, 1 dp)."""
    return [round(float(field[j, i]), 1) for j, i in idx]


def _recent_anom(ds, means, n):
    """Mean of the last n months (actual) minus the 1991–2020 normal for those calendar months —
    answers 'how did the latest period compare to normal?'. Returns (anomaly field, the n timestamps)."""
    last = list(ds.valid_time.values)[-n:]
    actual = ds["t2m"].sel(valid_time=last).mean("valid_time").values.astype("float32") - 273.15
    cal = sorted({int(str(t)[5:7]) for t in last})
    normal = np.nanmean(np.stack([means[f"{m:02d}"] for m in cal]), axis=0)
    return (actual - normal).astype("float32"), last


def build():
    pull()
    OUT.mkdir(parents=True, exist_ok=True)
    ds = xr.open_dataset(NC).squeeze()
    lon = np.where(ds.longitude.values > 180, ds.longitude.values - 360, ds.longitude.values)
    lat = ds.latitude.values
    latest = str(ds.valid_time.values.max())[:7]
    prov_path = OUT / "canada_provinces.geojson"        # detailed boundary: islands separated (BC=23 parts, NL=9, NU=91)
    prov = json.load(open(prov_path)) if prov_path.exists() else None
    lakes_path = OUT / "lakes_major.geojson"            # subtract the big lakes so they read as water
    lakes = json.load(open(lakes_path)) if lakes_path.exists() else None
    src_mask = _source_landmask(prov, lakes, lat, lon) if prov is not None else None
    points = {}                                         # layer key -> {lon,lat,val} for the hover overlay
    manifest = {"layers": [], "latest_month": latest,
                "source": "ECMWF Copernicus Climate Change Service — ERA5-Land",
                "attribution": "Generated using Copernicus Climate Change Service information"}

    # ---- monthly normals (1991–2020); all 12 months share ONE scale so flipping through the
    #      selector shows the seasonal swing as colour (and the legend stays put). F=4 keeps 12
    #      images affordable; the warming hero map below stays F=6. ----
    means = {mk: _mean_c(ds, NORMAL, [int(mk)]) for mk, _ in MONTHS}
    allvals = np.concatenate([f.ravel() for f in means.values()])
    lo = _nice(np.nanpercentile(allvals, 1), 5, np.floor)
    hi = _nice(np.nanpercentile(allvals, 99), 5, np.ceil)
    logger.info(f"monthly-temp scale: {lo}..{hi} °C")
    if src_mask is not None:
        pidx, plon, plat = _hover_grid(src_mask, lat, lon, means["07"], HOVER_STEP)   # July = full land coverage
        points = {"lon": plon, "lat": plat, "vals": {}}
    mask = None
    for mk, mlabel in MONTHS:
        f = means[mk]
        f3857, dst_t, corners = _reproject_3857(f, lon, lat, F=4)
        if mask is None and prov:                        # same F=4 grid for every month → rasterize once
            mask = _land_mask(prov, lakes, f3857.shape, dst_t)
        _webp(f3857, "RdYlBu_r", lo, hi, OUT / f"era5_month_{mk}.webp", mask)
        if src_mask is not None:
            points["vals"][f"month_{mk}"] = _sample(f, pidx)
        manifest["layers"].append(dict(key=f"month_{mk}", kind="month", month=int(mk), label=mlabel,
            webp=f"era5_month_{mk}.webp", corners=_corners(corners), vmin=lo, vmax=hi, cmap="RdYlBu_r", units="°C"))

    # ---- warming: latest 10 full years minus mid-century, annual (hero map, F=6) ----
    anom = _mean_c(ds, RECENT) - _mean_c(ds, BASE)
    amax = max(1.0, _nice(np.nanpercentile(np.abs(anom), 98), 0.5, np.ceil))
    logger.info(f"warming anomaly scale: ±{amax} °C; Canada-land mean Δ = {np.nanmean(anom):.2f} °C")
    a3857, dst_t, corners = _reproject_3857(anom, lon, lat)          # F=6
    mask_w = _land_mask(prov, lakes, a3857.shape, dst_t) if prov is not None else None
    _webp(a3857, "RdBu_r", -amax, amax, OUT / "era5_warming_annual.webp", mask_w)
    if src_mask is not None:
        points["vals"]["warming_annual"] = _sample(anom, pidx)
    manifest["layers"].append(dict(key="warming_annual", kind="anomaly", season="annual",
        label="Warming — annual mean, 2016–2025 vs 1961–1990", webp="era5_warming_annual.webp",
        corners=_corners(corners), vmin=-amax, vmax=amax, cmap="RdBu_r", units="°C"))
    _preview(anom, lon, lat, "RdBu_r", -amax, amax,
             "Warming — annual mean, 2016–2025 vs 1961–1990 (ERA5-Land)", OUT / "_preview_warming.png", prov)

    # ---- recent anomalies: how the latest 1 / 3 / 12 months compare to the 1991–2020 normal ----
    def _span(ts):
        f = lambda t: f"{calendar.month_abbr[int(str(t)[5:7])]} {str(t)[:4]}"
        a, b = f(ts[0]), f(ts[-1])
        return a if a == b else f"{a} – {b}"
    rec = [(key, lab) + _recent_anom(ds, means, n)
           for n, key, lab in [(1, "recent_1", "Past month"), (3, "recent_3", "Past 3 months"),
                               (12, "recent_12", "Past 12 months")]]
    ramax = max(2.0, _nice(np.nanpercentile(np.abs(rec[0][2]), 98), 1.0, np.ceil))
    logger.info(f"recent-anomaly scale: ±{ramax} °C; latest month = {_span(rec[0][3])}")
    for key, lab, af, last in rec:
        r3857, dst_t, corners = _reproject_3857(af, lon, lat, F=4)
        _webp(r3857, "RdBu_r", -ramax, ramax, OUT / f"era5_{key}.webp", mask)
        if src_mask is not None:
            points["vals"][key] = _sample(af, pidx)
        manifest["layers"].append(dict(key=key, kind="recent", label=lab, span=_span(last),
            webp=f"era5_{key}.webp", corners=_corners(corners), vmin=-ramax, vmax=ramax,
            cmap="RdBu_r", units="°C"))

    # ---- recent-anomaly strip: Canada-land mean monthly anomaly, last 24 months ----
    if src_mask is not None:
        mbool = src_mask.astype(bool)
        with open(OUT / "era5_recent_anomaly.csv", "w") as fh:
            fh.write("month,anomaly\n")
            for t in list(ds.valid_time.values)[-24:]:
                af = ds["t2m"].sel(valid_time=t).values.astype("float32") - 273.15 - means[f"{int(str(t)[5:7]):02d}"]
                fh.write(f"{str(t)[:7]},{round(float(np.nanmean(af[mbool])), 2)}\n")

    json.dump(manifest, open(OUT / "era5_climate_manifest.json", "w"), indent=0)
    json.dump(points, open(OUT / "era5_climate_points.json", "w"))
    npts = len(points.get("lon", []))
    logger.info(f"wrote {len(manifest['layers'])} layers + manifest + {npts}-point hover grid (data through {latest}) -> {OUT}")


if __name__ == "__main__":
    build()
