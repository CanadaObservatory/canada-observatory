"""
Post-render site guards (launch-plan E5). Run against the rendered site:

    python -m pipeline.check_site _site

Three checks:

1. og:image must be a raster (.png) on EVERY page — FAILS the run otherwise.
   Quarto auto-discovers the first body <img> as the share image when a page has no
   explicit `image:`; on the landing/overview pages that first image is a brand-leaf
   SVG, which X/Slack/iMessage/LinkedIn will not rasterize (launch gate G1 — the fix
   is the `image:` line in every directory's _metadata.yml; this guard keeps it fixed).

2. Navbar consistency — FAILS if any page's navbar menu differs from the majority.
   The navbar comes from _quarto.yml so a single render can't really diverge; this
   guards against partial renders/stale files ever being uploaded again (a July 2026
   external review saw mixed navs on the live site, most plausibly stale CDN copies).

3. Placeholder sections — WARNS (never fails) on source .qmd sections that have a
   heading but no narrative prose (only code/data-current lines), like the empty
   "Latest household debt: ranked" section the same review caught.
"""

from collections import Counter
from pathlib import Path
import re
import sys

RASTER_OK = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def page_files(site_dir: Path):
    for p in sorted(site_dir.rglob("*.html")):
        if "site_libs" in p.parts:
            continue
        yield p


def check_og_images(site_dir: Path):
    bad = []
    for p in page_files(site_dir):
        html = p.read_text(errors="replace")
        # Quarto `aliases:` redirect stubs are bare JS-redirect pages with no
        # meta tags or navbar — not reader pages, so exempt from the guard.
        if "<title>Redirect</title>" in html and "var redirects" in html:
            continue
        m = re.search(r'<meta property="og:image" content="([^"]+)"', html)
        if not m:
            bad.append((str(p.relative_to(site_dir)), "(no og:image tag)"))
        elif not m.group(1).split("?")[0].lower().endswith(RASTER_OK):
            bad.append((str(p.relative_to(site_dir)), m.group(1)))
    return bad


def navbar_signature(html: str):
    """Ordered menu labels inside the top navbar (depth-independent)."""
    m = re.search(r"<nav[^>]*\bnavbar\b.*?</nav>", html, re.S)
    if not m:
        return None
    labels = re.findall(r'<span class="menu-text">(.*?)</span>', m.group(0))
    return tuple(labels) if labels else None


def check_nav(site_dir: Path):
    sigs = {}
    for p in page_files(site_dir):
        sig = navbar_signature(p.read_text(errors="replace"))
        if sig:  # pages without a navbar (none expected) are skipped
            sigs[str(p.relative_to(site_dir))] = sig
    if not sigs:
        return ["(no navbars found at all — selector out of date?)"]
    majority, _ = Counter(sigs.values()).most_common(1)[0]
    return [f"{page}: navbar differs from the majority "
            f"({len(set(majority) ^ set(sig))} label(s) differ)"
            for page, sig in sigs.items() if sig != majority]


def check_placeholders(repo_root: Path):
    """Source-side: sections whose only content is code / a 'Data current to' line."""
    warnings = []
    for qmd in sorted(repo_root.rglob("*.qmd")):
        if any(part in ("_site", "_strategy", "temp_from_chat", ".claude") for part in qmd.parts):
            continue
        lines = qmd.read_text(errors="replace").splitlines()
        heading, prose, in_fence = None, [], False
        sections = []  # (heading, prose_lines)
        for ln in lines:
            if ln.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if re.match(r"^#{2,4}\s", ln):
                if heading is not None:
                    sections.append((heading, prose))
                heading, prose = ln.strip(), []
            elif heading is not None and ln.strip():
                prose.append(ln.strip())
        if heading is not None:
            sections.append((heading, prose))
        for head, prose in sections:
            # "Latest X: ranked" companion bars are an established idiom whose
            # narrative lives with the paired line chart above — skip them here.
            # (If the E4 accessibility pass later adds a one-line takeaway to every
            # chart, drop this exclusion.)
            if re.match(r"^#+\s+Latest .*: ranked$", head):
                continue
            substantive = [l for l in prose
                           if not re.match(r"^\*\*Data current to", l)]
            if not substantive:
                warnings.append(f"{qmd.relative_to(repo_root)} — '{head}' has no narrative prose")
    return warnings


def main():
    site_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "_site")
    if not site_dir.is_dir():
        print(f"check_site: {site_dir} not found — render the site first.")
        sys.exit(2)

    failures = 0

    bad_og = check_og_images(site_dir)
    if bad_og:
        failures += 1
        print(f"FAIL — {len(bad_og)} page(s) with a non-raster or missing og:image:")
        for page, url in bad_og:
            print(f"  {page}: {url}")
    else:
        print("ok — every page's og:image is a raster")

    nav_bad = check_nav(site_dir)
    if nav_bad:
        failures += 1
        print(f"FAIL — navbar inconsistency on {len(nav_bad)} page(s):")
        for msg in nav_bad[:20]:
            print(f"  {msg}")
    else:
        print("ok — navbar identical on every page")

    placeholders = check_placeholders(Path("."))
    if placeholders:
        print(f"warn — {len(placeholders)} section(s) with no narrative prose (not fatal):")
        for msg in placeholders[:30]:
            print(f"  {msg}")
    else:
        print("ok — no prose-less sections found")

    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
