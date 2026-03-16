"""
Target Ad Scraper — Flask Web UI
Run:  python app.py   ->   open http://localhost:5000
"""

import csv
import io
import json
import os
import threading
from dataclasses import asdict
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from scraper import scrape, scrape_contacts

app = Flask(__name__)

# ── Batch state ────────────────────────────────────────────────────────────────

RESULTS_FILE = Path("batch_results.json")

_batch_lock = threading.Lock()
_batch_stop = threading.Event()

_batch_state = {
    "status":          "idle",   # idle | running | done | stopped
    "keywords":        [],
    "completed":       0,        # keywords fully processed
    "current_keyword": "",
    "current_phase":   "",       # scraping | contacts
    "results":         [],       # [{search_term, brand, website, emails, phones}]
    "errors":          [],       # [{keyword, error}]
}


def _load_saved():
    if RESULTS_FILE.exists():
        try:
            data = json.loads(RESULTS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            pass
    return []


def _persist(results):
    try:
        RESULTS_FILE.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass


def _batch_worker(keywords):
    for kw in keywords:
        if _batch_stop.is_set():
            break

        with _batch_lock:
            _batch_state["current_keyword"] = kw
            _batch_state["current_phase"]   = "scraping"

        try:
            ads = scrape(kw)

            # Deduplicate by brand_href (fall back to brand name)
            seen = {}
            for ad in ads:
                key = ad.brand_href or ad.brand
                if key not in seen:
                    seen[key] = ad
            unique_ads = list(seen.values())

            for ad in unique_ads:
                if _batch_stop.is_set():
                    break

                with _batch_lock:
                    _batch_state["current_phase"] = "contacts"

                contacts = {}
                if ad.website:
                    try:
                        contacts = scrape_contacts(ad.website)
                    except Exception:
                        contacts = {}

                row = {
                    "search_term": kw,
                    "brand":       ad.brand,
                    "website":     ad.website or "",
                    "emails":      ", ".join(contacts.get("emails", [])),
                    "phones":      ", ".join(contacts.get("phones", [])),
                }

                with _batch_lock:
                    _batch_state["results"].append(row)
                    _persist(_batch_state["results"])

        except Exception as exc:
            with _batch_lock:
                _batch_state["errors"].append({"keyword": kw, "error": str(exc)})

        with _batch_lock:
            _batch_state["completed"] += 1

    with _batch_lock:
        _batch_state["status"]          = "stopped" if _batch_stop.is_set() else "done"
        _batch_state["current_keyword"] = ""
        _batch_state["current_phase"]   = ""


# Preload any results saved from a previous run
with _batch_lock:
    _batch_state["results"] = _load_saved()


# ── Single-search routes ───────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/scrape", methods=["POST"])
def run_scrape():
    term = (request.json or {}).get("term", "").strip()
    if not term:
        return jsonify({"error": "Search term is required."}), 400
    try:
        ads = scrape(term)
        return jsonify({"ads": [asdict(ad) for ad in ads]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/contacts", methods=["POST"])
def get_contacts():
    website = (request.json or {}).get("website", "").strip()
    if not website:
        return jsonify({"emails": [], "phones": []})
    try:
        result = scrape_contacts(website)
        return jsonify(result)
    except Exception as e:
        return jsonify({"emails": [], "phones": [], "error": str(e)})


@app.route("/download", methods=["POST"])
def download():
    rows = (request.json or {}).get("rows", [])
    if not rows:
        return jsonify({"error": "No results to download."}), 400

    fieldnames = ["brand", "website", "emails", "phones"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=sponsored_ads.csv"},
    )


# ── Batch routes ───────────────────────────────────────────────────────────────

@app.route("/batch/start", methods=["POST"])
def batch_start():
    with _batch_lock:
        if _batch_state["status"] == "running":
            return jsonify({"error": "A batch is already running."}), 400

    payload  = request.json or {}
    raw      = payload.get("keywords", [])
    keywords = [str(k).strip() for k in raw if str(k).strip()]

    if not keywords:
        return jsonify({"error": "No keywords provided."}), 400

    clear = bool(payload.get("clear", False))

    with _batch_lock:
        _batch_stop.clear()
        _batch_state["status"]          = "running"
        _batch_state["keywords"]        = keywords
        _batch_state["completed"]       = 0
        _batch_state["current_keyword"] = ""
        _batch_state["current_phase"]   = ""
        _batch_state["errors"]          = []
        if clear:
            _batch_state["results"] = []

    threading.Thread(target=_batch_worker, args=(keywords,), daemon=True).start()
    return jsonify({"ok": True, "total": len(keywords)})


@app.route("/batch/stop", methods=["POST"])
def batch_stop():
    _batch_stop.set()
    return jsonify({"ok": True})


@app.route("/batch/status", methods=["GET"])
def batch_status():
    with _batch_lock:
        s = _batch_state
        return jsonify({
            "status":          s["status"],
            "total":           len(s["keywords"]),
            "completed":       s["completed"],
            "current_keyword": s["current_keyword"],
            "current_phase":   s["current_phase"],
            "results":         list(s["results"]),
            "errors":          list(s["errors"]),
        })


@app.route("/batch/download", methods=["GET"])
def batch_download():
    with _batch_lock:
        rows = list(_batch_state["results"])

    if not rows:
        return Response("No results yet.", status=204)

    fieldnames = ["search_term", "brand", "website", "emails", "phones"]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=batch_results.csv"},
    )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, port=port, threaded=True)
