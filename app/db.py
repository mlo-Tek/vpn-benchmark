import json
import os
import sqlite3
import time
from pathlib import Path

DATA = Path(os.environ.get("DATA_DIR", "/data"))
CONFIGS = Path(os.environ.get("CONFIG_DIR", "/configs"))


def _own(path):
    """The container runs as root (network namespaces need it), but files in /data
    should belong to PUID/PGID (Unraid: 99/100 = nobody:users)."""
    uid, gid = os.environ.get("PUID"), os.environ.get("PGID")
    if uid and gid and os.path.exists(path):
        os.chown(path, int(uid), int(gid))


def conn():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DATA / "results.db")
    _own(DATA)
    _own(DATA / "results.db")
    c.row_factory = sqlite3.Row
    c.executescript("""
        CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, started REAL, mode TEXT,
            baseline TEXT, status TEXT, finished REAL);
        CREATE TABLE IF NOT EXISTS results (run_id INTEGER, name TEXT, data TEXT);
        CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
    """)
    try:
        c.execute("ALTER TABLE runs ADD COLUMN log TEXT")
    except sqlite3.OperationalError:
        pass  # column exists
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


def finish_run(run_id, baseline, status="done", log=""):
    with conn() as c:
        c.execute("UPDATE runs SET baseline=?, status=?, finished=?, log=? WHERE id=?",
                  (json.dumps(baseline), status, time.time(), log, run_id))


def save_result(run_id, name, data):
    with conn() as c:
        c.execute("DELETE FROM results WHERE run_id=? AND name=?", (run_id, name))
        c.execute("INSERT INTO results VALUES (?,?,?)", (run_id, name, json.dumps(data)))


def delete_run(run_id):
    with conn() as c:
        c.execute("DELETE FROM results WHERE run_id=?", (run_id,))
        return c.execute("DELETE FROM runs WHERE id=?", (run_id,)).rowcount > 0


def delete_result(run_id, name):
    with conn() as c:
        return c.execute("DELETE FROM results WHERE run_id=? AND name=?", (run_id, name)).rowcount > 0


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
    run["log"] = run.get("log") or ""
    run["results"] = [dict(json.loads(r["data"]), name=r["name"]) for r in rows]
    return run
