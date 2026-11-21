"""FM27 Tactic Lab — server.

Locally:  python app.py   then open http://127.0.0.1:8765
On a server: gunicorn app:app (see README, "Deploy").
The dashboard's "Update" button calls POST /api/refresh, which scrapes every source in a background thread;
the page then polls GET /api/refresh-status until it is done.
"""
import hmac
import json
import os
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import scrapers

ROOT = Path(__file__).parent
DATA = ROOT / "data" / "tactics.json"
MANUAL = ROOT / "data" / "manual.csv"
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", 8765))  # hosting platforms set PORT themselves
# On a public server set REFRESH_TOKEN: Update then needs this token. Without it, Update only works from this computer.
REFRESH_TOKEN = os.environ.get("REFRESH_TOKEN", "")

app = Flask(__name__, static_folder=None)
app.json.sort_keys = False  # keep sources in declaration order
_state = {"running": False, "error": None, "finished_at": None}  # one update at a time; one worker process only
_state_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load():
    if DATA.exists():
        return json.loads(DATA.read_text(encoding="utf-8"))
    return {"updated_at": None, "sources": {}, "records": []}


def _timed(fn):
    t = time.time()
    return fn(), round(time.time() - t, 1)


def refresh():
    old = load()
    scrapers.PREVIOUS = {(r["source"], r["url"]): r for r in old["records"] if r.get("url")}
    result = {"updated_at": now(), "sources": {}, "records": []}
    with ThreadPoolExecutor(len(scrapers.SOURCES)) as ex:
        futures = {key: ex.submit(_timed, fn) for key, (_, fn) in scrapers.SOURCES.items()}
    for key, fut in futures.items():
        label = scrapers.SOURCES[key][0]
        try:
            recs, secs = fut.result()
            info = {"status": "ok", "count": len(recs), "seconds": secs, "fetched_at": now()}
        except scrapers.Blocked as e:
            recs, info = [], {"status": "blocked", "message": str(e)}
        except scrapers.Skipped as e:
            recs, info = [], {"status": "skipped", "message": str(e)}
        except Exception as e:  # network error / site layout changed: keep the previous data
            recs = [r for r in old["records"] if r["source"] == key and r.get("origin") != "manual"]
            prev = old["sources"].get(key, {})
            info = {"status": "error", "message": f"{type(e).__name__}: {e}", "count": len(recs),
                    "fetched_at": prev.get("fetched_at")}
        result["sources"][key] = {"label": label, **info}
        result["records"] += recs
    try:
        manual = scrapers.manual(MANUAL)
        result["sources"]["manual"] = {"label": "Manual entry (CSV)", "status": "ok", "count": len(manual),
                                       "fetched_at": now()}
        result["records"] += manual
    except Exception as e:
        result["sources"]["manual"] = {"label": "Manual entry (CSV)", "status": "error", "message": str(e)}
    DATA.parent.mkdir(exist_ok=True)
    DATA.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


@app.get("/")
def index():
    return send_from_directory(ROOT / "static", "index.html")


@app.get("/api/data")
def api_data():
    return jsonify(load())


def _refresh_in_background():
    error = None
    try:
        refresh()
    except Exception as e:  # keep the server alive; the page shows the error
        error = f"{type(e).__name__}: {e}"
    with _state_lock:
        _state.update(running=False, error=error, finished_at=now())


def _allowed():
    """An update runs the scrapers, so a public server must not let anyone start one."""
    if REFRESH_TOKEN:
        if hmac.compare_digest(request.headers.get("X-Refresh-Token", ""), REFRESH_TOKEN):
            return None
        return jsonify({"error": "Wrong or missing refresh token"}), 401
    if request.remote_addr in ("127.0.0.1", "::1"):
        return None
    return jsonify({"error": "Update is disabled on this server (set REFRESH_TOKEN)"}), 403


@app.post("/api/refresh")
def api_refresh():
    denied = _allowed()
    if denied:
        return denied
    with _state_lock:
        if _state["running"]:
            return jsonify({"error": "Another update is already running"}), 409
        _state.update(running=True, error=None)
    threading.Thread(target=_refresh_in_background, daemon=True).start()
    return jsonify({"running": True}), 202


@app.get("/api/refresh-status")
def api_refresh_status():
    with _state_lock:
        return jsonify(dict(_state))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if "--refresh" in sys.argv:  # update from the command line without starting the server
        data = refresh()
        for k, s in data["sources"].items():
            print(f"{s['label']:16} {s['status']:8} {s.get('count', '')} {s.get('message', '')}")
        sys.exit()
    if HOST == "127.0.0.1":  # only on this computer
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{PORT}")).start()
    app.run(host=HOST, port=PORT, threaded=True)
