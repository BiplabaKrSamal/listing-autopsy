#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════╗
║  tracker.py — standalone CLI                                 ║
║                                                              ║
║  Runs the full pipeline from the terminal.                   ║
║  No Flask server needed.                                     ║
║                                                              ║
║  Usage:                                                      ║
║    python tracker.py --asin B08X3K9PLM                       ║
║    python tracker.py --asin B08X3K9PLM --days 90             ║
║    python tracker.py --asin B08X3K9PLM --output report.html  ║
║    python tracker.py --asin B08X3K9PLM --skip-wayback        ║
║    python tracker.py --help                                  ║
╚══════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Load .env before anything else so env vars are available
load_dotenv()

from pipeline import run_pipeline  # noqa: E402 (after load_dotenv)


# ─────────────────────────────────────────────────────────────
# CLI ARGUMENT PARSER
# ─────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tracker.py",
        description=(
            "Listing Autopsy — Competitor listing evolution tracker\n"
            "Wayback Machine CDX + Keepa + Anthropic Claude\n"
            "github.com/BiplabaKrSamal/listing-autopsy"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python tracker.py --asin B08X3K9PLM
  python tracker.py --asin B08X3K9PLM --days 60
  python tracker.py --asin B08X3K9PLM --output competitor.html
  python tracker.py --asin B08X3K9PLM --skip-wayback --days 30

  # batch (shell loop)
  for asin in B08X3K9PLM B09ABC1234; do
    python tracker.py --asin $asin --days 90
  done
        """,
    )

    p.add_argument(
        "--asin",
        required=True,
        help="Amazon ASIN to track (e.g. B08X3K9PLM)",
    )
    p.add_argument(
        "--keepa-key",
        default=os.getenv("KEEPA_API_KEY"),
        metavar="KEY",
        help="Keepa API key. Defaults to KEEPA_API_KEY env var. "
             "Get one at keepa.com/api (free tier: 250 tokens/day)",
    )
    p.add_argument(
        "--days",
        type=int,
        default=120,
        metavar="N",
        help="Number of days of history to analyze (default: 120)",
    )
    p.add_argument(
        "--output",
        default=None,
        metavar="FILE",
        help="Output HTML report filename (default: report_<ASIN>.html)",
    )
    p.add_argument(
        "--skip-wayback",
        action="store_true",
        help="Skip Wayback scraping — use Keepa + Claude only (much faster, "
             "no listing text diffs)",
    )
    return p


# ─────────────────────────────────────────────────────────────
# PROGRESS DISPLAY
# ─────────────────────────────────────────────────────────────

def print_progress(stage: str, msg: str, pct: int) -> None:
    """
    Render a compact progress bar to stdout.

    Example output:
      [████████████░░░░░░░░]  62%  [keepa] Loaded 2,847 BSR datapoints.
    """
    filled = pct // 5
    bar    = "█" * filled + "░" * (20 - filled)
    line   = f"  [{bar}] {pct:3d}%  [{stage:<8}] {msg:<60}"
    # Use \r to overwrite the same line; flush so it appears immediately
    print(line, end="\r", flush=True)


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────

def main() -> None:
    parser = build_parser()
    args   = parser.parse_args()

    # ── Validate keys ──────────────────────────────────────
    keepa_key     = args.keepa_key
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")

    if not keepa_key:
        parser.error(
            "Keepa API key required. Use --keepa-key KEY or set KEEPA_API_KEY env var.\n"
            "Get a free key at: https://keepa.com/api"
        )
    if not anthropic_key:
        parser.error(
            "Anthropic API key required. Set ANTHROPIC_API_KEY env var.\n"
            "Get one at: https://console.anthropic.com"
        )

    # ── Resolve output path ────────────────────────────────
    out_path = Path(args.output) if args.output else Path(f"report_{args.asin}.html")

    # ── Banner ─────────────────────────────────────────────
    divider = "═" * 62
    print(f"\n{divider}")
    print(f"  Listing Autopsy — {args.asin}")
    print(f"  History window : last {args.days} days")
    print(f"  Output file    : {out_path}")
    print(f"  Wayback scrape : {'SKIPPED' if args.skip_wayback else 'ENABLED'}")
    print(f"{divider}\n")

    # ── Run pipeline ───────────────────────────────────────
    report_html: str | None = None
    headline:    str        = ""

    for event in run_pipeline(
        asin=args.asin,
        keepa_key=keepa_key,
        anthropic_key=anthropic_key,
        days=args.days,
    ):
        stage = event.get("stage", "")
        msg   = event.get("msg", "")
        pct   = event.get("pct", 0)

        if stage == "done":
            # Clear the progress line
            print(" " * 80, end="\r")
            report_html = event.get("report")
            headline    = event.get("headline", "")

        elif stage == "error":
            print(" " * 80, end="\r")
            print(f"\n  ✗ ERROR: {msg}\n", flush=True)
            sys.exit(1)

        else:
            print_progress(stage, msg, pct)

    # ── Write report ───────────────────────────────────────
    if report_html:
        out_path.write_text(report_html, encoding="utf-8")

        print(f"\n{divider}")
        print(f"  ✓ Complete")
        if headline:
            print(f"  ✦ {headline[:70]}")
        print(f"  → Report saved: {out_path.resolve()}")
        print(f"{divider}\n")
        print(f"  Open in browser:")
        print(f"    open {out_path}          # macOS")
        print(f"    xdg-open {out_path}      # Linux")
        print(f"    start {out_path}         # Windows")
        print()
    else:
        print(f"\n  ✗ No report generated. Check the error messages above.\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
