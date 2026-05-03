# Listing Autopsy

**Reverse-engineer any Amazon competitor's full listing optimization journey.**

Scrapes the Wayback Machine for every archived version of a competitor's listing,
maps each change against Keepa's hourly BSR data, and runs the full annotated log
through Claude to identify *exactly* which changes caused rank jumps — and generates
a copy-ready playbook for your own listing.

> "They changed their main image on March 3rd. BSR went from 4,200 → 800 in 11 days."

---

## APIs used

| # | API | What it provides | Cost |
|---|-----|-----------------|------|
| 1 | **Wayback Machine CDX** | Every archived snapshot of the listing — title, bullets, images, price over time | Free, no key |
| 2 | **Keepa Product API** | Hourly BSR, price, and review count history | Free tier: 250 tokens/day · Paid: $19/mo |
| 3 | **Anthropic Claude API** | Causal analysis + ranked playbook generation | Pay per use |

---

## Project structure

```
listing-autopsy/
├── pipeline.py          ← core engine (Wayback + Keepa + Claude)
├── app.py               ← Flask web server with SSE streaming
├── tracker.py           ← standalone CLI script
├── templates/
│   └── index.html       ← web UI (form + live progress)
├── reports/             ← auto-created, stores generated reports
├── requirements.txt
├── Procfile             ← Railway / Render deploy
├── .env.example
└── README.md
```

---

## Step-by-step setup

### Step 1 — Clone the repo

```bash
git clone https://github.com/BiplabaKrSamal/listing-autopsy
cd listing-autopsy
```

### Step 2 — Install dependencies

Requires **Python 3.10+**.

```bash
pip install -r requirements.txt
```

Packages installed:
- `anthropic` — Claude API client
- `requests` — HTTP (Wayback + Keepa)
- `beautifulsoup4` + `lxml` — HTML parsing
- `flask` — web server
- `python-dotenv` — `.env` file loading

### Step 3 — Get API keys

**Keepa API key**
1. Go to [keepa.com/api](https://keepa.com/api)
2. Create a free account
3. Free tier: 250 tokens/day (~5 product lookups)
4. Paid: $19/month for 20,000 tokens/month

**Anthropic API key**
1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Create an account
3. Settings → API keys → Create key

### Step 4 — Configure keys

```bash
cp .env.example .env
```

Edit `.env`:
```
ANTHROPIC_API_KEY=sk-ant-YOUR_KEY
KEEPA_API_KEY=YOUR_KEEPA_KEY
```

Or pass keys inline (no `.env` file needed):
```bash
ANTHROPIC_API_KEY=sk-ant-... KEEPA_API_KEY=... python tracker.py --asin B08X3K9PLM
```

---

## Running the project

### Option A — Web app (recommended for judging)

```bash
python app.py
```

Open [http://localhost:5000](http://localhost:5000)

1. Enter an Amazon ASIN
2. Paste your Keepa and Anthropic API keys
3. Select a history window (120 days recommended)
4. Click **Run analysis →**
5. Watch the live progress stream
6. Click **Open report →** when complete

The report opens as a self-contained HTML file at `/report/<id>`.

### Option B — CLI (headless)

```bash
# Basic run
python tracker.py --asin B08X3K9PLM

# Custom window
python tracker.py --asin B08X3K9PLM --days 90

# Custom output filename
python tracker.py --asin B08X3K9PLM --output my_competitor.html

# Skip Wayback (Keepa + Claude only — much faster, no text diffs)
python tracker.py --asin B08X3K9PLM --skip-wayback

# Keys as flags instead of .env
python tracker.py --asin B08X3K9PLM --keepa-key YOUR_KEY

# Help
python tracker.py --help
```

Output: `report_<ASIN>.html` — open in any browser.

### Option C — Batch + cron

Track multiple competitors:
```bash
for asin in B08X3K9PLM B09ABC1234 B07XYZ5678; do
  python tracker.py --asin $asin --days 90
done
```

Weekly automation (crontab):
```
# Every Monday at 8am
0 8 * * 1  cd /path/to/listing-autopsy && python tracker.py --asin B08X3K9PLM --days 7
```

---

## What the pipeline does

| Step | API | What happens |
|------|-----|-------------|
| 1 | Wayback CDX | Discovers up to 150 archived snapshots of `amazon.com/dp/ASIN` |
| 2 | Wayback Replay | Fetches each snapshot via `if_` URL, parses HTML with BeautifulSoup |
| 3 | Diff engine | Diffs consecutive snapshots: title, bullets, image URL, price, reviews |
| 4 | Keepa | Pulls hourly BSR, price, review history (up to 1 year) |
| 5 | BSR annotation | For each change: measures BSR 14 days before vs after, flags high-impact |
| 6 | Claude | Receives full annotated log, returns ranked playbook JSON |
| 7 | Report builder | Assembles self-contained HTML report with Chart.js BSR chart |

### Causal attribution logic

A change is marked **high-impact** when:
1. BSR improved by more than 500 ranks in the 14-day window after the change
2. No other listing changes occurred in that same window

This "causal isolation window" approach isn't perfect — BSR also responds to
ad spend, external traffic, and seasonality — but an isolated window with a
single change and a 500+ rank improvement is a strong causal signal.

---

## Output report

The generated `report_ASIN.html` contains:

- **BSR trend chart** — full Keepa history with Chart.js
- **Metric summary** — BSR peak, current, total changes, high-impact count
- **Claude playbook** — ranked moves with copy templates
- **What didn't work** — change types with no BSR correlation
- **Execution sequence** — recommended order and timing
- **Full change timeline** — before/after text diffs for every detected change

The report is fully self-contained — one `.html` file, no server needed.

---

## Deploying for your submission link

### GitHub Pages (landing page — fastest)

```bash
git init
git add .
git commit -m "Listing Autopsy — Wayback + Keepa + Claude"
git remote add origin https://github.com/BiplabaKrSamal/listing-autopsy.git
git push -u origin main
```

Then: Settings → Pages → Source: main branch / root → Save

**Submission link:** `https://github.com/BiplabaKrSamal/listing-autopsy`

### Render (full Flask app — free tier)

1. [render.com](https://render.com) → New → Web Service
2. Connect your GitHub repo
3. Build command: `pip install -r requirements.txt`
4. Start command: `python app.py`
5. Environment → Add: `ANTHROPIC_API_KEY` and `KEEPA_API_KEY`

Note: Free tier cold-starts after 15 min idle (~30s first request). Upgrade to $7/mo for always-on.

### Railway (one command)

```bash
railway login
railway init
railway up
# Add env vars in Railway dashboard → Variables
```

---

## Limitations

- **Wayback coverage** varies. Popular ASINs have daily snapshots; newer
  products may have weekly or monthly gaps.
- **Amazon DOM changes** break parsers. CSS selectors target the 2024–2026
  Amazon layout. Update `parse_listing()` if Amazon redesigns the page.
- **Image diffing** compares URL strings only — we detect *that* the image
  changed but can't visually compare the before/after images.
- **BSR attribution** is correlational. Ad spend, coupons, and external
  traffic can cause BSR moves that aren't explained by listing changes.

*Built by [Biplaba Kumar Samal](https://github.com/BiplabaKrSamal) · 2026*
