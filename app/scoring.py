"""Scoring. Raw metrics are stored; scores are computed at read time so that
changing the country weights re-ranks old runs immediately."""
import statistics

DEFAULT_WEIGHTS = {"NO": 1.0, "NL": 1.0, "DE": 1.0, "SE": 1.0, "CH": 1.0, "FI": 1.0,
                   "DK": 1.0, "PL": 1.0}


def rtt_score(rtt, loss):
    if rtt is None:
        return 0.0
    return 100.0 / (1.0 + (rtt / 80.0) ** 2) * (1.0 - loss)  # 0ms=100, 80ms=50, 160ms=20


def country_stats(targets):
    """targets: worker 'matrix' output for one config -> {CC: {median, loss, targets}}"""
    by = {}
    for t in targets:
        by.setdefault(t["country"], []).append(t)
    out = {}
    for cc, ts in by.items():
        meds = [t["median"] for t in ts if t["median"] is not None]
        out[cc] = {
            "median": statistics.median(meds) if meds else None,
            "loss": statistics.mean(t["loss"] for t in ts),
            "targets": {t["host"]: {"median": t["median"], "loss": t["loss"]} for t in ts},
        }
    return out


def score(result, weights, baseline=None):
    """Returns dict with sub-scores and total (0-100)."""
    if result.get("error"):
        return {"total": 0.0, "peering": 0.0, "swarm": 0.0, "reliability": 0.0,
                "throughput": None}
    m = result.get("matrix", {})
    num = den = 0.0
    for cc, st in m.items():
        w = weights.get(cc, 1.0)
        num += w * rtt_score(st["median"], st["loss"])
        den += w
    peering = num / den if den else 0.0
    swarm = result.get("swarm", {}).get("success", 0.0) * 100
    hs = result.get("handshake_s") or 99
    reliability = (100 if hs <= 2 else 80 if hs <= 5 else 50) - (0 if result.get("mtu_ok") else 20)
    reliability = max(0.0, reliability)

    tp = result.get("throughput")
    tp_score = None
    if tp:
        bdl = (baseline or {}).get("dl", {}).get("mbps") or tp["dl"]["mbps"] or 1
        bul = (baseline or {}).get("ul", {}).get("mbps") or tp["ul"]["mbps"] or 1
        dl = min(1.0, tp["dl"]["mbps"] / bdl) * min(1.0, 0.5 + tp["dl"]["stability"])
        ul = min(1.0, tp["ul"]["mbps"] / bul)
        tp_score = (0.7 * dl + 0.3 * ul) * 100

    parts = {"peering": (peering, 0.5), "swarm": (swarm, 0.2), "reliability": (reliability, 0.1)}
    if tp_score is not None:
        parts["throughput"] = (tp_score, 0.2)
    total = sum(v * w for v, w in parts.values()) / sum(w for _, w in parts.values())
    return {"total": round(total, 1), "peering": round(peering, 1), "swarm": round(swarm, 1),
            "reliability": round(reliability, 1),
            "throughput": None if tp_score is None else round(tp_score, 1)}
