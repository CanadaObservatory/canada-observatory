"""PROTOTYPE — export a chart to a static, mobile-optimized PNG via Plotly + kaleido.

Demonstrates the static-image path (Option H in _strategy/mobile-options-analysis.md):
a fallback for mobile and the basis for social cards. One real peer chart (Real GDP
growth) is built with the PRODUCTION builder, then relaid out for a tall phone canvas
(legend below, range slider off, greyed legend-only peers dropped — you can't click
them in an image) and rasterized to PNG.

This is a proof of concept, not wired into the site. A full version would (a) loop over
the charts we want as images, (b) embed Radio-Canada (kaleido uses a fallback font here;
the social_cards.py qlmanage path embeds the brand font), and (c) run in CI on a Linux
rasterizer. Run: python -m pipeline.build_static_charts
"""
import pandas as pd
from pipeline.charts import peer_comparison_line

OUT = "/tmp/static-chart-proto.png"


def build(out=OUT):
    df = pd.read_csv("data/economics/oecd_real_gdp_growth.csv")
    fig = peer_comparison_line(
        df, "year", "real_gdp_growth", title="Real GDP growth",
        yaxis_title="Real GDP growth (annual %)", show_average=True,
        initial_start=2015, initial_visible=["USA"])
    fig.add_hline(y=0, line_dash="dot", line_color="#888", opacity=0.5)

    # Static = no interactivity, so drop the greyed legend-only peers from the legend.
    for tr in fig.data:
        if getattr(tr, "visible", True) == "legendonly":
            tr.showlegend = False

    # Tall phone canvas: legend below, slider off, a title (the page heading isn't here).
    fig.update_layout(
        width=450, height=720, showlegend=True,
        title=dict(text="Real GDP growth", x=0.02, xanchor="left",
                   y=0.97, font=dict(size=19, color="#17324D")),
        legend=dict(orientation="h", x=0, xanchor="left", y=-0.14, yanchor="top",
                    font=dict(size=11)),
        margin=dict(l=10, r=16, t=52, b=96),
        annotations=[dict(
            text="Source: OECD Economic Outlook  ·  Canada Observatory",
            xref="paper", yref="paper", x=0, y=-0.27, showarrow=False,
            font=dict(size=10, color="#999"))],
    )
    # Static image: drop both interactive range controls (slider + buttons).
    fig.update_xaxes(rangeslider=dict(visible=False), rangeselector=dict(visible=False))
    fig.write_image(out, scale=2)
    print("wrote", out)
    return out


if __name__ == "__main__":
    build()
