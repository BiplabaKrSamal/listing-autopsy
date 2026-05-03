"""
╔══════════════════════════════════════════════════════════════╗
║  app.py — Flask web server                                   ║
║                                                              ║
║  Routes:                                                     ║
║    GET  /              → landing page (templates/index.html) ║
║    POST /analyze       → start job, return {job_id}          ║
║    GET  /progress/:id  → SSE stream of progress events       ║
║    GET  /report/:id    → serve completed HTML report file    ║
║    GET  /health        → {"status": "ok"}                    ║
║                                                              ║
║  Run:                                                        ║
║    python app.py                                             ║
║    → http://localhost:5000                                   ║
╚══════════════════════════════════════════════════════════════╝
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request, send_from_directory

from pipeline import run_pipeline

# ─────────────────────────────────────────────────────────────
# SETUP
# ─────────────────────────────────────────────────────────────

load_dotenv()   # reads .env file if present

app = Flask(__name__)

# All generated reports are saved here as {uuid}.html
REPORTS_DIR = Path("reports")
REPORTS_DIR.mkdir(exist_ok=True)

# In-memory job registry
# Schema: {job_id: {"events": list, "done": bool, "report_id": str|None, "error": str|None}}
_jobs: dict[str, dict] = {}
_lock = threading.Lock()   # protects _jobs from concurrent read/write


# ─────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    """Serve the landing page + analysis form."""
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    """
    Start a pipeline job in a background thread.

    Accepts JSON body or form data:
        asin          — Amazon ASIN (required)
        keepa_key     — Keepa API key (falls back to KEEPA_API_KEY env var)
        anthropic_key — Anthropic API key (falls back to ANTHROPIC_API_KEY env var)
        days          — history window in days (default: 120)

    Returns immediately with:
        {"job_id": "uuid-string"}

    Clients poll /progress/:id via EventSource to stream live updates.
    """
    data          = request.get_json(silent=True) or request.form
    asin          = (data.get("asin") or "").strip().upper()
    keepa_key     = data.get("keepa_key")     or os.getenv("KEEPA_API_KEY", "")
    anthropic_key = data.get("anthropic_key") or os.getenv("ANTHROPIC_API_KEY", "")
    days          = int(data.get("days") or 120)

    # ── validation ─────────────────────────────────────────
    if not asin:
        return jsonify({"error": "ASIN is required"}), 400
    if not keepa_key:
        return jsonify({"error": "Keepa API key is required (or set KEEPA_API_KEY env var)"}), 400
    if not anthropic_key:
        return jsonify({"error": "Anthropic API key is required (or set ANTHROPIC_API_KEY env var)"}), 400

    # ── create job slot ────────────────────────────────────
    job_id = str(uuid.uuid4())
    with _lock:
        _jobs[job_id] = {
            "events":    [],
            "done":      False,
            "report_id": None,
            "error":     None,
        }

    # ── start pipeline in background thread ────────────────
    def _run_job():
        try:
            for event in run_pipeline(asin, keepa_key, anthropic_key, days):
                with _lock:
                    _jobs[job_id]["events"].append(event)

                if event.get("stage") == "done":
                    # Save the report HTML to disk
                    report_id = str(uuid.uuid4())
                    report_path = REPORTS_DIR / f"{report_id}.html"
                    report_path.write_text(event["report"], encoding="utf-8")
                    with _lock:
                        _jobs[job_id]["done"]      = True
                        _jobs[job_id]["report_id"] = report_id

                elif event.get("stage") == "error":
                    with _lock:
                        _jobs[job_id]["done"]  = True
                        _jobs[job_id]["error"] = event.get("msg", "Unknown error")

        except Exception as exc:
            with _lock:
                _jobs[job_id]["done"]  = True
                _jobs[job_id]["error"] = str(exc)

    thread = threading.Thread(target=_run_job, daemon=True)
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id: str):
    """
    Server-Sent Events (SSE) stream for a running job.

    The browser connects here with:
        const es = new EventSource(`/progress/${jobId}`);
        es.onmessage = e => { const ev = JSON.parse(e.data); ... }

    Each event is a JSON-serialized progress dict:
        {"stage": "wayback", "msg": "...", "pct": 25}

    When the job finishes, one final event is sent with report_id:
        {"stage": "done", "pct": 100, "report_id": "uuid"}

    The stream closes automatically after the "done" event.
    """
    def generate():
        sent = 0   # track how many events we've already sent this connection

        while True:
            with _lock:
                job = _jobs.get(job_id)
                if not job:
                    yield f"data: {json.dumps({'stage': 'error', 'msg': 'Job not found'})}\n\n"
                    return

                # Collect new events since last iteration
                new_events = job["events"][sent:]
                is_done    = job["done"]
                report_id  = job["report_id"]

            for event in new_events:
                # Strip the full HTML from the payload (too large for SSE)
                payload = {k: v for k, v in event.items() if k != "report"}
                # Attach report_id once it's available so the client can redirect
                if report_id:
                    payload["report_id"] = report_id
                yield f"data: {json.dumps(payload)}\n\n"

            sent += len(new_events)

            if is_done:
                return   # close the SSE stream

            time.sleep(0.4)   # poll interval

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control":    "no-cache",
            "X-Accel-Buffering": "no",   # disable nginx buffering
        },
    )


@app.route("/report/<report_id>")
def report(report_id: str):
    """
    Serve a completed report HTML file.

    report_id is a UUID4. We sanitize it to only allow hex chars + hyphens
    to prevent path traversal attacks.
    """
    safe_id = "".join(c for c in report_id if c in "0123456789abcdef-")
    path    = REPORTS_DIR / f"{safe_id}.html"

    if not path.exists():
        return "Report not found. It may have expired or the ID is incorrect.", 404

    return send_from_directory(REPORTS_DIR, f"{safe_id}.html")


@app.route("/health")
def health():
    """Health check endpoint for Render / Railway deploy monitors."""
    return jsonify({"status": "ok", "jobs_in_memory": len(_jobs)})


# ─────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"

    print(f"\n{'─'*56}")
    print(f"  Listing Autopsy")
    print(f"  http://localhost:{port}")
    print(f"  Reports dir: {REPORTS_DIR.resolve()}")
    print(f"{'─'*56}\n")

    app.run(
        host="0.0.0.0",
        port=port,
        debug=debug,
        threaded=True,   # required — each SSE stream needs its own thread
    )
