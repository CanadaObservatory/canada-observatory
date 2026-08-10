"""CREA MLS® Home Price Index — public educational charts.

Used **with written permission from CREA for educational public use** on this site.
Per that permission we publish *charts* (display), not the raw dataset: this module
loads the monthly HPI workbook at **render time** and builds figures, and **never
writes CREA data to `data/` or commits it** — only the rendered charts are published.

The monthly zip is cached in the git-ignored `internal/` dir (reused locally; a fresh
CI checkout downloads the latest month). Every figure carries the CREA attribution.

Data: `Seasonally Adjusted (M).xlsx` inside `MLS_HPI_<Month>_<Year>.zip` — per-market
sheets with `*_Benchmark_SA` columns + an `AGGREGATE` sheet (`Composite_Benchmark_SA`).
"""

import io
import os
import zipfile
import requests
import pandas as pd
import plotly.graph_objects as go
from pipeline.config import CANADA_COLOR, CATEGORICAL_COLORS

ATTRIB = ("Source: CREA MLS® Home Price Index, © The Canadian Real Estate Association. "
          "Used with permission for educational purposes.")
_HDR = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}

# (CREA workbook sheet name, display label)
MARKETS = [
    ("GREATER_VANCOUVER", "Vancouver"),
    ("GREATER_TORONTO",   "Toronto"),
    ("OTTAWA",            "Ottawa"),
    ("MONTREAL_CMA",      "Montréal"),
    ("CALGARY",           "Calgary"),
    ("EDMONTON",          "Edmonton"),
    ("WINNIPEG",          "Winnipeg"),
    ("HALIFAX_DARTMOUTH", "Halifax"),
]
TYPES = [
    # NB the CREA "Single Family" benchmark includes BOTH detached and
    # semi-detached (attached) single-family homes — never label it "Detached"
    # (MLS HPI Methodology, App. C; no national semi-detached series exists).
    ("Single_Family_Benchmark_SA", "Single-family house", "#1f77b4"),
    ("Townhouse_Benchmark_SA",     "Townhouse",           "#ff7f0e"),
    ("Apartment_Benchmark_SA",     "Apartment / condo",   "#2ca02c"),
]
# Years-of-income selector: the composite "typical home" leads, then each type.
RATIO_TYPES = [
    ("Composite_Benchmark_SA",     "Typical home"),
    ("Single_Family_Benchmark_SA", "Single-family house"),
    ("Townhouse_Benchmark_SA",     "Townhouse"),
    ("Apartment_Benchmark_SA",     "Apartment / condo"),
]
CITY_PALETTE = ["#B5403A", "#1f77b4", "#2ca02c", "#9467bd", "#ff7f0e",
                "#8c564b", "#17becf", "#bcbd22"]
# CREA market sheet -> 2021-Census CMA code, for the entry-level income join.
ENTRY_CMAUID = {
    "GREATER_VANCOUVER": "933", "GREATER_TORONTO": "535", "OTTAWA": "505",
    "MONTREAL_CMA": "462", "CALGARY": "825", "EDMONTON": "835",
    "WINNIPEG": "602", "HALIFAX_DARTMOUTH": "205",
}


def _zip_bytes(cache_dir):
    """Reuse a cached MLS_HPI_*.zip in `cache_dir` (git-ignored internal/), else
    download the latest available month. Raises if none can be obtained."""
    if os.path.isdir(cache_dir):
        cached = sorted(f for f in os.listdir(cache_dir)
                        if f.startswith("MLS_HPI_") and f.endswith(".zip"))
        if cached:
            return open(os.path.join(cache_dir, cached[-1]), "rb").read()
    import datetime  # current month is fine here; only used to pick a filename
    months = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]
    # try the last few months across the plausible current/previous year
    candidates = []
    for yr in (2026, 2025):
        for m in months:
            candidates.append((m, yr))
    for m, yr in candidates:
        url = f"https://www.crea.ca/files/mls-hpi-data/MLS_HPI_{m}_{yr}.zip"
        try:
            r = requests.get(url, headers=_HDR, timeout=90)
        except Exception:
            continue
        if r.status_code == 200 and r.content[:2] == b"PK":
            os.makedirs(cache_dir, exist_ok=True)
            open(os.path.join(cache_dir, f"MLS_HPI_{m}_{yr}.zip"), "wb").write(r.content)
            return r.content
    raise RuntimeError("CREA MLS HPI zip unavailable (no cache, download failed)")


def load_crea(root="."):
    """Return (sheets dict, aggregate df, month label) from the latest CREA workbook."""
    z = zipfile.ZipFile(io.BytesIO(_zip_bytes(os.path.join(root, "internal"))))
    xls = pd.ExcelFile(io.BytesIO(z.read("Seasonally Adjusted (M).xlsx")))
    sheets = {s: xls.parse(s) for s, _ in MARKETS}
    agg = xls.parse("AGGREGATE")
    month = pd.Timestamp(agg["Date"].max()).strftime("%B %Y")
    return sheets, agg, month


def _src(fig, note, y=-0.18):
    from pipeline.charts import _wrap     # word-wrap so the long CREA attribution fits the column
    fig.add_annotation(text=_wrap(note, 82), xref="paper", yref="paper", x=0,
                       xanchor="left", y=y, yanchor="top", align="left",
                       showarrow=False, font=dict(size=10, color="#999"))


def fig_price_by_type(sheets, month):
    """Grouped bar: latest benchmark price by city × dwelling type."""
    rows = []
    for sheet, label in MARKETS:
        last = sheets[sheet].dropna(subset=["Single_Family_Benchmark_SA"]).iloc[-1]
        rows.append({"city": label, **{c: last[c] for c, _, _ in TYPES}})
    table = pd.DataFrame(rows).sort_values("Single_Family_Benchmark_SA", ascending=False)
    fig = go.Figure()
    for col, name, color in TYPES:
        fig.add_trace(go.Bar(x=table["city"], y=table[col], name=name, marker_color=color,
                             hovertemplate=f"{name}: $%{{y:,.0f}}<extra>%{{x}}</extra>"))
    fig.update_layout(barmode="group", plot_bgcolor="white",
        yaxis=dict(title="Benchmark price", tickprefix="$", tickformat=",", gridcolor="#e0e0e0"),
        xaxis=dict(title=""), height=520, margin=dict(b=150, t=30),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0))
    _src(fig, f"{ATTRIB} Benchmark (seasonally adjusted), {month}.", y=-0.20)
    return fig


def fig_price_time_series(sheets):
    """Benchmark price over time by city, with a dwelling-type dropdown
    (single-family / townhouse / apartment). Single-family shows first."""
    short = {"Single_Family_Benchmark_SA": "single-family house",
             "Townhouse_Benchmark_SA": "townhouse",
             "Apartment_Benchmark_SA": "apartment / condo"}
    default_col = TYPES[0][0]
    fig = go.Figure()
    for (sheet, label), color in zip(MARKETS, CITY_PALETTE):
        df = sheets[sheet][["Date", default_col]].dropna()
        fig.add_trace(go.Scatter(x=df["Date"], y=df[default_col],
                                 name=label, mode="lines", line=dict(color=color, width=2),
                                 hovertemplate=f"{label}: $%{{y:,.0f}}<extra>%{{x|%b %Y}}</extra>"))
    # Each dwelling type re-styles every city trace's x/y (date ranges differ by type)
    buttons = []
    for col, tlabel, _ in TYPES:
        xs, ys = [], []
        for sheet, _label in MARKETS:
            d = sheets[sheet][["Date", col]].dropna()
            xs.append(d["Date"]); ys.append(d[col])
        buttons.append(dict(method="update", label=tlabel,
            args=[{"x": xs, "y": ys},
                  {"yaxis.title.text": f"Benchmark price ({short[col]})"}]))
    fig.update_layout(plot_bgcolor="white",
        yaxis=dict(title=f"Benchmark price ({short[default_col]})",
                   tickprefix="$", tickformat=",", gridcolor="#e0e0e0"),
        xaxis=dict(title="", gridcolor="#e0e0e0",
                   rangeslider=dict(visible=True, thickness=0.08, bgcolor="#f5f5f5")),
        height=560, margin=dict(b=165, t=54, r=160),
        legend=dict(orientation="v", x=1.02, y=1),
        updatemenus=[dict(buttons=buttons, active=0, x=0, xanchor="left", y=1.07,
            yanchor="bottom", bgcolor="white", bordercolor="#ccc", borderwidth=1, showactive=True)])
    _src(fig, ATTRIB, y=-0.30)
    return fig


def _years_of_income(bench_annual, inc, cpi, cpi2024):
    """Annual-mean benchmark (nominal $) → years of 2024-real median income.
    Deflate each year's benchmark to 2024 $ via CPI, then divide by median income."""
    xs, ys = [], []
    for yr in sorted(set(bench_annual.index) & set(inc.index)):
        if pd.isna(bench_annual.loc[yr]):
            continue
        bench_real = bench_annual.loc[yr] * (cpi2024 / cpi.loc[yr])
        xs.append(yr)
        ys.append(bench_real / inc.loc[yr])
    return xs, ys


def years_of_income_series(agg, root="."):
    """{benchmark column: (years, ratios)} for each RATIO_TYPES entry — the
    years-of-median-income series behind fig_price_to_income, shared with the
    housing page's computed inline prose figures so text and chart can never
    disagree."""
    inc = pd.read_csv(os.path.join(root, "data/income/statcan_median_income.csv"),
                      parse_dates=["date"])
    inc = inc.assign(year=inc["date"].dt.year).set_index("year")["median_income"]
    cpi = pd.read_csv(os.path.join(root, "data/economics/statcan_cpi.csv"),
                      parse_dates=["date"])
    cpi = cpi.assign(year=cpi["date"].dt.year).groupby("year")["cpi_value"].mean()
    cpi2024 = cpi.loc[2024]
    agg = agg.assign(year=agg["Date"].dt.year)
    return {col: _years_of_income(agg.groupby("year")[col].mean(), inc, cpi, cpi2024)
            for col, _ in RATIO_TYPES}


def fig_price_to_income(agg, root="."):
    """National benchmark ÷ median after-tax income, over time, with a dwelling-type
    selector (typical home / detached / townhouse / apartment).

    CREA benchmark is nominal $; median income is in 2024 constant $. Deflate the
    benchmark to 2024 $ via CPI, then divide → "years of median after-tax income"."""
    series = years_of_income_series(agg, root)

    default_col = RATIO_TYPES[0][0]
    xs0, ys0 = series[default_col]
    fig = go.Figure(go.Scatter(x=xs0, y=ys0, mode="lines+markers",
                               line=dict(color=CANADA_COLOR, width=3),
                               hovertemplate="%{y:.1f}× income<extra>%{x}</extra>"))
    # The selector restyles the single trace's x/y to the chosen dwelling type.
    buttons = [dict(method="update", label=label,
                    args=[{"x": [series[col][0]], "y": [series[col][1]]}])
               for col, label in RATIO_TYPES]
    fig.update_layout(plot_bgcolor="white",
        yaxis=dict(title="Years of median after-tax income", gridcolor="#e0e0e0", rangemode="tozero"),
        xaxis=dict(title="", gridcolor="#e0e0e0"), height=480, margin=dict(b=155, t=54),
        showlegend=False,
        updatemenus=[dict(buttons=buttons, active=0, x=0, xanchor="left", y=1.07,
            yanchor="bottom", bgcolor="white", bordercolor="#ccc", borderwidth=1, showactive=True)])
    _src(fig, f"{ATTRIB} Benchmark deflated to 2024 $ via CPI; income: StatCan 11-10-0190-01.", y=-0.16)
    return fig


def fig_entry_level_affordability(sheets, month, root="."):
    """Starter-home (apartment / condo) affordability by city: the latest CREA
    apartment benchmark ÷ a typical local household income, as a ranked bar of
    "years of income". No clean entry-level price feed exists, so the apartment
    tier stands in for the starter home (the affordability companion to the
    value-to-income map, which uses the *typical* dwelling). Income is the
    2021-Census median household income by metro, grown to the benchmark year by
    national average weekly earnings — a single factor, so the cross-city ranking
    is unaffected by the income vintage."""
    from pipeline.charts import category_bar
    cma = pd.read_csv(os.path.join(root, "data/geo/statcan_cma_indicators.csv"),
                      dtype={"cmauid": str}).set_index("cmauid")["median_income"]
    wage = pd.read_csv(os.path.join(root, "data/income/statcan_wages_by_province.csv"))
    awe = wage[wage["geo"] == "Canada"].set_index("year")["avg_weekly_wage"]
    rows, byear = [], None
    for sheet, label in MARKETS:
        uid = ENTRY_CMAUID.get(sheet)
        if uid is None or uid not in cma.index or pd.isna(cma.get(uid)):
            continue
        s = sheets[sheet][["Date", "Apartment_Benchmark_SA"]].dropna()
        if s.empty:
            continue
        last = s.iloc[-1]
        byear = pd.Timestamp(last["Date"]).year
        wyear = min(byear, int(awe.index.max()))
        income = float(cma.loc[uid]) * (awe.get(wyear, awe.iloc[-1]) / awe.loc[2020])
        rows.append({"city": label, "ratio": float(last["Apartment_Benchmark_SA"]) / income})
    table = pd.DataFrame(rows)
    fig = category_bar(table, "city", "ratio",
        xaxis_title="Years of a typical household's income (apartment / condo)",
        value_fmt=".1f", color=CATEGORICAL_COLORS[0],
        text_col="ratio", text_fmt="{:.1f}",
        hovertemplate="%{y}: %{x:.1f} years of household income<extra></extra>",
        source_note=f"{ATTRIB} Apartment/condo benchmark ({month}) ÷ 2021-Census median "
                    f"household income by metro, grown to {byear} by average weekly earnings.")
    return fig


def fig_price_by_type_over_time(sheets, agg):
    """Benchmark price over time by dwelling type — single-family, townhouse,
    apartment — on ONE chart, with a city selector, so the gap between a
    single-family house and a condo (the cost of moving up the ladder) is visible. Opens on the
    national aggregate; the spread is far wider in Vancouver and Toronto. Each city
    option restyles the three type traces' x/y to that market. (Complements the
    existing by-city chart, which switches one type at a time.)"""
    geos = [("Canada", agg)] + [(label, sheets[sheet]) for sheet, label in MARKETS]

    def series(frame, col):
        d = frame[["Date", col]].dropna()
        return d["Date"], d[col]

    fig = go.Figure()
    for col, name, color in TYPES:
        x, y = series(agg, col)
        fig.add_trace(go.Scatter(x=x, y=y, name=name, mode="lines",
            line=dict(color=color, width=2.5),
            hovertemplate=f"{name}: $%{{y:,.0f}}<extra>%{{x|%b %Y}}</extra>"))
    buttons = []
    for label, frame in geos:
        xs, ys = [], []
        for col, name, color in TYPES:
            x, y = series(frame, col)
            xs.append(x)
            ys.append(y)
        buttons.append(dict(method="update", label=label, args=[{"x": xs, "y": ys}]))
    fig.update_layout(plot_bgcolor="white", height=480, hovermode="x unified",
        yaxis=dict(title="Benchmark price", tickprefix="$", tickformat=",",
                   gridcolor="#e0e0e0", rangemode="tozero"),
        xaxis=dict(title="", gridcolor="#e0e0e0"),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(t=72, b=95, r=20),
        updatemenus=[dict(buttons=buttons, active=0, x=0, xanchor="left", y=1.12,
            yanchor="bottom", bgcolor="white", bordercolor="#ccc", borderwidth=1,
            showactive=True)])
    _src(fig, ATTRIB, y=-0.22)
    return fig
