"""
Data-freshness health check for the daily pipeline (launch-gate G9).

Every registry indicator that fetches successfully re-stamps its metadata sidecar's
`retrieved_at` on every run, so on a healthy daily cadence no registry sidecar should
ever be more than a day or two old — regardless of how often the *source* publishes.
A sidecar that stops being re-stamped means the indicator is silently riding the
STALE-CSV fallback: its fetch is failing, or the pipeline is dying before reaching it
(the 2026-07 incident: a 5-minute workflow timeout killed the run mid-registry every
day for weeks while the job reported green — the tail indicators froze at June 20).

Run AFTER `python -m pipeline.run_pipeline`:

    python -m pipeline.check_freshness

Exit 0 = every registry indicator fresh (or allow-listed). Exit 1 = at least one
stale/missing sidecar; a human-readable report is printed and written to
REPORT_PATH for the workflow's notification step to attach to its GitHub issue.
"""

from datetime import datetime, timezone
import json
import sys

from pipeline.config import INDICATORS

# A sidecar older than this many days means the indicator has missed several
# consecutive daily runs (tolerates a weekend outage of a source without paging).
STALE_DAYS = 4

REPORT_PATH = "/tmp/freshness-report.txt"

# Indicators with a KNOWN long-running upstream outage, so the daily issue stays
# high-signal. Each entry needs a reason and should be removed when the source
# recovers (the check still lists them, marked "allowed", so they aren't forgotten).
# Applies to BOTH the stale and the missing-sidecar cases (a dead source can leave
# an old sidecar OR never produce one at all).
ALLOW_STALE = {
    # (Empty as of 2026-08-09. Add entries only for confirmed upstream outages so
    # the daily alert stays high-signal.)
    #
    # RESOLVED 2026-08-09 — OECD Productivity Database (labour_productivity,
    # labour_utilisation): the old DSD_PDB@DF_PDB_LV,1.0 flow's data backend was
    # decommissioned (~2026-03, HTTP 500 with the definition still registered);
    # both indicators now fetch from the successor DSD_PDB@DF_PDB,2.0.
    # RESOLVED 2026-08-09 — the 3 OECD SHA health-spending series broke
    # ~2026-07-05 because OECD version-bumped DSD_SHA 1.0→1.1 and pruned 1.0's
    # data; config now pins 1.1 (which also carries 2025 data 1.0 never got).
    # KNOWN FUTURE RISK, same class: DAC1 pins 1.4 (catalogue latest 1.7) and
    # HHDASH pins 1.0 (latest 1.1). Both still served data on 2026-08-08; if OECD
    # prunes them the way it pruned SHA 1.0, this check flags them within days.
}


def main():
    now = datetime.now(timezone.utc)
    stale, allowed, missing, unknown = [], [], [], []

    for ind in INDICATORS:
        sidecar = ind.out_path.with_suffix(".json")
        if not sidecar.exists():
            (allowed.append((ind.id, None, ALLOW_STALE[ind.id]))
             if ind.id in ALLOW_STALE else missing.append(ind.id))
            continue
        try:
            retrieved = json.loads(sidecar.read_text()).get("retrieved_at")
        except (json.JSONDecodeError, OSError):
            missing.append(ind.id)
            continue
        if not retrieved:
            unknown.append(ind.id)
            continue
        try:
            ts = datetime.fromisoformat(str(retrieved).replace("Z", "+00:00"))
        except ValueError:
            unknown.append(ind.id)
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = (now - ts).days
        if age >= STALE_DAYS:
            if ind.id in ALLOW_STALE:
                allowed.append((ind.id, age, ALLOW_STALE[ind.id]))
            else:
                stale.append((ind.id, age, str(sidecar)))

    lines = [f"Data-freshness check — {now:%Y-%m-%d %H:%M} UTC "
             f"({len(INDICATORS)} registry indicators, threshold {STALE_DAYS} days)"]
    if stale:
        lines.append(f"\nSTALE ({len(stale)}) — fetch failing or never reached this run:")
        for iid, age, path in sorted(stale, key=lambda t: -t[1]):
            lines.append(f"  {iid:32s} last fetched {age} days ago  ({path})")
    if missing:
        lines.append(f"\nMISSING sidecar ({len(missing)}): " + ", ".join(sorted(missing)))
    if allowed:
        lines.append(f"\nAllow-listed (known upstream outages):")
        for iid, age, why in allowed:
            state = "missing" if age is None else f"{age} days stale"
            lines.append(f"  {iid:32s} {state} — {why}")
    if unknown:
        lines.append(f"\nNo readable retrieved_at ({len(unknown)}): " + ", ".join(sorted(unknown)))
    if not stale and not missing:
        lines.append("\nAll fresh.")

    report = "\n".join(lines)
    print(report)
    try:
        with open(REPORT_PATH, "w") as f:
            f.write(report + "\n")
    except OSError:
        pass

    sys.exit(1 if (stale or missing) else 0)


if __name__ == "__main__":
    main()
