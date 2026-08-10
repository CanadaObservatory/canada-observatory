"""
Social-media card generator (static PNGs) for Canada Observatory.

Renders share-ready cards from the SAME committed data as the site, in the brand
identity: the Warm Off-White #F7F4EE field (the brand surface / og-card colour),
the cells maple-leaf mark, and the locked palette. v1 = the CPI / inflation
"latest release" card — Concept B (plot-only): the chart, a small upper-right
cells-leaf mark, and the latest value labelled above the final bar.

The card is authored as SVG (pixel-exact to the approved design) and rasterized
with qlmanage + sips — macOS QuickLook, the project's standard SVG->PNG recipe
(see visual_assets/brand/README.md). NOTE: that runs LOCALLY (macOS). CI
auto-regeneration off the release calendar would need a Linux SVG rasterizer
(resvg / cairosvg) or a Plotly+kaleido port; the SVG/layout here is the part to
keep either way.

Run:    python -m pipeline.social_cards
Output: social/<name>_<period>.png  (git-ignored — a derived artifact)
"""

import base64
import math
import os
import subprocess
import tempfile

import pandas as pd

from pipeline.config import PROJECT_ROOT

OUT_DIR = PROJECT_ROOT / "social"

# Canonical card sizes (px), 2026 best practice — full per-network notes + sources in
# the social-media-image-sizes reference memory. 4:5 portrait is the strongest
# general-purpose default (mobile-first; IG/FB/LinkedIn/X); 1:1 is universally safe;
# 16:9 suits X in-stream + slides; 1.91:1 is the link/share (OG) preview (== the site
# og:image). NOTE: build_cpi_card currently composes a 1:1 layout; portrait/landscape/og
# need a per-aspect layout pass (element reflow) — the next build step.
SOCIAL_FORMATS = {
    "portrait":  (1080, 1350),   # 4:5    — IG / FB / LinkedIn feed (best reach; default)
    "square":    (1080, 1080),   # 1:1    — universal (X, LinkedIn)
    "landscape": (1200, 675),    # 16:9   — X in-stream, slides
    "og":        (1200, 630),    # 1.91:1 — link / share preview (== site og:image)
    # "story":   (1080, 1920),   # 9:16   — full-screen vertical (future)
}

# --- brand palette (chrome, not data ink) ---
BG = "#F7F4EE"      # Warm Off-White — the brand surface
NAVY = "#17324D"
MAROON = "#7A263A"
RED = "#C0392B"      # valence: inflation above the BoC target band
LAKE = "#2A7F9E"     # valence: below the band
SLATE = "#6B7280"
INBAND = "#5E6B7A"   # within the 1-3% band
FONT = "'Radio Canada', 'Helvetica Neue', Helvetica, Arial, sans-serif"

# The botanical display leaf + the six cells wedges (from
# visual_assets/brand/botanical/leaf-botanical-cells.svg).
_LEAF = ("M 50.0 4.0 Q 50.0 4.0 50.4 5.1 L 51.9 9.2 Q 53.2 13.0 55.0 11.5 L 56.6 10.3 "
         "Q 57.5 9.5 57.6 10.7 L 57.9 15.0 Q 58.2 19.0 60.4 17.5 L 62.5 16.2 Q 63.5 15.5 "
         "63.3 16.7 L 61.7 26.1 Q 60.5 33.0 64.5 29.6 L 69.1 25.8 Q 70.0 25.0 70.3 26.1 "
         "L 70.9 27.9 Q 71.5 30.0 74.2 27.0 L 82.2 17.9 Q 83.0 17.0 83.1 18.2 L 83.6 22.2 "
         "Q 84.0 26.0 86.1 25.6 L 87.8 25.2 Q 89.0 25.0 88.4 26.0 L 83.2 34.1 Q 79.5 40.0 "
         "83.9 41.3 L 88.8 42.7 Q 90.0 43.0 89.3 44.0 L 86.8 47.6 Q 84.5 51.0 85.1 51.8 "
         "L 85.4 52.2 Q 86.0 53.0 84.9 53.3 L 73.7 56.7 Q 66.0 59.0 60.1 59.4 L 54.0 59.9 "
         "Q 52.0 60.0 51.7 61.3 L 51.4 62.2 Q 51.2 63.0 51.2 63.8 L 50.8 87.2 Q 50.8 88.0 "
         "50.1 88.0 L 49.9 88.0 Q 49.2 88.0 49.2 87.2 L 48.8 63.8 Q 48.8 63.0 48.6 62.2 "
         "L 48.3 61.3 Q 48.0 60.0 46.0 59.9 L 39.9 59.4 Q 34.0 59.0 26.3 56.7 L 15.1 53.3 "
         "Q 14.0 53.0 14.6 52.2 L 14.9 51.8 Q 15.5 51.0 13.2 47.6 L 10.7 44.0 Q 10.0 43.0 "
         "11.2 42.7 L 16.1 41.3 Q 20.5 40.0 16.8 34.1 L 11.6 26.0 Q 11.0 25.0 12.2 25.2 "
         "L 13.9 25.6 Q 16.0 26.0 16.4 22.2 L 16.9 18.2 Q 17.0 17.0 17.8 17.9 L 25.8 27.0 "
         "Q 28.5 30.0 29.1 27.9 L 29.7 26.1 Q 30.0 25.0 30.9 25.8 L 35.5 29.6 Q 39.5 33.0 "
         "38.3 26.1 L 36.7 16.7 Q 36.5 15.5 37.5 16.2 L 39.6 17.5 Q 41.8 19.0 42.1 15.0 "
         "L 42.4 10.7 Q 42.5 9.5 43.4 10.3 L 45.0 11.5 Q 46.8 13.0 48.1 9.2 L 49.6 5.1 "
         "Q 50.0 4.0 50.0 4.0 Z")
_WEDGES = [("3.9 -39.1", "97.5 -38.3", "#C2972F"), ("97.5 -38.3", "142.9 24.2", "#17324D"),
           ("142.9 24.2", "113.6 114.6", "#2A7F9E"), ("113.6 114.6", "-12.3 115.7", "#7A263A"),
           ("-12.3 115.7", "-42.9 24.2", "#3F6F5E"), ("-42.9 24.2", "3.9 -39.1", "#6B7280")]


_FONT_PATH = PROJECT_ROOT / "visual_assets/brand/fonts/radio-canada-latin.woff2"
_font_css_cache = None


def _font_css():
    """@font-face embedding Radio Canada (base64) so the rasterizer renders the
    BRAND type, not a system fallback — keeps every card on-brand. Latin subset
    (English copy); add radio-canada-latin-ext.woff2 the same way for accents."""
    global _font_css_cache
    if _font_css_cache is None:
        b64 = base64.b64encode(_FONT_PATH.read_bytes()).decode()
        _font_css_cache = ('@font-face{font-family:"Radio Canada";font-style:normal;'
                           'font-weight:300 700;'
                           f'src:url(data:font/woff2;base64,{b64}) format("woff2");}}')
    return _font_css_cache


def _cells_leaf(x, y, s, clip_id="cbcSocial"):
    """The cells maple-leaf mark, scaled to `s` px, top-left at (x, y)."""
    wedges = "".join(f'<path d="M 50 44 L {a} L {b} Z" fill="{c}"/>' for a, b, c in _WEDGES)
    veins = "".join(f'<line x1="50" y1="44" x2="{a.split()[0]}" y2="{a.split()[1]}" '
                    f'stroke="{BG}" stroke-width="1.6"/>' for a, _, _ in _WEDGES)
    return (f'<g transform="translate({x},{y}) scale({s/100})">'
            f'<clipPath id="{clip_id}"><path d="{_LEAF}"/></clipPath>'
            f'<g clip-path="url(#{clip_id})">{wedges}{veins}</g></g>')


def _txt(x, y, s, fill, text, *, weight=400, anchor="start", spacing=None):
    sp = f' letter-spacing="{spacing}"' if spacing else ""
    return (f'<text x="{x}" y="{y}" font-family="{FONT}" font-size="{s}" '
            f'font-weight="{weight}" fill="{fill}" text-anchor="{anchor}"{sp}>{text}</text>')


def _cpi_card_svg(window, W, H):
    """Build the CPI card SVG (W×H) from a tidy [date, yoy] window. Fixed-top
    header + bottom footer; the chart fills the space between, so one composition
    works square, portrait (taller chart) and the wide landscape/OG banners."""
    x0, x1 = 150, W - 70
    title_y, sub_y, ctop = 130, 178, 235
    cbot, foot_y = H - 132, H - 42   # extra gap between the x-axis/ticks and the footer line
    leaf_sz = 132
    vals = window["yoy"].tolist()
    maxv = max(3.0, max(vals) + 0.6)   # modest headroom above the tallest bar for its label

    def y(v):
        return cbot - (v / maxv) * (cbot - ctop)

    n = len(vals)
    slot = (x1 - x0) / n
    bw = slot * 0.7
    parts = [f'<rect x="0" y="0" width="{W}" height="{H}" fill="{BG}"/>']

    # Header: title + subtitle (left), cells-leaf mark (upper right)
    parts.append(_txt(70, title_y, 54, NAVY, "Inflation (CPI)", weight=600))
    parts.append(_txt(70, sub_y, 31, SLATE, "Year-over-year change · Canada"))
    parts.append(_cells_leaf(x1 - leaf_sz * 0.86, 46, leaf_sz))

    # Bank of Canada 1-3% target band + 2% midpoint line
    parts.append(f'<rect x="{x0}" y="{y(3):.1f}" width="{x1-x0}" height="{y(1)-y(3):.1f}" '
                 f'fill="{NAVY}" opacity="0.06"/>')
    for v in (1, 3):
        parts.append(f'<line x1="{x0}" y1="{y(v):.1f}" x2="{x1}" y2="{y(v):.1f}" '
                     f'stroke="{NAVY}" stroke-width="1.4" opacity="0.22" stroke-dasharray="7 7"/>')
    parts.append(f'<line x1="{x0}" y1="{y(2):.1f}" x2="{x1}" y2="{y(2):.1f}" '
                 f'stroke="#555" stroke-width="1.6" opacity="0.55" stroke-dasharray="5 7"/>')

    # y-axis % labels (the band + dashed line carry the target; no text label needed)
    tick = 1 if maxv <= 5 else 2
    v = 0
    while v <= maxv + 0.01:
        parts.append(_txt(x0 - 18, y(v) + 9, 24, SLATE, f"{v:g}%", anchor="end"))
        v += tick

    # bars (valence colours mirror the site) + latest-value label
    last_cx = last_top = None
    for i, vv in enumerate(vals):
        bx = x0 + i * slot + (slot - bw) / 2
        col = RED if vv > 3 else (LAKE if vv < 1 else INBAND)
        parts.append(f'<rect x="{bx:.1f}" y="{y(vv):.1f}" width="{bw:.1f}" '
                     f'height="{cbot-y(vv):.1f}" rx="3" fill="{col}"/>')
        if i == n - 1:
            last_cx, last_top, last_col = bx + bw / 2, y(vv), col
    parts.append(_txt(last_cx, last_top - 16, 32, last_col, f"{vals[-1]:.1f}%",
                      weight=600, anchor="middle"))

    # x baseline + year ticks (each January in the window)
    parts.append(f'<line x1="{x0}" y1="{cbot}" x2="{x1}" y2="{cbot}" '
                 f'stroke="{SLATE}" stroke-width="1.2" opacity="0.5"/>')
    for i, d in enumerate(window["date"]):
        if d.month == 1:
            cx = x0 + i * slot + bw / 2
            parts.append(_txt(cx, cbot + 36, 24, SLATE, str(d.year), anchor="middle"))

    # footer (no URL until the .ca domain is live; no "next update" — that lives on the site)
    parts.append(_txt(70, foot_y, 27, SLATE, "Canada Observatory · Source: Statistics Canada (CPI)"))

    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}">'
            f'<defs><style>{_font_css()}</style></defs>'
            + "".join(parts) + "</svg>")


def _svg_to_png(svg_str, out_png, w, h):
    """Rasterize an SVG string to a w×h PNG via qlmanage + sips (macOS QuickLook).

    qlmanage thumbnails an SVG into a SQUARE (max side), letterboxing a non-square
    viewBox with the SVG centred; we normalise to that square then centre-crop to
    w×h (the project's standard SVG->PNG recipe; see visual_assets/brand/README.md)."""
    OUT_DIR.mkdir(exist_ok=True)
    m = max(w, h)
    with tempfile.TemporaryDirectory() as td:
        svg_path = os.path.join(td, "card.svg")
        with open(svg_path, "w") as f:
            f.write(svg_str)
        subprocess.run(["qlmanage", "-t", "-s", str(m), "-o", td, svg_path],
                       capture_output=True)
        rendered = os.path.join(td, "card.svg.png")
        if not os.path.exists(rendered):
            raise RuntimeError("qlmanage produced no PNG (is this macOS?)")
        subprocess.run(["sips", "-z", str(m), str(m), rendered], check=True, capture_output=True)
        subprocess.run(["sips", "-c", str(h), str(w), rendered, "--out", str(out_png)],
                       check=True, capture_output=True)
    return out_png


def build_cpi_card(fmt="portrait", months=24):
    """Build the CPI 'latest release' card for a SOCIAL_FORMATS key
    -> social/cpi_inflation_<YYYY-MM>_<fmt>.png."""
    w, h = SOCIAL_FORMATS[fmt]
    df = pd.read_csv(PROJECT_ROOT / "data/economics/statcan_cpi.csv", parse_dates=["date"])
    if "product_group" in df.columns:
        df = df[df["product_group"] == "All-items"]
    df = df.sort_values("date").reset_index(drop=True)
    df["yoy"] = pd.to_numeric(df["cpi_value"], errors="coerce").pct_change(12) * 100
    window = df.dropna(subset=["yoy"]).tail(months).reset_index(drop=True)
    period = window["date"].iloc[-1].strftime("%Y-%m")
    out = OUT_DIR / f"cpi_inflation_{period}_{fmt}.png"
    _svg_to_png(_cpi_card_svg(window, w, h), out, w, h)
    print(f"  wrote {out}  ({window['yoy'].iloc[-1]:.1f}% for {period}, {w}×{h})")
    return out


def build_all_cpi_cards(months=24):
    """Every SOCIAL_FORMATS variant of the CPI card."""
    return [build_cpi_card(fmt, months) for fmt in SOCIAL_FORMATS]


if __name__ == "__main__":
    build_all_cpi_cards()
