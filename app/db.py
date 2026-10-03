import json
import os
import sqlite3
import time
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
CONFIGS = Path(os.environ.get("CONFIG_DIR", "/configs"))


def conn():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DATA / "results.db")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started REAL, mode TEXT,
            baseline TEXT, status TEXT, finished REAL);
        CREATE TABLE IF NOT EXISTS results (run_id INTEGER, name TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


def get_setting(key, default):
    with conn() as c:
        row = c.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return json.loads(row["v"]) if row else default


def set_setting(key, value):
    with conn() as c:
        c.execute("REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))


def new_run(mode):
    with conn() as c:
        return c.execute("INSERT INTO runs (started, mode, status) VALUES (?,?,'running')",
                         (time.time(), mode)).lastrowid


def finish_run(run_id, baseline, status="done"):
    with conn() as c:
        c.execute("UPDATE runs SET baseline=?, status=?, finished=? WHERE id=?",
                  (json.dumps(baseline), status, time.time(), run_id))


def save_result(run_id, name, data):
    with conn() as c:
        c.execute("DELETE FROM results WHERE run_id=? AND name=?", (run_id, name))
        c.execute("INSERT INTO results VALUES (?,?,?)", (run_id, name, json.dumps(data)))


def list_runs():
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT id, started, mode, status, finished FROM runs ORDER BY id DESC LIMIT 50")]


def get_run(run_id):
    with conn() as c:
        run = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not run:
            return None
        rows = c.execute("SELECT name, data FROM results WHERE run_id=?", (run_id,)).fetchall()
    run = dict(run)
    run["baseline"] = json.loads(run["baseline"]) if run["baseline"] else None
    run["results"] = [dict(json.loads(r["data"]), name=r["name"]) for r in rows]
    return run
