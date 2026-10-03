import pytest

from app import scoring
from app.netns import parse_conf, split_endpoint

CONF = """
[Interface]
PrivateKey = AAAA
Address = 10.2.0.2/32, fd00::2/128
DNS = 10.2.0.1
[Peer]
PublicKey = BBBB
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = de-fra.example.net:51820
PersistentKeepalive = 25
"""


def test_parse_conf():
    c = parse_conf(CONF)
    assert c["iface"]["address"].startswith("10.2.0.2")
    assert c["peer"]["publickey"] == "BBBB"


def test_parse_conf_rejects_invalid():
    with pytest.raises(ValueError):
        parse_conf("[Interface]\nPrivateKey = x\n")


def test_split_endpoint_v6():
    assert split_endpoint("[2001:db8::1]:51820") == ("2001:db8::1", 51820)


def mk(rtt, loss=0.0):
    return [{"country": "DE", "host": "a", "median": rtt, "loss": loss, "jitter": 0}]


def test_country_stats_and_score_ordering():
    fast = {"matrix": scoring.country_stats(mk(15)), "swarm": {"success": 1.0},
            "handshake_s": 1.0, "mtu_ok": True}
    slow = {"matrix": scoring.country_stats(mk(150, 0.2)), "swarm": {"success": 0.6},
            "handshake_s": 6.0, "mtu_ok": False}
    w = scoring.DEFAULT_WEIGHTS
    assert scoring.score(fast, w)["total"] > scoring.score(slow, w)["total"]


def test_weights_change_ranking():
    a = {"matrix": scoring.country_stats(
        [{"country": "DE", "host": "x", "median": 10, "loss": 0, "jitter": 0},
         {"country": "NO", "host": "y", "median": 200, "loss": 0, "jitter": 0}]),
        "swarm": {"success": 1}, "handshake_s": 1, "mtu_ok": True}
    de_heavy = {"DE": 5.0, "NO": 0.1}
    no_heavy = {"DE": 0.1, "NO": 5.0}
    assert scoring.score(a, de_heavy)["total"] > scoring.score(a, no_heavy)["total"]


def test_error_scores_zero():
    assert scoring.score({"error": "x"}, scoring.DEFAULT_WEIGHTS)["total"] == 0.0


def test_subfolders_are_providers(tmp_path, monkeypatch):
    from app import db, runner
    (tmp_path / "CryptoStorm").mkdir()
    (tmp_path / "CryptoStorm" / "cs-poland.conf").write_text(CONF)
    (tmp_path / "Windscribe").mkdir()
    (tmp_path / "Windscribe" / "cs-poland.conf").write_text(CONF)  # same name, other provider
    (tmp_path / "top.conf").write_text(CONF)
    monkeypatch.setattr(db, "CONFIGS", tmp_path)
    found = runner.list_configs()
    assert len(found) == 3
    assert {runner.cid(p) for p in found} == {
        "CryptoStorm/cs-poland", "Windscribe/cs-poland", "top"}
    assert {runner.provider(p) for p in found} == {"CryptoStorm", "Windscribe", "example.net"}  # loose file: detected from its endpoint


def test_upload_validation_and_safety(tmp_path, monkeypatch):
    from app import configs, db
    monkeypatch.setattr(db, "CONFIGS", tmp_path)
    r = configs.save("vpns/CryptoStorm/cs-x.conf", CONF.encode())
    assert r["id"] == "CryptoStorm/cs-x"
    r = configs.save("evil.conf", CONF.encode(), provider_name="../../etc")
    assert (tmp_path / r["id"]).resolve().is_relative_to(tmp_path.resolve())
    for bad in ("notes.txt", "x.conf"):
        with pytest.raises(ValueError):
            configs.save(bad, b"garbage")
    d = configs.describe(tmp_path / "CryptoStorm" / "cs-x.conf")
    assert d["endpoint"] == "de-fra.example.net:51820" and "privatekey" not in str(d).lower()
    assert configs.delete("CryptoStorm/cs-x") and not (tmp_path / "CryptoStorm").exists()
    assert not configs.delete("../../etc/passwd")


def test_targets_yaml_norway(tmp_path, monkeypatch):
    from app import db, runner
    monkeypatch.setattr(db, "DATA", tmp_path)
    (tmp_path / "targets.yaml").write_text("targets:\n  NO:\n    - a.no\n  DE:\n    - b.de\n")
    assert {t["country"] for t in runner.load_targets()} == {"NO", "DE"}
    monkeypatch.setattr(db, "DATA", tmp_path / "none")
    assert "NO" in {t["country"] for t in runner.load_targets()}  # bundled file


def test_pause_gate():
    import asyncio
    from app import runner
    async def go():
        runner.STATE["running"] = True
        runner.pause()
        waiter = asyncio.create_task(runner.GATE.wait())
        await asyncio.sleep(0.05)
        assert not waiter.done() and runner.STATE["paused"]
        runner.resume()
        await asyncio.wait_for(waiter, 1)
        runner.STATE["running"] = False
    asyncio.run(go())


def test_missing_binary_is_a_tunnel_error():
    import asyncio
    from app.netns import TunnelError, run
    assert asyncio.run(run("definitely-not-a-binary", check=False)) == (127, "")
    with pytest.raises(TunnelError):
        asyncio.run(run("definitely-not-a-binary"))


def test_delete_run_and_result(tmp_path, monkeypatch):
    from app import db
    monkeypatch.setattr(db, "DATA", tmp_path)
    a, b = db.new_run("peering"), db.new_run("peering")
    for rid in (a, b):
        db.save_result(rid, "x/one", {"swarm": {}})
        db.save_result(rid, "x/two", {"swarm": {}})
    assert db.delete_result(a, "x/one") and not db.delete_result(a, "x/one")
    assert {r["name"] for r in db.get_run(a)["results"]} == {"x/two"}
    assert db.delete_run(a) and db.get_run(a) is None
    assert len(db.get_run(b)["results"]) == 2  # other run untouched


def test_history_build_orders_and_scores():
    from app import history, scoring
    def run(i, rtt, err=None):
        res = {"name": "p/a"}
        if err:
            res["error"] = err
        else:
            res.update(matrix=scoring.country_stats([{"country": "DE", "host": "x", "median": rtt, "loss": 0, "jitter": 0}]),
                       swarm={"success": 1.0}, handshake_s=0.3, mtu_ok=True)
        return {"id": i, "started": 1000 + i, "mode": "peering", "baseline": {}, "results": [res]}
    h = history.build([run(1, 20), run(2, 0, err="dead"), run(3, 90)], scoring.DEFAULT_WEIGHTS)["p/a"]
    assert [e["run"] for e in h] == [1, 2, 3]
    assert h[1]["score"] == 0.0 and h[1]["error"] == "dead" and h[1]["avg_rtt"] is None
    assert h[0]["score"] > h[2]["score"] > 0


def test_provider_detected_from_endpoint(tmp_path, monkeypatch):
    from app import configs, db
    monkeypatch.setattr(db, "CONFIGS", tmp_path)
    mk = lambda name, ep: (tmp_path / name).write_text(CONF.replace("de-fra.example.net:51820", ep))  # noqa: E731
    mk("a.conf", "berlin.cstorm.is:443")
    mk("b.conf", "ams-466-wg.whiskergalaxy.com:443")
    mk("c.conf", "1.2.3.4:51820")
    mk("d.conf", "node.unknownvpn.org:51820")
    (tmp_path / "Mine").mkdir()
    mk("Mine/e.conf", "berlin.cstorm.is:443")
    got = {p.name: configs.provider(p) for p in configs.list_paths()}
    assert got == {"a.conf": "CryptoStorm", "b.conf": "Windscribe", "c.conf": "Unbekannt",
                   "d.conf": "unknownvpn.org", "e.conf": "Mine"}


def test_throughput_median_ignores_collapsed_runs():
    """Regression: runs of [188, 1, 1] Mbit/s (rate limited) must not give a 1 Mbit/s median."""
    from app.worker import aggregate
    r = aggregate([{"mbps": 188.9, "stability": .9}, {"mbps": 1.0, "stability": 0}, {"mbps": 1.1, "stability": 0}])
    assert r["mbps"] == 188.9 and r["excluded"] == 2 and r["runs"] == [188.9, 1.0, 1.1]
    r = aggregate([{"mbps": 400, "stability": 1}, {"mbps": 450, "stability": 1}, {"mbps": 420, "stability": 1}])
    assert r["mbps"] == 420 and r["excluded"] == 0  # normal case: plain median
    assert aggregate([{"mbps": 0.0, "stability": 0}])["mbps"] == 0.0  # dead tunnel stays 0


def test_iperf_parsing_and_ports():
    from app.worker import iperf_mbps
    from app.runner import parse_ports
    down = {"end": {"sum_received": {"bits_per_second": 450e6}, "sum_sent": {"bits_per_second": 460e6}}}
    assert iperf_mbps(down, reverse=True) == 450.0      # download = what the client received
    assert iperf_mbps(down, reverse=False) == 460.0     # upload = what the client sent
    assert iperf_mbps({"end": {}}, True) is None
    assert parse_ports("5200-5203") == [5200, 5201, 5202, 5203] and parse_ports(5201) == [5201]


def test_regions_yaml_is_valid_and_has_norway_and_finland():
    from app import runner
    r = runner.load_regions()
    assert {"NO", "FI", "NL", "DE", "SE", "CH", "DK", "PL"} <= set(r)  # NO must not become False
    assert all(s["ports"] and s["host"] for srv in r.values() for s in srv)


def test_region_throughput_changes_score_and_worst_region_counts():
    from app import scoring
    def res(dk_up, with_regions=True):
        m = scoring.country_stats([{"country": c, "host": "x", "median": 20, "loss": 0, "jitter": 0} for c in ("NL", "DK")])
        r = {"matrix": m, "swarm": {"success": 1.0}, "handshake_s": 0.1, "mtu_ok": True}
        if with_regions:
            r["throughput"] = {"dl": {"mbps": 400, "stability": 1.0}, "ul": {"mbps": 180, "stability": 1.0},
                               "regions": {"NL": {"down": 400, "up": 180}, "DK": {"down": 50, "up": dk_up}}}
        return r
    base = {"dl": {"mbps": 450}, "ul": {"mbps": 190}}
    good, bad = scoring.score(res(180), scoring.DEFAULT_WEIGHTS, base), scoring.score(res(10), scoring.DEFAULT_WEIGHTS, base)
    assert good["peering"] > bad["peering"] + 10          # a weak country now shows in the score
    assert scoring.score(res(0, False), scoring.DEFAULT_WEIGHTS, base)["peering"] > 80  # no regions: latency only
    # an unmeasurable direction (None) is skipped, not counted as 0
    r = res(180); r["throughput"]["regions"]["DK"]["down"] = None
    assert scoring.score(r, scoring.DEFAULT_WEIGHTS, base)["peering"] > bad["peering"]
    # a broken baseline (2 Mbit/s) must not blow up the score: fall back to own measurement
    s = scoring.score(res(180), scoring.DEFAULT_WEIGHTS, {"dl": {"mbps": 2}, "ul": {"mbps": 2}})
    assert 0 < s["total"] <= 100
