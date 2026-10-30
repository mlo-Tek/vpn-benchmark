"""Orchestrates a benchmark run: baseline -> parallel peering phase -> serial throughput."""
import asyncio
import json
import sys
import time
from pathlib import Path

import yaml

from . import db, scoring
from .configs import cid, list_paths, provider  # noqa: F401  (re-exported for main/tests)
from .netns import Tunnel, TunnelError, run

WORKER = str(Path(__file__).with_name("worker.py"))
BUNDLED_TARGETS = Path(__file__).with_name("targets.yaml")

# Live state polled by the UI.
STATE = {"running": False, "run_id": None, "mode": None, "phase": "idle", "items": {},
         "log": [], "started": None, "progress": 0, "total": 0, "done": 0}
TASK = None


def log(msg):
    STATE["log"] = (STATE["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-300:]


def load_targets():
    path = db.DATA / "targets.yaml"
    data = yaml.safe_load((path if path.exists() else BUNDLED_TARGETS).read_text())["targets"]
    return [{"country": cc, "host": h} for cc, hosts in data.items() for h in hosts]


def list_configs():
    return list_paths()


async def host_worker(cmd, args, timeout=60):
    rc, out = await asyncio.wait_for(
        run(sys.executable, WORKER, cmd, stdin=json.dumps(args)), timeout)
    return json.loads(out)


def set_item(name, **kw):
    STATE["items"].setdefault(name, {}).update(kw)


def set_progress(mode, p1, p2):
    """p1/p2 = fraction of phase 1 / phase 2 done."""
    STATE["progress"] = round(5 + (95 * p1 if mode == "peering" else 40 * p1 + 55 * p2), 1)


async def test_peering(path, idx, targets):
    name = cid(path)
    set_item(name, status="connecting", stage="Tunnel aufbauen", t0=time.time())
    result = {}
    t = None
    try:
        t = Tunnel(path, idx)
        await t.up()
        set_item(name, stage="Handshake")
        result["handshake_s"] = round(await t.wait_handshake(), 2)
        set_item(name, status="peering", stage="MTU + Exit-Info")
        result["mtu_ok"] = await t.mtu_ok()
        info_task = asyncio.ensure_future(t.worker("info", {}))
        set_item(name, stage="Peer-Länder messen")
        matrix = await t.worker("matrix", {"targets": targets})
        info = await info_task
        set_item(name, stage="Swarm-Test (100 Verbindungen)")
        sw = await t.worker("swarm", {"targets": targets, "total": 100})
        result.update(exit={k: info.get(k) for k in ("ip", "country", "city", "org")},
                      ipv6=info.get("ipv6"), dns_ok=info.get("dns_ok"),
                      dns_ms=info.get("dns_ms"), matrix=scoring.country_stats(matrix),
                      swarm=sw)
        set_item(name, status="peering done", stage="Peering fertig", t1=time.time())
    except Exception as e:  # noqa: BLE001
        result["error"] = str(e) or type(e).__name__
        set_item(name, status="failed", stage=result["error"][:120], t1=time.time())
        log(f"{name}: {result['error']}")
    finally:
        if t:
            await t.down()
    return result


async def test_throughput(path, idx, duration):
    t = Tunnel(path, idx)
    try:
        await t.up()
        await t.wait_handshake()
        return await t.worker("throughput", {"duration": duration}, timeout=duration * 2 + 30)
    finally:
        await t.down()


def start(mode, parallel, topn, selected=None, providers=None):
    global TASK
    TASK = asyncio.create_task(run_benchmark(mode, parallel, topn, selected, providers))


def stop():
    if TASK and not TASK.done():
        TASK.cancel()
        return True
    return False


async def run_benchmark(mode, parallel, topn, selected=None, providers=None):
    """mode: peering | standard | full"""
    STATE.update(running=True, mode=mode, phase="baseline", items={}, log=[],
                 started=time.time(), progress=0, total=0, done=0)
    run_id = db.new_run(mode)
    STATE["run_id"] = run_id
    baseline, status = {}, "done"
    try:
        configs = [p for p in list_configs()
                   if (not selected or cid(p) in selected)
                   and (not providers or provider(p) in providers)]
        STATE["total"] = len(configs)
        for p in configs:
            set_item(cid(p), status="queued", stage="wartet")
        log(f"{len(configs)} Configs, Modus '{mode}', {parallel} parallel")
        targets = load_targets()
        weights = db.get_setting("weights", scoring.DEFAULT_WEIGHTS)

        log("Baseline ohne VPN")
        try:
            m = await host_worker("matrix", {"targets": targets})
            baseline["matrix"] = scoring.country_stats(m)
            if mode != "peering":
                baseline.update(await host_worker("throughput", {"duration": 8}, timeout=60))
        except Exception as e:  # noqa: BLE001
            log(f"Baseline fehlgeschlagen: {e}")
        set_progress(mode, 0, 0)

        STATE["phase"] = "peering"
        sem = asyncio.Semaphore(parallel)
        results = {}

        async def one(i, p):
            async with sem:
                r = await test_peering(p, i, targets)
                results[cid(p)] = r
                db.save_result(run_id, cid(p), r)
                STATE["done"] += 1
                set_progress(mode, STATE["done"] / max(1, len(configs)), 0)

        await asyncio.gather(*(one(i, p) for i, p in enumerate(configs)))

        if mode != "peering":
            STATE["phase"] = "throughput"
            ranked = sorted(
                (p for p in configs if not results[cid(p)].get("error")),
                key=lambda p: -scoring.score(results[cid(p)], weights)["total"])
            todo = ranked if mode == "full" else ranked[:topn]
            log(f"Durchsatz für {len(todo)} Configs")
            for n, p in enumerate(todo):
                set_item(cid(p), status="throughput", stage="Download/Upload messen", t1=None)
                try:
                    results[cid(p)]["throughput"] = await test_throughput(p, 0, 8)
                    db.save_result(run_id, cid(p), results[cid(p)])
                    set_item(cid(p), status="done", stage="fertig", t1=time.time())
                except Exception as e:  # noqa: BLE001
                    log(f"{cid(p)}: Durchsatz fehlgeschlagen: {e}")
                    set_item(cid(p), status="throughput failed", stage=str(e)[:120],
                             t1=time.time())
                set_progress(mode, 1, (n + 1) / len(todo))
        STATE["progress"] = 100
    except asyncio.CancelledError:
        status = "cancelled"
        log("Abgebrochen")
        for it in STATE["items"].values():
            if it.get("status") in ("queued", "connecting", "peering", "throughput"):
                it.update(status="cancelled", stage="abgebrochen")
    except Exception as e:  # noqa: BLE001
        log(f"Lauf abgebrochen: {e}")
        status = "failed"
    finally:
        db.finish_run(run_id, baseline, status)
        STATE.update(running=False, phase="idle")
        log("Fertig" if status == "done" else f"Beendet ({status})")
