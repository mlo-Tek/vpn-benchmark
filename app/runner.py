"""Orchestrates a benchmark run: baseline -> parallel peering phase -> serial throughput."""
import asyncio
import json
import sys
import time
from pathlib import Path

import yaml

from . import db, scoring
from .netns import Tunnel, TunnelError, run

WORKER = str(Path(__file__).with_name("worker.py"))
BUNDLED_TARGETS = Path(__file__).with_name("targets.yaml")

# Live state polled by the UI.
STATE = {"running": False, "run_id": None, "phase": "idle", "items": {}, "log": []}


def log(msg):
    STATE["log"] = (STATE["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-200:]


def load_targets():
    path = db.DATA / "targets.yaml"
    data = yaml.safe_load((path if path.exists() else BUNDLED_TARGETS).read_text())["targets"]
    return [{"country": cc, "host": h} for cc, hosts in data.items() for h in hosts]


def list_configs():
    """All *.conf below /configs; sub-folders are treated as providers."""
    return sorted(db.CONFIGS.rglob("*.conf")) if db.CONFIGS.exists() else []


def cid(path):
    """Unique id: path relative to /configs without .conf (e.g. CryptoStorm/cs-poland)."""
    return str(path.relative_to(db.CONFIGS).with_suffix(""))


def provider(path):
    parts = path.relative_to(db.CONFIGS).parts
    return parts[0] if len(parts) > 1 else "(root)"


async def host_worker(cmd, args, timeout=60):
    rc, out = await asyncio.wait_for(
        run(sys.executable, WORKER, cmd, stdin=json.dumps(args)), timeout)
    return json.loads(out)


def set_item(name, **kw):
    STATE["items"].setdefault(name, {}).update(kw)


async def test_peering(path, idx, targets):
    name = cid(path)
    set_item(name, status="connecting")
    result = {}
    t = None
    try:
        t = Tunnel(path, idx)
        await t.up()
        result["handshake_s"] = round(await t.wait_handshake(), 2)
        set_item(name, status="peering")
        result["mtu_ok"] = await t.mtu_ok()
        info, matrix = await asyncio.gather(
            t.worker("info", {}), t.worker("matrix", {"targets": targets}))
        sw = await t.worker("swarm", {"targets": targets, "total": 100})
        result.update(exit={k: info.get(k) for k in ("ip", "country", "city", "org")},
                      ipv6=info.get("ipv6"), dns_ok=info.get("dns_ok"),
                      dns_ms=info.get("dns_ms"), matrix=scoring.country_stats(matrix),
                      swarm=sw)
        set_item(name, status="peering done")
    except (TunnelError, asyncio.TimeoutError, Exception) as e:  # noqa: BLE001
        result["error"] = str(e) or type(e).__name__
        set_item(name, status="failed")
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


async def run_benchmark(mode, parallel, topn, selected=None, providers=None):
    """mode: peering | standard | full"""
    STATE.update(running=True, phase="baseline", items={}, log=[])
    run_id = db.new_run(mode)
    STATE["run_id"] = run_id
    baseline, status = {}, "done"
    try:
        configs = [p for p in list_configs()
                   if (not selected or cid(p) in selected)
                   and (not providers or provider(p) in providers)]
        for p in configs:
            set_item(cid(p), status="queued")
        targets = load_targets()
        weights = db.get_setting("weights", scoring.DEFAULT_WEIGHTS)

        log("baseline (no VPN)")
        try:
            m = await host_worker("matrix", {"targets": targets})
            baseline["matrix"] = scoring.country_stats(m)
            if mode != "peering":
                baseline.update(await host_worker("throughput", {"duration": 8}, timeout=60))
        except Exception as e:  # noqa: BLE001
            log(f"baseline failed: {e}")

        STATE["phase"] = "peering"
        sem = asyncio.Semaphore(parallel)
        results = {}

        async def one(i, p):
            async with sem:
                r = await test_peering(p, i, targets)
                results[cid(p)] = r
                db.save_result(run_id, cid(p), r)

        await asyncio.gather(*(one(i, p) for i, p in enumerate(configs)))

        if mode != "peering":
            STATE["phase"] = "throughput"
            ranked = sorted(
                (p for p in configs if not results[cid(p)].get("error")),
                key=lambda p: -scoring.score(results[cid(p)], weights)["total"])
            for p in (ranked if mode == "full" else ranked[:topn]):
                set_item(cid(p), status="throughput")
                try:
                    results[cid(p)]["throughput"] = await test_throughput(p, 0, 8)
                    db.save_result(run_id, cid(p), results[cid(p)])
                    set_item(cid(p), status="done")
                except Exception as e:  # noqa: BLE001
                    log(f"{cid(p)}: throughput failed: {e}")
                    set_item(cid(p), status="throughput failed")
    except Exception as e:  # noqa: BLE001
        log(f"run aborted: {e}")
        status = "failed"
    finally:
        db.finish_run(run_id, baseline, status)
        STATE.update(running=False, phase="idle")
        log("finished")
