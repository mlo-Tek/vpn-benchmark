"""Orchestrates a benchmark run: baseline -> parallel peering phase -> serial throughput."""
import asyncio
import json
import re
import sys
import time
from pathlib import Path

import yaml

from . import db, scoring
from .configs import cid, list_paths, provider  # noqa: F401  (re-exported for main/tests)
from .netns import IGNORE_V6, Tunnel, TunnelError, run

WORKER = str(Path(__file__).with_name("worker.py"))
BUNDLED_TARGETS = Path(__file__).with_name("targets.yaml")
BUNDLED_REGIONS = Path(__file__).with_name("regions.yaml")

# Live state polled by the UI.
STATE = {"paused": False, "running": False, "run_id": None, "mode": None, "phase": "idle", "items": {},
         "log": [], "started": None, "progress": 0, "total": 0, "done": 0}
TASK = None
GATE = asyncio.Event()  # cleared while paused; waiting configs don't start until it is set
GATE.set()


def log(msg):
    STATE["log"] = (STATE["log"] + [f"{time.strftime('%H:%M:%S')} {msg}"])[-3000:]


def load_targets():
    path = db.DATA / "targets.yaml"
    data = yaml.safe_load((path if path.exists() else BUNDLED_TARGETS).read_text())["targets"]
    # YAML 1.1 reads an unquoted NO as False, so user-supplied files may still contain it.
    fix = lambda cc: "NO" if cc is False else str(cc)  # noqa: E731
    return [{"country": fix(cc), "host": h} for cc, hosts in data.items() for h in hosts]


def parse_ports(spec):
    """5201 / "5201" / "5200-5209" -> [ports]"""
    spec = str(spec)
    if "-" in spec:
        a, b = spec.split("-", 1)
        return list(range(int(a), int(b) + 1))
    return [int(spec)]


def load_regions():
    """{country: [{host, ports:[...]}, ...]} from /data/regions.yaml or the bundled file."""
    path = db.DATA / "regions.yaml"
    data = yaml.safe_load((path if path.exists() else BUNDLED_REGIONS).read_text())["regions"]
    fix = lambda cc: "NO" if cc is False else str(cc)  # noqa: E731  (YAML 1.1: NO -> False)
    return {fix(cc): [{"host": s["host"], "ports": parse_ports(s.get("ports", 5201))} for s in srv]
            for cc, srv in data.items()}


def list_bittorrent_peers():
    """Optional, consent-based peer list for a legal test torrent."""
    path = db.DATA / "bittorrent.yaml"
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text()) or {}
    peers = raw.get("peers", [])
    if not isinstance(peers, list) or len(peers) > 25:
        raise ValueError("bittorrent.yaml: expected up to 25 peers")
    out = []
    for p in peers:
        if not isinstance(p, dict):
            raise ValueError("bittorrent.yaml: invalid peer")
        host, infohash = str(p.get("host", "")), str(p.get("infohash", ""))
        try:
            port = int(p["port"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("bittorrent.yaml: invalid port") from None
        if not host or len(host) > 253 or any(c.isspace() for c in host) or not 1 <= port <= 65535:
            raise ValueError("bittorrent.yaml: invalid host/port")
        if not re.fullmatch(r"[A-Fa-f0-9]{40}", infohash):
            raise ValueError("bittorrent.yaml: infohash must be 40 hexadecimal digits")
        out.append({"host": host, "port": port, "infohash": infohash})
    return out


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


async def test_peering(path, idx, targets, bt_peers=None):
    name = cid(path)
    t0 = time.time()
    set_item(name, status="connecting", stage="Tunnel aufbauen", t0=t0)
    result, events = {"provider": provider(path)}, []
    t = None

    def ev(msg):
        events.append({"t": round(time.time() - t0, 2), "msg": msg})

    try:
        t = Tunnel(path, idx)
        ev(f"Tunnel aufbauen, Endpoint {t.conf['peer']['endpoint']}"
           + (" (nur IPv4)" if IGNORE_V6 else ""))
        await t.up()
        set_item(name, stage="Handshake")
        result["handshake_s"] = round(await t.wait_handshake(), 2)
        ev(f"Handshake nach {result['handshake_s']} s"
           + (" (WireGuard hat nach 5 s wiederholt: erstes Paket ging verloren)"
              if result["handshake_s"] >= 4.5 else ""))
        set_item(name, status="peering", stage="MTU + Exit-Info")
        result["mtu_ok"] = await t.mtu_ok()
        ev("MTU 1420: " + ("ok" if result["mtu_ok"] else "Pakete werden fragmentiert/verworfen"))
        info_task = asyncio.ensure_future(t.worker("info", {"skip_v6": IGNORE_V6}))
        set_item(name, stage="Peer-Länder messen")
        matrix = await t.worker("matrix", {"targets": targets})
        reached = sum(1 for m in matrix if m["median"] is not None)
        ev(f"Peer-Ziele erreicht: {reached}/{len(matrix)}")
        info = await info_task
        set_item(name, stage="Swarm-Test (100 Verbindungen)")
        sw = await t.worker("swarm", {"targets": targets, "total": 100})
        ev(f"TCP-Swarm: {round(sw['success'] * 100)}% der 100 Verbindungen erfolgreich")
        if bt_peers:
            set_item(name, stage="Echte BitTorrent-Peer-Handshakes (optional)")
            try:
                bt = await t.worker("bittorrent", {"peers": bt_peers}, timeout=60)
                result["bittorrent"] = bt
                ev(f"BitTorrent: {round(bt['success'] * 100)}% von {bt['total']} Peer-Handshakes erfolgreich")
            except Exception as e:
                ev(f"BitTorrent-Zusatztest fehlgeschlagen: {str(e)[:120]}")
        result.update(exit={k: info.get(k) for k in ("ip", "country", "city", "org")},
                      ipv6=info.get("ipv6"), dns_ok=info.get("dns_ok"),
                      dns_ms=info.get("dns_ms"), matrix=scoring.country_stats(matrix),
                      swarm=sw)
        result["wg"] = await t.wg_stats()
        ev(f"WireGuard-Zähler: {result['wg']['tx']} B gesendet, {result['wg']['rx']} B empfangen")
        if reached == 0 and sw["success"] == 0:
            result["error"] = (
                "Handshake ok, aber kein Datenverkehr durch den Tunnel. Der Server nimmt die "
                "Verbindung an, leitet aber nichts weiter (Exit defekt/überlastet, oder die "
                "Config wird gleichzeitig woanders benutzt).")
            raise TunnelError(result["error"])
        set_item(name, status="peering done", stage="Peering fertig", t1=time.time())
        avg_rtt = [m["median"] for m in matrix if m["median"] is not None]
        log(f"{name}: OK, Handshake {result['handshake_s']} s, Ø "
            f"{round(sum(avg_rtt) / len(avg_rtt))} ms, Swarm {round(sw['success'] * 100)}%")
    except Exception as e:  # noqa: BLE001
        result["error"] = result.get("error") or str(e) or type(e).__name__
        ev(f"FEHLER: {result['error']}")
        set_item(name, status="failed", stage=result["error"][:120], t1=time.time())
        log(f"{name}: FEHLER {result['error']}")
        if t and t.pid and "wg" not in result:
            try:
                result["wg"] = await t.wg_stats()
                ev(f"WireGuard-Zähler: {result['wg']['tx']} B gesendet, "
                   f"{result['wg']['rx']} B empfangen")
            except Exception:  # noqa: BLE001
                pass
    finally:
        if t:
            await t.down()
        result["events"] = events
    return result


async def test_throughput(path, idx, duration, repeats=1, regions=None):
    """Global down/up and, if `regions` is given, down/up to every peer country through the
    same tunnel (one country after another: they all share your line)."""
    name = cid(path)
    t = Tunnel(path, idx)
    try:
        await t.up()
        await t.wait_handshake()
        set_item(name, stage="Download/Upload messen")
        tp = await t.worker("throughput", {"duration": duration, "repeats": repeats},
                            timeout=duration * 2 * repeats + 60)
        if regions:
            tp["regions"] = {}
            for n, (cc, servers) in enumerate(regions.items(), 1):
                set_item(name, stage=f"Länder-Durchsatz {cc} ({n}/{len(regions)}), iPerf3")
                try:
                    r = await t.worker("region", {"country": cc, "servers": servers,
                                                  "duration": 3, "parallel": 4}, timeout=240)
                except Exception as e:  # noqa: BLE001
                    r = {"country": cc, "down": None, "up": None, "down_error": str(e)[:80]}
                tp["regions"][cc] = r
                log(f"{name}: {cc} ↓ {r.get('down')} / ↑ {r.get('up')} Mbit/s"
                    + (f" ({r['down_error']})" if r.get("down_error") and r.get("down") is None else ""))
        return tp
    finally:
        await t.down()


def start(mode, parallel, topn, selected=None, providers=None, tp_runs=1, regions=True):
    global TASK
    TASK = asyncio.create_task(
        run_benchmark(mode, parallel, topn, selected, providers, tp_runs, regions))


def pause():
    if STATE["running"]:
        GATE.clear()
        STATE["paused"] = True
        log("Pausiert: bereits laufende Configs werden noch fertig, neue starten nicht")
    return STATE["paused"]


def resume():
    GATE.set()
    if STATE["paused"]:
        STATE["paused"] = False
        log("Fortgesetzt")
    return True


def stop():
    if TASK and not TASK.done():
        TASK.cancel()
        return True
    return False


async def run_benchmark(mode, parallel, topn, selected=None, providers=None, tp_runs=1,
                        regions=True):
    """mode: peering | standard | full"""
    STATE.update(running=True, mode=mode, phase="baseline", items={}, log=[],
                 started=time.time(), progress=0, total=0, done=0, paused=False)
    GATE.set()
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
        log(f"{len(configs)} Configs, Modus '{mode}', {parallel} parallel, "
            f"Durchsatz {tp_runs}x pro Richtung" + (", nur IPv4" if IGNORE_V6 else "")
            + (", Länder-Durchsatz an" if regions and mode != "peering" else ""))
        region_cfg = load_regions() if regions and mode != "peering" else None
        targets = load_targets()
        bt_peers = list_bittorrent_peers()
        weights = db.get_setting("weights", scoring.DEFAULT_WEIGHTS)
        profile = db.get_setting("profile", scoring.DEFAULT_PROFILE)

        log("Baseline ohne VPN")
        try:
            m = await host_worker("matrix", {"targets": targets})
            baseline["matrix"] = scoring.country_stats(m)
            if mode != "peering":
                baseline.update(await host_worker(
                    "throughput", {"duration": 8, "repeats": tp_runs}, timeout=40 + 20 * tp_runs))
                log(f"Baseline: ↓ {round(baseline['dl']['mbps'])} / ↑ {round(baseline['ul']['mbps'])} Mbit/s")
        except Exception as e:  # noqa: BLE001
            log(f"Baseline fehlgeschlagen: {e}")
        set_progress(mode, 0, 0)

        STATE["phase"] = "peering"
        sem = asyncio.Semaphore(parallel)
        results = {}

        async def one(i, p):
            async with sem:
                await GATE.wait()
                r = await test_peering(p, i, targets, bt_peers)
                results[cid(p)] = r
                db.save_result(run_id, cid(p), r)
                STATE["done"] += 1
                set_progress(mode, STATE["done"] / max(1, len(configs)), 0)

        await asyncio.gather(*(one(i, p) for i, p in enumerate(configs)))

        if mode != "peering":
            STATE["phase"] = "throughput"
            ranked = sorted(
                (p for p in configs if not results[cid(p)].get("error")),
                key=lambda p: -scoring.score(results[cid(p)], weights, baseline, profile)["total"])
            todo = ranked if mode == "full" else ranked[:topn]
            log(f"Durchsatz für {len(todo)} Configs")
            for n, p in enumerate(todo):
                await GATE.wait()
                set_item(cid(p), status="throughput", stage="Download/Upload messen", t1=None)
                try:
                    results[cid(p)]["throughput"] = tp = await test_throughput(
                        p, 0, 8, tp_runs, region_cfg)
                    log(f"{cid(p)}: ↓ {round(tp['dl']['mbps'])} / ↑ {round(tp['ul']['mbps'])} Mbit/s"
                        + (f" (Läufe ↓ {tp['dl']['runs']})" if tp_runs > 1 else ""))
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
        GATE.set()
        log("Fertig" if status == "done" else f"Beendet ({status})")
        db.finish_run(run_id, baseline, status, "\n".join(STATE["log"]))
        STATE.update(running=False, phase="idle", paused=False)
