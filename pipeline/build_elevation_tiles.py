"""FALLBACK relief-tile builder (NOT used by the live site) — kept as insurance.

geography/elevation.qmd streams NRCan's live CBME hillshade (EPSG:3978). This builder is the
escape hatch if that service is ever retired or changes: it rebuilds an equivalent **self-hosted**
Web-Mercator relief from NRCan MRDEM-30 (EPSG:3979 COG, OGL) — reading per zoom level, reprojecting
to 3857, shading it (hypsometric tint * hillshade), and slicing the result into standard
{z}/{x}/{y}.webp XYZ tiles that Leaflet can serve as static files with no GIS server. One-time
build, not the weekly pipeline.

Defaults below build a small test region (SW BC -> AB Rockies, z6-10) so a smoke-test is fast.
For a national base, set REGION to Canada's bounds (about -141, 41.5, -52, 83.5) and ZMAX=8
(~6k tiles, ~30-55 MB as WebP) -- then point the Leaflet layer at the tiles and drop Proj4Leaflet.
The proven cost lever: ship WebP, not RGBA PNG (it is ~6% of the PNG size for this colour relief).
"""
import math
import os

import numpy as np
import rasterio
from matplotlib.colors import LinearSegmentedColormap
from PIL import Image
from rasterio.enums import Resampling
from rasterio.warp import reproject, transform_bounds
from rasterio.windows import from_bounds as win_from_bounds

MRDEM = "/vsicurl/https://canelevation-dem.s3.ca-central-1.amazonaws.com/mrdem-30/mrdem-30-dtm.vrt"
OUT = "geography/tiles_relief"
REGION = (-125.0, 49.0, -114.0, 54.0)   # lon/lat  W, S, E, N  (Coast Mtns -> Rockies)
ZMIN, ZMAX = 6, 10
R = 20037508.342789244                  # half-circumference in EPSG:3857 metres
HI_M = 3900.0                           # hypsometric high anchor (Mt Robson ~3954 m)

# muted hypsometric ramp: green lowland -> tan -> brown -> pale high ground
CMAP = LinearSegmentedColormap.from_list("relief", [
    (0.00, "#5d7d54"), (0.18, "#8aa06b"), (0.42, "#c2b280"),
    (0.65, "#a98c6b"), (0.85, "#d8cfc2"), (1.00, "#ffffff")])


def lon2x(lon, z):
    return int(math.floor((lon + 180.0) / 360.0 * 2 ** z))


def lat2y(lat, z):
    s = math.sin(math.radians(lat))
    return int(math.floor((0.5 - math.log((1 + s) / (1 - s)) / (4 * math.pi)) * 2 ** z))


def shade(elev, nd, ground_m):
    """hypsometric colour * hillshade -> RGBA uint8 (alpha 0 where no data)."""
    valid = np.isfinite(elev) & (elev != nd) & (elev > -100) & (elev < 7000)
    z = np.where(valid, elev, np.nan)
    gy, gx = np.gradient(z, ground_m, ground_m)
    slope = np.pi / 2 - np.arctan(np.hypot(gx, gy))
    aspect = np.arctan2(-gx, gy)
    az, alt = math.radians(360 - 315 + 90), math.radians(45)
    hs = np.sin(alt) * np.sin(slope) + np.cos(alt) * np.cos(slope) * np.cos(az - aspect)
    hs = np.where(np.isfinite(hs), np.clip(hs, 0, 1), 0.6)
    e = np.sqrt(np.clip(np.where(valid, elev, 0.0), 0, HI_M) / HI_M)
    rgb = CMAP(e)[..., :3] * (0.45 + 0.55 * hs)[..., None]
    rgba = np.dstack([np.clip(rgb, 0, 1), valid.astype(float)])
    return (rgba * 255).astype("uint8"), valid


def build_zoom(src, z, nd):
    w, s, e, n = REGION
    x0, x1 = lon2x(w, z), lon2x(e, z)
    y0, y1 = lat2y(n, z), lat2y(s, z)            # north -> small y
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    ts = 2 * R / 2 ** z
    left, right = -R + x0 * ts, -R + (x1 + 1) * ts
    top, bottom = R - y0 * ts, R - (y1 + 1) * ts
    W, H = nx * 256, ny * 256
    dt = rasterio.transform.from_bounds(left, bottom, right, top, W, H)

    sl, sb, sr, st_ = transform_bounds("EPSG:3857", "EPSG:3979", left, bottom, right, top)
    win = win_from_bounds(sl, sb, sr, st_, src.transform)
    arr = src.read(1, window=win, out_shape=(H, W), boundless=True,
                   fill_value=nd, resampling=Resampling.bilinear).astype("float32")
    swt = src.window_transform(win) * rasterio.Affine.scale(win.width / W, win.height / H)
    dst = np.full((H, W), nd, dtype="float32")
    reproject(arr, dst, src_transform=swt, src_crs="EPSG:3979",
              dst_transform=dt, dst_crs="EPSG:3857",
              src_nodata=nd, dst_nodata=nd, resampling=Resampling.bilinear)

    ground_m = (right - left) / W * math.cos(math.radians((s + n) / 2))
    img, valid = shade(dst, nd, ground_m)

    written, nbytes = 0, 0
    for ix in range(nx):
        for iy in range(ny):
            sub = valid[iy * 256:(iy + 1) * 256, ix * 256:(ix + 1) * 256]
            if not sub.any():
                continue                          # skip all-ocean / no-data tiles
            tile = img[iy * 256:(iy + 1) * 256, ix * 256:(ix + 1) * 256]
            d = f"{OUT}/{z}/{x0 + ix}"
            os.makedirs(d, exist_ok=True)
            p = f"{d}/{y0 + iy}.webp"
            Image.fromarray(tile, "RGBA").save(p, "WEBP", quality=82, method=6)
            written += 1
            nbytes += os.path.getsize(p)
    print(f"  z{z}: {nx}x{ny} grid -> {written} tiles, {nbytes/1024:.0f} KB")
    return written, nbytes


def main():
    os.environ.update(GDAL_DISABLE_READDIR_ON_OPEN="EMPTY_DIR", VSI_CACHE="TRUE",
                      GDAL_HTTP_MULTIRANGE="YES", GDAL_HTTP_MERGE_CONSECUTIVE_RANGES="YES")
    print(f"Building MRDEM hillshade tiles {REGION} z{ZMIN}-{ZMAX} -> {OUT}/")
    tot_t, tot_b = 0, 0
    with rasterio.open(MRDEM) as src:
        nd = src.nodata if src.nodata is not None else -32767.0
        for z in range(ZMIN, ZMAX + 1):
            t, b = build_zoom(src, z, nd)
            tot_t += t
            tot_b += b
    print(f"DONE: {tot_t} tiles, {tot_b/1024/1024:.1f} MB total "
          f"({tot_b/max(tot_t,1)/1024:.1f} KB/tile avg)")


if __name__ == "__main__":
    main()
