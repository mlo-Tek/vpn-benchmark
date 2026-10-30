import asyncio
import csv
import io
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import configs as cfg
from . import db, runner, scoring, version

app = FastAPI(title="VPN Benchmark")
STATIC = Path(__file__).with_name("static")


class RunRequest(BaseModel):
    mode: str = "standard"  # peering | standard | full
    parallel: int = 8
    topn: int = 10
    selected: list[str] | None = None
    providers: list[str] | None = None
    tp_runs: int = 1  # throughput repeats per direction (median)


class Settings(BaseModel):
    weights: dict[str, float]


def scored(run):
    weights = db.get_setting("weights", scoring.DEFAULT_WEIGHTS)
    base = run.get("baseline") or {}
    for r in run["results"]:
        r["score"] = scoring.score(r, weights, base)
    run["results"].sort(key=lambda r: -r["score"]["total"])
    return run


@app.get("/api/configs")
def configs():
    return [cfg.describe(p) for p in cfg.list_paths()]


@app.post("/api/configs/upload")
async def upload(files: list[UploadFile] = File(...), provider: str = Form("")):
    if runner.STATE["running"]:
        raise HTTPException(409, "während eines Laufs nicht möglich")
    saved, errors = [], []
    for f in files:
        try:
            saved.append(cfg.save(f.filename or "", await f.read(), provider))
        except ValueError as e:
            errors.append(str(e))
    return {"saved": saved, "errors": errors}


@app.delete("/api/configs")
def delete_config(id: str):
    if runner.STATE["running"]:
        raise HTTPException(409, "während eines Laufs nicht möglich")
    if not cfg.delete(id):
        raise HTTPException(404)
    return {"ok": True}


@app.post("/api/run")
async def start(req: RunRequest):
    if runner.STATE["running"]:
        raise HTTPException(409, "es läuft bereits ein Test")
    if req.mode not in ("peering", "standard", "full"):
        raise HTTPException(400, "bad mode")
    runner.start(req.mode, max(1, min(req.parallel, 32)), max(1, req.topn),
                 req.selected, req.providers, max(1, min(req.tp_runs, 5)))
    return {"started": True}


@app.post("/api/stop")
def stop():
    return {"stopped": runner.stop()}


@app.post("/api/pause")
def pause():
    return {"paused": runner.pause()}


@app.post("/api/resume")
def resume():
    return {"paused": not runner.resume()}


@app.get("/api/version")
async def get_version(force: bool = False):
    return await version.check(force)


@app.get("/api/status")
def status():
    return runner.STATE


@app.get("/api/runs")
def runs():
    return db.list_runs()


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404)
    return scored(run)


@app.get("/api/runs/{run_id}/log")
def run_log(run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404)
    return PlainTextResponse(run["log"] or "(kein Log gespeichert)")


@app.get("/api/runs/{run_id}/csv")
def run_csv(run_id: int):
    run = db.get_run(run_id)
    if not run:
        raise HTTPException(404)
    run = scored(run)
    countries = sorted({cc for r in run["results"] for cc in r.get("matrix", {})})
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["config", "score", "exit_ip", "exit_country", "handshake_s", "swarm_success",
                "dl_mbps", "ul_mbps", "error"] + [f"rtt_{c}_ms" for c in countries])
    for r in run["results"]:
        tp = r.get("throughput") or {}
        w.writerow([r["name"], r["score"]["total"], (r.get("exit") or {}).get("ip"),
                    (r.get("exit") or {}).get("country"), r.get("handshake_s"),
                    (r.get("swarm") or {}).get("success"),
                    (tp.get("dl") or {}).get("mbps"), (tp.get("ul") or {}).get("mbps"),
                    r.get("error")] +
                   [(r.get("matrix", {}).get(c) or {}).get("median") for c in countries])
    return PlainTextResponse(out.getvalue(), media_type="text/csv")


@app.get("/api/settings")
def get_settings():
    return {"weights": db.get_setting("weights", scoring.DEFAULT_WEIGHTS)}


@app.put("/api/settings")
def put_settings(s: Settings):
    db.set_setting("weights", s.weights)
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
