"""Per-config history across runs (scores are recomputed with the current weights)."""
from . import scoring


def _avg(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def build(runs, weights):
    """runs: run dicts (as returned by db.get_run), oldest first -> {config: [entry, ...]}"""
    out = {}
    for run in runs:
        base = run.get("baseline") or {}
        for r in run["results"]:
            tp = r.get("throughput") or {}
            err = r.get("error")
            out.setdefault(r["name"], []).append({
                "run": run["id"], "t": run["started"], "mode": run["mode"],
                "provider": r.get("provider"),
                "error": err,
                "score": scoring.score(r, weights, base)["total"],
                "avg_rtt": None if err else _avg([m["median"] for m in (r.get("matrix") or {}).values()]),
                "swarm": None if err else (r.get("swarm") or {}).get("success"),
                "hs": r.get("handshake_s"),
                "dl": (tp.get("dl") or {}).get("mbps"),
                "ul": (tp.get("ul") or {}).get("mbps"),
            })
    return out
