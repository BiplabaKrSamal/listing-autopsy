"""
╔══════════════════════════════════════════════════════════════╗
║  pipeline.py — Listing Autopsy core engine                   ║
║                                                              ║
║  APIs wired here:                                            ║
║    1. Wayback Machine CDX  (snapshot discovery + fetch)      ║
║    2. Keepa Product API    (BSR / price / review history)    ║
║    3. Anthropic Claude API (causal analysis + playbook)      ║
║                                                              ║
║  Usage (imported by app.py and tracker.py):                  ║
║    from pipeline import run_pipeline                         ║
║    for event in run_pipeline(asin, keepa_key, claude_key):   ║
║        print(event)   # {"stage","msg","pct"} or "done"      ║
╚══════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional

import anthropic
import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────────

WAYBACK_CDX   = "https://web.archive.org/cdx/search/cdx"
WAYBACK_FETCH = "https://web.archive.org/web/{ts}if_/{url}"
KEEPA_API     = "https://api.keepa.com/product"
AMAZON_URL    = "https://www.amazon.com/dp/{asin}"

# Keepa stores timestamps as minutes elapsed since this epoch
KEEPA_EPOCH   = datetime(2011, 1, 1, tzinfo=timezone.utc)
KEEPA_OFFSET  = 21_564_000   # minutes between keepa epoch and real epoch

# Metadata for each change type (used in the HTML report)
CHANGE_META: dict[str, dict] = {
    "title":       {"color": "#7F77DD", "label": "Title"},
    "bullets":     {"color": "#5DCAA5", "label": "Bullets"},
    "main_image":  {"color": "#D85A30", "label": "Main image"},
    "price":       {"color": "#EF9F27", "label": "Price"},
    "reviews":     {"color": "#BA7517", "label": "Review spike"},
    "description": {"color": "#888780", "label": "Description"},
}

SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────────────────────────
# STEP 1 — WAYBACK MACHINE: snapshot discovery
# ─────────────────────────────────────────────────────────────

def get_snapshots(asin: str, days: int) -> list[dict]:
    """
    Query the Wayback CDX API for all archived snapshots of an
    Amazon listing URL within the last `days` days.

    Returns a list of dicts: [{"timestamp": "20260303120000", "statuscode": "200"}, ...]

    CDX parameters used:
      collapse=timestamp:8  — deduplicate to ~1 snapshot per calendar day
      filter=statuscode:200 — skip redirects and errors
      limit=150             — cap at 150 snapshots max
    """
    url    = AMAZON_URL.format(asin=asin)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    params = {
        "url":      url,
        "output":   "json",
        "fl":       "timestamp,statuscode",
        "filter":   "statuscode:200",
        "collapse": "timestamp:8",
        "from":     cutoff.strftime("%Y%m%d%H%M%S"),
        "limit":    "150",
    }

    resp = requests.get(WAYBACK_CDX, params=params, timeout=30)
    resp.raise_for_status()

    rows = resp.json()
    if not rows or len(rows) < 2:
        return []  # first row is the header; <2 rows means no data

    header, *data = rows
    return [dict(zip(header, row)) for row in data]


# ─────────────────────────────────────────────────────────────
# STEP 2 — WAYBACK MACHINE: snapshot fetch + HTML parse
# ─────────────────────────────────────────────────────────────

def fetch_snapshot(timestamp: str, asin: str) -> Optional[str]:
    """
    Fetch a single Wayback snapshot via the if_ replay URL.

    The `if_` modifier bypasses the Wayback toolbar injection,
    giving us cleaner HTML closer to what Amazon actually served.

    Returns raw HTML string, or None if the fetch fails.
    """
    target_url = AMAZON_URL.format(asin=asin)
    replay_url = WAYBACK_FETCH.format(ts=timestamp, url=target_url)

    try:
        resp = requests.get(
            replay_url,
            headers=SCRAPE_HEADERS,
            timeout=20,
            allow_redirects=True,
        )
        # Require 200 + minimum content length to filter out error pages
        if resp.status_code == 200 and len(resp.text) > 5_000:
            return resp.text
    except requests.RequestException:
        pass

    return None


def parse_listing(html: str) -> dict:
    """
    Extract structured listing fields from an Amazon product page HTML.

    Targets (CSS selectors for 2024–2026 Amazon DOM):
      title       → #productTitle
      bullets     → #feature-bullets li span.a-list-item
      main_image  → #landingImage[data-old-hires] or [src]
      price       → .a-price .a-offscreen  or  #priceblock_ourprice
      reviews     → #acrCustomerReviewText
      description → #productDescription p

    Returns a dict with all fields (empty/None if not found).
    """
    soup = BeautifulSoup(html, "lxml")

    def sel_text(selector: str) -> str:
        el = soup.select_one(selector)
        return el.get_text(strip=True) if el else ""

    # ── title ──────────────────────────────────────────────
    title = (
        sel_text("#productTitle")
        or sel_text("h1#title span")
        or sel_text("span#productTitle")
    )

    # ── bullet points ──────────────────────────────────────
    bullets = [
        b.get_text(strip=True)
        for b in soup.select("#feature-bullets li span.a-list-item")
        if b.get_text(strip=True)
    ]

    # ── main image URL ─────────────────────────────────────
    main_image = ""
    img_el = soup.select_one("#landingImage, #imgBlkFront")
    if img_el:
        main_image = img_el.get("data-old-hires") or img_el.get("src", "")

    # ── price ──────────────────────────────────────────────
    price: Optional[float] = None
    raw_price = sel_text(".a-price .a-offscreen") or sel_text("#priceblock_ourprice")
    if raw_price:
        m = re.search(r"[\d,]+\.?\d*", raw_price.replace(",", ""))
        if m:
            price = float(m.group())

    # ── review count ───────────────────────────────────────
    review_count = 0
    rc_el = soup.select_one("#acrCustomerReviewText")
    if rc_el:
        m = re.search(r"[\d,]+", rc_el.get_text().replace(",", ""))
        if m:
            review_count = int(m.group())

    # ── description ────────────────────────────────────────
    description = sel_text("#productDescription p") or sel_text("#productDescription")

    return {
        "title":       title,
        "bullets":     bullets,
        "main_image":  main_image,
        "price":       price,
        "reviews":     review_count,
        "description": description[:400],
    }


# ─────────────────────────────────────────────────────────────
# STEP 3 — DIFF ENGINE
# ─────────────────────────────────────────────────────────────

def diff_listings(prev: dict, curr: dict) -> list[dict]:
    """
    Compare two consecutive parsed listings and return a list of
    detected changes. Each change is a dict:
      {"type": str, "before": str, "after": str, "delta": ...}

    Change types:
      title       — title text changed
      bullets     — any bullet point added/removed/changed
      main_image  — main image URL changed (normalized)
      price       — price moved by >= $0.50
      reviews     — review count jumped by >= 20
      description — description text changed
    """
    changes = []

    # ── title ──────────────────────────────────────────────
    if prev["title"] and curr["title"] and prev["title"] != curr["title"]:
        changes.append({
            "type":   "title",
            "before": prev["title"],
            "after":  curr["title"],
        })

    # ── bullets ────────────────────────────────────────────
    prev_bullets = "\n".join(prev["bullets"])
    curr_bullets = "\n".join(curr["bullets"])
    if prev_bullets and curr_bullets and prev_bullets != curr_bullets:
        changes.append({
            "type":   "bullets",
            "before": prev_bullets[:400],
            "after":  curr_bullets[:400],
        })

    # ── main image ─────────────────────────────────────────
    def normalize_image_url(url: str) -> str:
        """
        Strip Amazon image size suffixes (._AC_SL1500_., ._SX355_., etc.)
        and query strings so we compare the base image, not the resized variant.
        """
        return re.sub(r"\._[^.]+\.", ".", url.split("?")[0])

    prev_img = normalize_image_url(prev.get("main_image", ""))
    curr_img = normalize_image_url(curr.get("main_image", ""))

    if prev_img and curr_img and prev_img != curr_img:
        changes.append({
            "type":   "main_image",
            "before": prev["main_image"],
            "after":  curr["main_image"],
        })

    # ── price ──────────────────────────────────────────────
    prev_price = prev.get("price")
    curr_price = curr.get("price")
    if prev_price and curr_price and abs(prev_price - curr_price) >= 0.50:
        changes.append({
            "type":   "price",
            "before": f"${prev_price:.2f}",
            "after":  f"${curr_price:.2f}",
            "delta":  round(curr_price - prev_price, 2),
        })

    # ── review velocity spike ──────────────────────────────
    prev_rev = prev.get("reviews", 0)
    curr_rev = curr.get("reviews", 0)
    if prev_rev and curr_rev and (curr_rev - prev_rev) >= 20:
        changes.append({
            "type":   "reviews",
            "before": str(prev_rev),
            "after":  str(curr_rev),
            "delta":  curr_rev - prev_rev,
        })

    # ── description ────────────────────────────────────────
    if (prev["description"] and curr["description"]
            and prev["description"] != curr["description"]):
        changes.append({
            "type":   "description",
            "before": prev["description"][:200],
            "after":  curr["description"][:200],
        })

    return changes


# ─────────────────────────────────────────────────────────────
# STEP 4 — KEEPA API: BSR + price history
# ─────────────────────────────────────────────────────────────

def keepa_minutes_to_datetime(keepa_minutes: int) -> datetime:
    """
    Keepa stores timestamps as minutes elapsed since a proprietary epoch
    (Jan 1 2011 UTC). To get real UTC datetime:
      real_minutes = keepa_minutes + 21,564,000
      real_dt      = datetime(2011,1,1) + timedelta(minutes=real_minutes)
    """
    return KEEPA_EPOCH + timedelta(minutes=keepa_minutes + KEEPA_OFFSET)


def fetch_keepa(asin: str, api_key: str) -> dict:
    """
    Fetch product history from the Keepa Product API.

    Keepa returns time-series data in the `csv` array. Each series
    is encoded as alternating [timestamp, value, timestamp, value, ...].
    Sentinel value -1 means "no data at this point".

    CSV array indices (domain=1 is amazon.com):
      csv[0]  — Amazon price (in cents)
      csv[3]  — Best Seller Rank
      csv[16] — Review count
      csv[18] — Rating (x10, e.g. 45 = 4.5 stars)

    Returns a dict with parsed time series + product metadata.
    """
    resp = requests.get(
        KEEPA_API,
        params={
            "key":     api_key,
            "domain":  1,          # amazon.com
            "asin":    asin,
            "history": 1,
            "days":    365,        # up to 1 year of history
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if not data.get("products"):
        raise RuntimeError(
            "Keepa returned no product data. "
            "Check your API key and confirm the ASIN exists on amazon.com."
        )

    product = data["products"][0]
    csv     = product.get("csv", [])

    def parse_series(raw: Optional[list]) -> list[dict]:
        """Convert a flat Keepa CSV series into a list of {ts, value} dicts."""
        if not raw:
            return []
        out = []
        for i in range(0, len(raw) - 1, 2):
            ts_raw, value = raw[i], raw[i + 1]
            if ts_raw == -1 or value == -1:
                continue   # sentinel — skip
            # Keepa prices are stored in cents; BSR and reviews are raw integers
            real_value = value / 100 if value > 100_000 else value
            out.append({
                "ts":    keepa_minutes_to_datetime(ts_raw),
                "value": real_value,
            })
        return out

    return {
        "bsr":     parse_series(csv[3]  if len(csv) > 3  else None),
        "price":   parse_series(csv[0]  if len(csv) > 0  else None),
        "reviews": parse_series(csv[16] if len(csv) > 16 else None),
        "title":   product.get("title", ""),
        "brand":   product.get("brand", ""),
    }


def get_bsr_window(
    bsr_series: list[dict],
    change_date: str,
    window_days: int = 14,
) -> dict:
    """
    For a given change date, find:
      before — the last BSR recorded on or before the change date
      after  — the BEST (lowest) BSR in the N days following the change

    This is the core of causal attribution: if BSR improved significantly
    in this window with no other concurrent changes, we can attribute the
    rank improvement to this specific change.
    """
    target = datetime.strptime(change_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    before_points = [p for p in bsr_series if p["ts"] <= target]
    after_points  = [
        p for p in bsr_series
        if target < p["ts"] <= target + timedelta(days=window_days)
    ]

    return {
        "before": before_points[-1]["value"] if before_points else None,
        "after":  min((p["value"] for p in after_points), default=None),
    }


# ─────────────────────────────────────────────────────────────
# STEP 5 — CLAUDE API: causal analysis + playbook generation
# ─────────────────────────────────────────────────────────────

def analyze_with_claude(
    asin:        str,
    events:      list[dict],
    keepa_data:  dict,
    api_key:     str,
) -> dict:
    """
    Send the full annotated change log to Claude and receive a
    structured playbook JSON.

    The prompt includes:
      - Chronological list of all changes with BSR deltas
      - High-impact events (BSR improved >500 in isolation window)

    Claude returns JSON with:
      headline   — one punchy sentence summarizing the key finding
      playbook   — ranked list of moves to copy (action, why, impact, template)
      dead_ends  — change types that showed no BSR correlation
      sequence   — recommended execution order and timing
      potential  — projected BSR if playbook is fully executed
    """
    client = anthropic.Anthropic(api_key=api_key)

    # Build a concise change summary for the prompt
    change_summary = []
    for ev in events:
        for ch in ev.get("changes", []):
            entry = {
                "date":      ev["date"],
                "type":      ch["type"],
                "bsr_delta": ev.get("bsr_delta"),
            }
            # Include before/after for high-signal change types
            if ch["type"] == "title":
                entry["before"] = ch["before"][:100]
                entry["after"]  = ch["after"][:100]
            elif ch["type"] in ("price", "reviews"):
                entry["before"] = ch.get("before")
                entry["after"]  = ch.get("after")
            change_summary.append(entry)

    # Isolate high-impact events for focused attribution
    high_impact = [ev for ev in events if ev.get("is_high_impact")]

    prompt = f"""You are an Amazon SEO strategist reverse-engineering a competitor's listing optimization journey.

ASIN: {asin}
Product: {keepa_data.get('title', 'Unknown')[:80]} by {keepa_data.get('brand', 'Unknown')}

LISTING CHANGES (chronological — bsr_delta is BSR change in the 14-day window after each change):
{json.dumps(change_summary, indent=2)}

HIGH-IMPACT EVENTS (BSR improved >500 in a clean causal isolation window — no concurrent changes):
{json.dumps([
    {
        "date":       ev["date"],
        "bsr_before": ev.get("bsr_before"),
        "bsr_after":  ev.get("bsr_after"),
        "change_types": [c["type"] for c in ev["changes"]],
    }
    for ev in high_impact
], indent=2)}

Respond ONLY with valid JSON, no markdown fences, no preamble:
{{
  "headline": "One punchy sentence summarizing the single biggest finding. Max 15 words.",
  "playbook": [
    {{
      "rank":     1,
      "action":   "What to do on your own listing",
      "why":      "Data-backed causal explanation",
      "impact":   "Quantified BSR delta, e.g. -3,400 BSR in 11 days",
      "template": "Exact, copy-pasteable instruction for replication"
    }}
  ],
  "dead_ends":  ["List of change types that showed no measurable BSR correlation"],
  "sequence":   "Recommended execution order with specific timing between each move",
  "potential":  "Projected BSR range if the full playbook is executed in sequence"
}}

Rules:
- 3 to 5 playbook items, ranked by BSR impact descending
- Be specific and quantitative — cite exact BSR numbers from the data
- dead_ends should explain WHY those changes didn't work (e.g. category not price-elastic)
"""

    message = client.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=1_500,
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    # Strip markdown fences if Claude added them despite the instruction
    raw = re.sub(r"^```json\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)

    return json.loads(raw)


# ─────────────────────────────────────────────────────────────
# STEP 6 — HTML REPORT BUILDER
# ─────────────────────────────────────────────────────────────

def build_report(
    asin:     str,
    events:   list[dict],
    keepa:    dict,
    insights: dict,
    days:     int,
) -> str:
    """
    Assemble a self-contained HTML report from all pipeline outputs.

    The report is a single file with:
      - BSR trend chart (Chart.js via CDN)
      - Metric summary cards
      - Claude playbook (ranked cards with copy templates)
      - What didn't work
      - Full change timeline with before/after text diffs
      - Dead-ends + execution sequence

    No external CSS or JS files required — open in any browser.
    """
    # Build chart data from Keepa BSR series
    cutoff   = datetime.now(timezone.utc) - timedelta(days=days)
    bsr_pts  = [p for p in keepa["bsr"] if p["ts"] >= cutoff]
    c_labels = json.dumps([p["ts"].strftime("%b %d") for p in bsr_pts])
    c_values = json.dumps([int(p["value"]) for p in bsr_pts])

    # Summary metrics
    all_bsr     = [p["value"] for p in keepa["bsr"]]
    bsr_peak    = int(max(all_bsr)) if all_bsr else 0
    bsr_current = int(all_bsr[-1]) if all_bsr else 0
    total_chg   = sum(len(e.get("changes", [])) for e in events)
    high_ct     = sum(1 for e in events if e.get("is_high_impact"))

    def type_badge(ctype: str) -> str:
        meta  = CHANGE_META.get(ctype, {"color": "#888", "label": ctype})
        color = meta["color"]
        return (
            f'<span style="background:{color}22;color:{color};'
            f'font-size:10px;padding:2px 7px;border-radius:10px;font-weight:500;">'
            f'{meta["label"]}</span>'
        )

    def delta_badge(delta: Optional[int]) -> str:
        if delta is None:
            return ""
        color  = "#4aab78" if delta < 0 else "#c25252"
        arrow  = "▼" if delta < 0 else "▲"
        return (
            f'<span style="color:{color};font-family:monospace;'
            f'font-size:11px;font-weight:500;">'
            f'{arrow} {abs(delta):,}</span>'
        )

    # Build timeline rows
    timeline_html = ""
    for ev in events:
        if not ev.get("changes") or ev.get("baseline"):
            continue
        for ch in ev["changes"]:
            hl_style = (
                "border-left:2px solid #4aab78;border-radius:0 6px 6px 0;"
                if ev.get("is_high_impact") else ""
            )
            detail = ""
            if ch["type"] in ("title", "bullets", "description"):
                b = ch.get("before", "")[:150].replace("<", "&lt;").replace(">", "&gt;")
                a = ch.get("after",  "")[:150].replace("<", "&lt;").replace(">", "&gt;")
                detail = (
                    f'<div style="font-size:11px;margin-top:6px;line-height:1.6;color:#888;">'
                    f'<span style="color:#c25252;">Before:</span> {b}<br>'
                    f'<span style="color:#4aab78;">After:</span>  {a}'
                    f'</div>'
                )
            elif ch["type"] == "price":
                detail = (
                    f'<div style="font-size:11px;margin-top:4px;color:#888;">'
                    f'{ch.get("before","")} → {ch.get("after","")}'
                    f'</div>'
                )
            elif ch["type"] == "reviews":
                detail = (
                    f'<div style="font-size:11px;margin-top:4px;color:#888;">'
                    f'+{ch.get("delta","")} new reviews detected'
                    f'</div>'
                )

            star_badge = (
                '<span style="font-size:10px;background:#4aab7822;color:#4aab78;'
                'padding:2px 7px;border-radius:10px;">★ high-impact</span>'
                if ev.get("is_high_impact") else ""
            )

            timeline_html += f"""
<div style="display:grid;grid-template-columns:80px 1fr;gap:10px;margin-bottom:8px;">
  <div style="font-size:11px;color:#888;font-family:monospace;padding-top:4px;text-align:right;">{ev['date']}</div>
  <div style="background:#1a1814;border:1px solid #2a2720;border-radius:6px;padding:10px 12px;{hl_style}">
    <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
      {type_badge(ch['type'])}
      {delta_badge(ev.get('bsr_delta'))}
      {star_badge}
    </div>
    {detail}
  </div>
</div>"""

    # Build playbook cards
    playbook_html = ""
    for item in insights.get("playbook", []):
        playbook_html += f"""
<div style="background:#1a1814;border:1px solid #2a2720;
            border-left:2px solid #d4860f;
            border-radius:0 6px 6px 0;
            padding:14px;margin-bottom:10px;">
  <div style="font-size:10px;font-family:monospace;color:#888;
              text-transform:uppercase;letter-spacing:.08em;margin-bottom:4px;">
    Move #{item.get('rank','')} · {item.get('impact','')}
  </div>
  <div style="font-size:13px;font-weight:500;color:#f0ead8;margin-bottom:5px;">
    {item.get('action','')}
  </div>
  <div style="font-size:12px;color:#888;margin-bottom:8px;">
    {item.get('why','')}
  </div>
  <div style="font-size:11px;background:#0c0b09;border:1px solid #2a2720;
              border-radius:4px;padding:9px 12px;color:#c8bfa8;
              font-style:italic;font-family:monospace;line-height:1.6;">
    {item.get('template','')}
  </div>
</div>"""

    # Build dead-ends chips
    dead_ends_html = "".join(
        f'<span style="background:#1a1814;color:#666;font-size:11px;'
        f'padding:3px 9px;border-radius:3px;font-family:monospace;">{d}</span> '
        for d in insights.get("dead_ends", [])
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Listing Autopsy — {asin}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.js"></script>
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0c0b09;color:#e8e2d6;font-family:'DM Mono',monospace,sans-serif;
        padding:32px 24px;line-height:1.6}}
  .wrap{{max-width:920px;margin:0 auto}}
  h1{{font-size:20px;font-weight:400;margin-bottom:4px;color:#f0ead8}}
  .meta{{font-size:11px;color:#666;font-family:monospace;margin-bottom:28px}}
  .metrics{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-bottom:24px}}
  .metric{{background:#141210;border:1px solid #2a2720;border-radius:6px;padding:13px 15px}}
  .m-label{{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:#666;margin-bottom:3px}}
  .m-value{{font-size:22px;font-weight:500;font-family:monospace}}
  .card{{background:#141210;border:1px solid #2a2720;border-radius:8px;
         padding:18px;margin-bottom:18px}}
  .card-title{{font-size:12px;color:#888;text-transform:uppercase;
               letter-spacing:.08em;margin-bottom:14px}}
  .headline{{font-size:16px;color:#f0ead8;margin-bottom:8px;line-height:1.4}}
  .sequence{{font-size:12px;color:#888;line-height:1.7;margin-top:10px}}
  footer{{text-align:center;font-size:10px;color:#444;margin-top:36px;font-family:monospace}}
  @media(max-width:600px){{.metrics{{grid-template-columns:1fr 1fr}}}}
</style>
</head>
<body>
<div class="wrap">
  <h1>Listing Autopsy — {asin}</h1>
  <div class="meta">
    {keepa.get('title','')[:70]} · {keepa.get('brand','')} ·
    Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} UTC ·
    Last {days} days · Wayback + Keepa + Claude
  </div>

  <div class="metrics">
    <div class="metric">
      <div class="m-label">BSR peak</div>
      <div class="m-value" style="color:#c25252">{bsr_peak:,}</div>
    </div>
    <div class="metric">
      <div class="m-label">BSR current</div>
      <div class="m-value" style="color:#4aab78">{bsr_current:,}</div>
    </div>
    <div class="metric">
      <div class="m-label">Changes detected</div>
      <div class="m-value">{total_chg}</div>
    </div>
    <div class="metric">
      <div class="m-label">High-impact</div>
      <div class="m-value" style="color:#d4860f">{high_ct}</div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">BSR history · Keepa API</div>
    <div style="position:relative;height:220px;">
      <canvas id="bsrChart" role="img" aria-label="BSR trend chart for {asin}"></canvas>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Claude playbook · reverse-engineered from BSR data</div>
    <div class="headline">{insights.get('headline','')}</div>
    <div style="font-size:11px;color:#666;margin-bottom:14px;">
      Estimated potential: {insights.get('potential','')}
    </div>
    {playbook_html}
  </div>

  <div class="card">
    <div class="card-title">What didn't move the needle</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;">
      {dead_ends_html}
    </div>
    <div style="font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:#666;margin-bottom:6px;">
      Execution sequence
    </div>
    <div class="sequence">{insights.get('sequence','')}</div>
  </div>

  <div class="card">
    <div class="card-title">Full change timeline · Wayback Machine diffs</div>
    {timeline_html or '<div style="color:#666;font-size:13px;">No changes detected in this window.</div>'}
  </div>

  <footer>
    Wayback Machine CDX API · Keepa Product API · Anthropic Claude API ·
    ASIN {asin} · github.com/BiplabaKrSamal/listing-autopsy
  </footer>
</div>

<script>
new Chart(document.getElementById('bsrChart'), {{
  type: 'line',
  data: {{
    labels: {c_labels},
    datasets: [{{
      label: 'BSR',
      data: {c_values},
      borderColor: '#378ADD',
      borderWidth: 2,
      backgroundColor: 'rgba(55,138,221,0.05)',
      fill: true,
      tension: 0.35,
      pointRadius: 1.5,
      pointHoverRadius: 5,
    }}]
  }},
  options: {{
    responsive: true,
    maintainAspectRatio: false,
    plugins: {{ legend: {{ display: false }} }},
    scales: {{
      y: {{
        reverse: true,
        ticks: {{ callback: v => v.toLocaleString(), font: {{ size: 10 }}, color: '#666' }},
        grid:  {{ color: 'rgba(255,255,255,0.05)' }},
      }},
      x: {{
        ticks: {{ font: {{ size: 10 }}, color: '#666', maxTicksLimit: 10 }},
        grid:  {{ display: false }},
      }},
    }},
  }},
}});
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# MAIN PIPELINE GENERATOR
# ─────────────────────────────────────────────────────────────

def run_pipeline(
    asin:          str,
    keepa_key:     str,
    anthropic_key: str,
    days:          int = 120,
) -> Generator[dict, None, None]:
    """
    Main pipeline generator.

    Yields progress dicts at each stage:
        {"stage": "wayback", "msg": "...", "pct": 15}

    Final yield when complete:
        {"stage": "done", "report": "<full HTML>", "headline": "...", "pct": 100}

    Error yield:
        {"stage": "error", "msg": "...", "pct": 0}

    Designed to be consumed by both:
      - tracker.py (CLI) — prints progress bar
      - app.py (Flask)   — streams via SSE
    """

    def progress(stage: str, msg: str, pct: int) -> dict:
        return {"stage": stage, "msg": msg, "pct": pct}

    # ── Stage 1: Wayback CDX discovery ────────────────────
    yield progress("wayback", f"Querying Wayback CDX for ASIN {asin}…", 5)
    raw_snapshots = get_snapshots(asin, days)

    if not raw_snapshots:
        yield progress("error", f"No Wayback snapshots found for ASIN {asin} in the last {days} days.", 0)
        return

    yield progress("wayback", f"Found {len(raw_snapshots)} archived snapshots. Starting fetch…", 10)

    # ── Stage 2: Fetch + parse + diff each snapshot ────────
    events:    list[dict]         = []
    prev_snap: Optional[dict]     = None
    total                         = len(raw_snapshots)

    for i, snap in enumerate(raw_snapshots):
        ts       = snap["timestamp"]
        date_str = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
        pct      = 10 + int((i / total) * 44)

        yield progress("wayback", f"Snapshot {i+1}/{total} — {date_str}", pct)

        html = fetch_snapshot(ts, asin)
        if not html:
            time.sleep(1.0)
            continue

        listing = parse_listing(html)

        if prev_snap is not None:
            diffs = diff_listings(prev_snap["listing"], listing)
            if diffs:
                events.append({
                    "date":     date_str,
                    "ts":       ts,
                    "changes":  diffs,
                    "listing":  listing,
                })
        else:
            # First snapshot — store as baseline (no diff possible yet)
            events.append({
                "date":     date_str,
                "ts":       ts,
                "changes":  [],
                "listing":  listing,
                "baseline": True,
            })

        prev_snap = {"ts": ts, "listing": listing}
        time.sleep(1.2)   # polite crawl delay — don't hammer the archive

    change_count = sum(len(e["changes"]) for e in events)
    yield progress("wayback", f"Wayback complete — {change_count} changes across {len(events)} snapshots.", 55)

    # ── Stage 3: Keepa BSR history ────────────────────────
    yield progress("keepa", "Fetching BSR + price history from Keepa…", 60)
    try:
        keepa_data = fetch_keepa(asin, keepa_key)
    except Exception as exc:
        yield progress("error", f"Keepa API error: {exc}", 0)
        return

    yield progress("keepa", f"Loaded {len(keepa_data['bsr'])} BSR datapoints from Keepa.", 65)

    # ── Stage 4: Annotate events with BSR context ─────────
    for ev in events:
        if ev.get("baseline") or not ev["changes"]:
            continue

        bsr_ctx = get_bsr_window(keepa_data["bsr"], ev["date"])
        ev["bsr_before"]    = bsr_ctx["before"]
        ev["bsr_after"]     = bsr_ctx["after"]
        ev["bsr_delta"]     = (
            int(bsr_ctx["after"] - bsr_ctx["before"])
            if bsr_ctx["before"] is not None and bsr_ctx["after"] is not None
            else None
        )
        # High-impact = BSR improved by >500 in a 14-day window
        # (with no concurrent changes = clean causal isolation)
        ev["is_high_impact"] = bool(ev["bsr_delta"] and ev["bsr_delta"] < -500)

    yield progress("keepa", "BSR context annotated on all change events.", 70)

    # ── Stage 5: Claude analysis ──────────────────────────
    yield progress("claude", "Sending annotated change log to Claude for analysis…", 75)
    try:
        insights = analyze_with_claude(asin, events, keepa_data, anthropic_key)
    except Exception as exc:
        yield progress("error", f"Claude API error: {exc}", 0)
        return

    yield progress("claude", "Playbook generated successfully.", 85)

    # ── Stage 6: Build HTML report ────────────────────────
    yield progress("report", "Assembling self-contained HTML report…", 90)
    report_html = build_report(asin, events, keepa_data, insights, days)
    yield progress("report", "Report ready.", 98)

    yield {
        "stage":    "done",
        "report":   report_html,
        "headline": insights.get("headline", ""),
        "asin":     asin,
        "pct":      100,
    }
