"""Version info + update check.

VERSION is bumped by hand for releases; GIT_SHA / BUILD_DATE are baked into the image
by the GitHub Action. The update check compares GIT_SHA with the newest commit on
main. The repo is private, so it needs an optional GITHUB_TOKEN (fine-grained,
read-only "Contents"); without it the check reports "unknown".
"""
import asyncio
import json
import os
import time
import urllib.error
import urllib.request

VERSION = "0.5.0"
GIT_SHA = os.environ.get("GIT_SHA", "dev")
BUILD_DATE = os.environ.get("BUILD_DATE", "")
REPO = os.environ.get("GITHUB_REPO", "mlo-Tek/vpn-benchmark")

_cache = {"t": 0.0, "data": None}


def info():
    return {"version": VERSION, "sha": GIT_SHA[:7], "built": BUILD_DATE, "repo": REPO}


def _fetch_latest():
    req = urllib.request.Request(
        f"https://api.github.com/repos/{REPO}/commits/main",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "vpn-benchmark"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=8) as r:
        d = json.load(r)
    return {"sha": d["sha"], "message": d["commit"]["message"].splitlines()[0],
            "date": d["commit"]["committer"]["date"]}


def _check():
    base = info()
    if GIT_SHA == "dev":
        return {**base, "status": "dev", "detail": "Entwicklungs-Build, kein Update-Check."}
    try:
        latest = _fetch_latest()
    except urllib.error.HTTPError as e:
        hint = ("GITHUB_TOKEN fehlt oder ist ungültig (Repo ist privat)."
                if e.code in (401, 403, 404) else f"GitHub antwortete mit {e.code}.")
        return {**base, "status": "unknown", "detail": hint}
    except Exception as e:  # noqa: BLE001
        return {**base, "status": "unknown", "detail": f"Prüfung fehlgeschlagen: {e}"}
    if latest["sha"].startswith(GIT_SHA[:7]) or GIT_SHA.startswith(latest["sha"][:7]):
        return {**base, "status": "current", "detail": "Du bist auf dem neuesten Stand.",
                "latest": latest["sha"][:7]}
    return {**base, "status": "update", "latest": latest["sha"][:7],
            "detail": f"Neuer Commit auf main: {latest['message']} ({latest['sha'][:7]}). "
                      "Das Image ist ggf. noch im Bau (GitHub Actions)."}


async def check(force=False):
    if force or not _cache["data"] or time.time() - _cache["t"] > 600:
        _cache["data"] = await asyncio.to_thread(_check)
        _cache["t"] = time.time()
    return _cache["data"]
