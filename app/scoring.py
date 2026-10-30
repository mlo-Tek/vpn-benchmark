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


PROFILES = ("balanced", "seeding")
DEFAULT_PROFILE = "seeding"


def country_score(st, rg, ref_dl, ref_ul, profile=DEFAULT_PROFILE):
    """One country: RTT/loss always, regional upload/download when available."""
    lat = rtt_score(st["median"], 0.0) / 100
    if profile == "seeding":
        parts = [(0.18, lat), (0.17, 1 - min(1.0, st["loss"]))]
        upload_weight, download_weight = 0.50, 0.15
    else:
        parts = [(0.20, lat), (0.15, 1 - min(1.0, st["loss"]))]
        upload_weight, download_weight = 0.40, 0.25
    if rg:
        if rg.get("up") is not None:
            parts.append((upload_weight, min(1.0, rg["up"] / (0.85 * ref_ul))))
        if rg.get("down") is not None:
            parts.append((download_weight, min(1.0, rg["down"] / (0.85 * ref_dl))))
    if len(parts) == 2:
        return rtt_score(st["median"], st["loss"])
    return 100 * sum(w * v for w, v in parts) / sum(w for w, _ in parts)


def score(result, weights, baseline=None, profile=DEFAULT_PROFILE):
    """Return a score plus its measurement coverage (not comparable across tiers)."""
    if profile not in PROFILES:
        profile = DEFAULT_PROFILE
    if result.get("error"):
        return {"total": 0.0, "peering": 0.0, "swarm": 0.0, "reliability": 0.0,
                "throughput": None, "coverage": "failed"}
    m = result.get("matrix", {})
    tp = result.get("throughput") or {}
    regs = tp.get("regions") or {}
    base = baseline or {}
    ref_dl = _ref((base.get("dl") or {}).get("mbps"), (tp.get("dl") or {}).get("mbps"), 300)
    ref_ul = _ref((base.get("ul") or {}).get("mbps"), (tp.get("ul") or {}).get("mbps"), 100)
    num = den = 0.0
    worst = None
    for cc, st in m.items():
        s = country_score(st, regs.get(cc), ref_dl, ref_ul, profile)
        w = max(0.0, weights.get(cc, 1.0))
        num += w * s
        den += w
        worst = s if worst is None else min(worst, s)
    peering = (0.85 * num / den + 0.15 * worst) if den else 0.0
    swarm = result.get("swarm", {}).get("success", 0.0) * 100
    hs = result.get("handshake_s")
    reliability = (100 if hs is not None and hs <= 2 else 80 if hs is not None and hs <= 5 else 50)
    reliability = max(0.0, reliability - (0 if result.get("mtu_ok") else 20))

    tp_score = None
    if (tp.get("dl") or {}).get("mbps") is not None and (tp.get("ul") or {}).get("mbps") is not None:
        dl = min(1.0, tp["dl"]["mbps"] / ref_dl) * min(1.0, 0.5 + tp["dl"].get("stability", 0.5))
        ul = min(1.0, tp["ul"]["mbps"] / ref_ul)
        tp_score = ((0.25 * dl + 0.75 * ul) if profile == "seeding"
                    else (0.70 * dl + 0.30 * ul)) * 100

    if profile == "seeding":
        parts = {"peering": (peering, 0.45), "swarm": (swarm, 0.15),
                 "reliability": (reliability, 0.10)}
        tp_weight = 0.30
    else:
        parts = {"peering": (peering, 0.50), "swarm": (swarm, 0.20),
                 "reliability": (reliability, 0.10)}
        tp_weight = 0.20
    if tp_score is not None:
        parts["throughput"] = (tp_score, tp_weight)
    total = sum(v * w for v, w in parts.values()) / sum(w for _, w in parts.values())
    return {"total": round(total, 1), "peering": round(peering, 1), "swarm": round(swarm, 1),
            "reliability": round(reliability, 1),
            "throughput": None if tp_score is None else round(tp_score, 1),
            "coverage": "full" if tp_score is not None else "peering"}
