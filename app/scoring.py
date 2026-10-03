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


def _ref(base, own, fallback):
    """Reference speed for 'how much of the line is used': your direct measurement if it looks
    valid, else this config's own global result, else a fixed guess."""
    for v in (base, own):
        if v and v >= 20:
            return v
    return fallback


def country_score(st, rg, ref_dl, ref_ul):
    """One country 0-100. Latency/loss always count; measured throughput to that country
    (iPerf3) is added when available, with upload weighted highest (seeding). A direction that
    could not be measured is left out instead of counting as 0."""
    lat = rtt_score(st["median"], 0.0) / 100
    parts = [(0.20, lat), (0.15, 1 - min(1.0, st["loss"]))]
    if rg:
        if rg.get("up") is not None:
            parts.append((0.40, min(1.0, rg["up"] / (0.85 * ref_ul))))
        if rg.get("down") is not None:
            parts.append((0.25, min(1.0, rg["down"] / (0.85 * ref_dl))))
    if len(parts) == 2:  # no throughput measured: keep the plain latency score
        return rtt_score(st["median"], st["loss"])
    return 100 * sum(w * v for w, v in parts) / sum(w for w, _ in parts)


def score(result, weights, baseline=None):
    """Returns dict with sub-scores and total (0-100)."""
    if result.get("error"):
        return {"total": 0.0, "peering": 0.0, "swarm": 0.0, "reliability": 0.0,
                "throughput": None}
    m = result.get("matrix", {})
    tp = result.get("throughput") or {}
    regs = tp.get("regions") or {}
    base = baseline or {}
    ref_dl = _ref(base.get("dl", {}).get("mbps"), tp.get("dl", {}).get("mbps"), 300)
    ref_ul = _ref(base.get("ul", {}).get("mbps"), tp.get("ul", {}).get("mbps"), 100)
    num = den = 0.0
    worst = None
    for cc, st in m.items():
        s = country_score(st, regs.get(cc), ref_dl, ref_ul)
        w = weights.get(cc, 1.0)
        num += w * s
        den += w
        worst = s if worst is None else min(worst, s)
    # 85% weighted average + 15% worst country: one badly connected country must not hide
    peering = (0.85 * num / den + 0.15 * worst) if den else 0.0
    swarm = result.get("swarm", {}).get("success", 0.0) * 100
    hs = result.get("handshake_s") or 99
    reliability = (100 if hs <= 2 else 80 if hs <= 5 else 50) - (0 if result.get("mtu_ok") else 20)
    reliability = max(0.0, reliability)

    tp_score = None
    if tp:
        dl = min(1.0, tp["dl"]["mbps"] / ref_dl) * min(1.0, 0.5 + tp["dl"]["stability"])
        ul = min(1.0, tp["ul"]["mbps"] / ref_ul)
        tp_score = (0.7 * dl + 0.3 * ul) * 100

    parts = {"peering": (peering, 0.5), "swarm": (swarm, 0.2), "reliability": (reliability, 0.1)}
    if tp_score is not None:
        parts["throughput"] = (tp_score, 0.2)
    total = sum(v * w for v, w in parts.values()) / sum(w for _, w in parts.values())
    return {"total": round(total, 1), "peering": round(peering, 1), "swarm": round(swarm, 1),
            "reliability": round(reliability, 1),
            "throughput": None if tp_score is None else round(tp_score, 1)}
