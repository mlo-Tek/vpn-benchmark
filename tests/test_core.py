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
