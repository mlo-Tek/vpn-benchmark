"""Probe worker. Runs INSIDE a network namespace (or on the host for the baseline).

Usage: python worker.py <cmd>   (JSON args on stdin, JSON result on stdout)
Pure stdlib so it can be started via `nsenter` without any extra setup.
"""
import asyncio
import json
import random
import socket
import ssl
import statistics
import sys
import time


async def resolve(host):
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, 443, family=socket.AF_INET, type=socket.SOCK_STREAM), 4)
        return infos[0][4][0]
    except Exception:
        return None


async def tcp_rtt(ip, port=443, timeout=2.5):
    t = time.perf_counter()
    try:
        _, w = await asyncio.wait_for(asyncio.open_connection(ip, port), timeout)
    except Exception:
        return None
    dt = (time.perf_counter() - t) * 1000
    w.close()
    try:
        await w.wait_closed()
    except Exception:
        pass
    return dt


async def matrix(args):
    """TCP connect RTT to every target, all targets in parallel."""
    n = args.get("samples", 8)

    async def one(t):
        ip = await resolve(t["host"])
        rtts = []
        if ip:
            for _ in range(n):
                rtts.append(await tcp_rtt(ip))
                await asyncio.sleep(0.05)
        else:
            rtts = [None] * n
        ok = [r for r in rtts if r is not None]
        return {
            "country": t["country"], "host": t["host"],
            "median": statistics.median(ok) if ok else None,
            "jitter": statistics.pstdev(ok) if len(ok) > 1 else 0.0,
            "loss": 1 - len(ok) / n,
        }

    return await asyncio.gather(*(one(t) for t in args["targets"]))


async def swarm(args):
    """Many parallel TCP connects, simulating a peer swarm."""
    targets, total = args["targets"], args.get("total", 100)
    ips = [ip for ip in await asyncio.gather(*(resolve(t["host"]) for t in targets)) if ip]
    if not ips:
        return {"success": 0.0, "median_ms": None, "total": total}
    res = await asyncio.gather(*(tcp_rtt(ips[i % len(ips)], timeout=5) for i in range(total)))
    ok = [r for r in res if r is not None]
    return {"success": len(ok) / total, "median_ms": statistics.median(ok) if ok else None,
            "total": total}


async def https_request(host, request, ctx):
    r, w = await asyncio.open_connection(host, 443, ssl=ctx, server_hostname=host)
    w.write(request)
    await w.drain()
    return r, w


async def info(args):
    out = {}
    ctx = ssl.create_default_context()
    try:
        t = time.perf_counter()
        r, w = await asyncio.wait_for(https_request(
            "ipinfo.io", b"GET /json HTTP/1.1\r\nHost: ipinfo.io\r\nAccept: application/json\r\n"
            b"Connection: close\r\n\r\n", ctx), 8)
        data = await asyncio.wait_for(r.read(-1), 8)
        w.close()
        out["https_ms"] = (time.perf_counter() - t) * 1000
        body = json.loads(data.split(b"\r\n\r\n", 1)[1].decode())
        out.update(ip=body.get("ip"), country=body.get("country"), city=body.get("city"),
                   org=body.get("org"))
    except Exception as e:
        out["error"] = f"ipinfo failed: {e!r}"
    if args.get("skip_v6"):
        out["ipv6"] = None
    else:
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection("2606:4700:4700::1111", 443), 3)
            w.close()
            out["ipv6"] = True
        except Exception:
            out["ipv6"] = False
    t = time.perf_counter()
    out["dns_ok"] = (await resolve("www.wikipedia.org")) is not None
    out["dns_ms"] = (time.perf_counter() - t) * 1000
    return out


# Download sources. Several, because a single endpoint answers repeated heavy requests from
# the same IP (all tunnels of one provider share the exit IP) with 429/403 or throttles them.
DL_SOURCES = [("speed.cloudflare.com", "/__down?bytes=100000000"),
              ("nbg1-speed.hetzner.com", "/100MB.bin"),
              ("proof.ovh.net", "/files/100Mb.dat"),
              ("fsn1-speed.hetzner.com", "/100MB.bin")]  # all checked: HTTPS, HTTP 200, no auth


async def read_status(r):
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 8)
    return int(head.split(b" ", 2)[1])


def aggregate(runs):
    """Median of the valid runs. A run that collapsed to <15% of the best one is a
    throttled/refused run, not a result, and is left out (but still listed)."""
    best = max(r["mbps"] for r in runs)
    good = [r for r in runs if r["mbps"] >= 0.15 * best] or runs
    med = sorted(r["mbps"] for r in good)[len(good) // 2]
    pick = min(good, key=lambda r: abs(r["mbps"] - med))
    return {**pick, "runs": [round(r["mbps"], 1) for r in runs], "excluded": len(runs) - len(good)}


async def throughput(args):
    """Multi-stream down/up. Only HTTP 200 responses count as data; rejected requests are
    counted separately so a rate-limited run can be recognised and excluded."""
    duration, streams = args.get("duration", 8), args.get("streams", 6)
    ctx = ssl.create_default_context()
    up_host = "speed.cloudflare.com"
    cooldown = {}   # source index -> time until which it counts as rate limited (kept across repeats)
    got = {}        # source index -> bytes it delivered so far: the best source is tried first

    async def run(direction, run_idx):
        counter, rejected, by_src = [0], {}, {}
        stop = time.monotonic() + duration
        n = len(DL_SOURCES)

        def pick():
            now = time.monotonic()
            free = [i for i in range(n) if cooldown.get(i, 0) <= now]
            return max(free, key=lambda i: (got.get(i, 0), -i)) if free else None

        async def down():
            while time.monotonic() < stop:
                i = pick()
                if i is None:  # every source is cooling down
                    await asyncio.sleep(0.5)
                    continue
                host, path = DL_SOURCES[i]
                req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUser-Agent: curl/8\r\n"
                       "Connection: close\r\n\r\n").encode()
                try:
                    r, w = await https_request(host, req, ctx)
                    status = await read_status(r)
                    if status != 200:
                        rejected[status] = rejected.get(status, 0) + 1
                        cooldown[i] = time.monotonic() + (30 if status == 429 else 10)
                        w.close()
                        await asyncio.sleep(0.2)
                        continue
                    while time.monotonic() < stop:
                        chunk = await asyncio.wait_for(r.read(65536), 5)
                        if not chunk:
                            break
                        counter[0] += len(chunk)
                        by_src[host] = by_src.get(host, 0) + len(chunk)
                        got[i] = got.get(i, 0) + len(chunk)
                    w.close()
                except Exception:
                    cooldown[i] = time.monotonic() + 10
                    await asyncio.sleep(0.3)

        async def up():
            size, block = 10_000_000, b"0" * 65536
            while time.monotonic() < stop:
                req = (f"POST /__up HTTP/1.1\r\nHost: {up_host}\r\nContent-Length: {size}\r\n"
                       "Content-Type: application/octet-stream\r\nConnection: close\r\n\r\n").encode()
                try:
                    r, w = await https_request(up_host, req, ctx)
                    sent = 0
                    while sent < size and time.monotonic() < stop:
                        n = min(len(block), size - sent)
                        w.write(block[:n])
                        await asyncio.wait_for(w.drain(), 5)
                        sent += n
                        counter[0] += n
                    w.close()
                except Exception:
                    await asyncio.sleep(0.3)

        async def sampler():
            samples, last = [], 0
            while time.monotonic() < stop:
                await asyncio.sleep(1)
                samples.append((counter[0] - last) * 8 / 1e6)
                last = counter[0]
            return samples

        worker = down if direction == "down" else up
        tasks = [asyncio.create_task(worker()) for _ in range(streams)]
        samples = await sampler()
        for t in tasks:
            t.cancel()
        steady = samples[2:] or samples  # drop TCP slow-start warmup
        mbps = statistics.mean(steady) if steady else 0.0
        out = {"mbps": mbps, "stability": (min(steady) / mbps) if mbps else 0.0}
        if direction == "down":
            if by_src:
                out["source"] = max(by_src, key=by_src.get)  # where most of the data came from
            if rejected:
                out["rejected"] = rejected
        return out

    reps, out = max(1, args.get("repeats", 1)), {}
    for direction, key in (("down", "dl"), ("up", "ul")):
        runs = []
        for i in range(reps):
            if i and direction == "down":
                await asyncio.sleep(15)  # public servers throttle repeated downloads per IP
            runs.append(await run(direction, i))
        out[key] = aggregate(runs)
    return out


IPERF_OUTLIER_MBPS = 20.0  # below this a result is re-measured and the median is used


def iperf_mbps(data, reverse):
    """Mbit/s out of an `iperf3 -J` document (reverse = download = what the client received)."""
    end = data.get("end", {})
    bucket = end.get("sum_received" if reverse else "sum_sent", {})
    bps = (bucket.get("bits_per_second") or end.get("sum_received", {}).get("bits_per_second")
           or end.get("sum_sent", {}).get("bits_per_second"))
    return round(float(bps) / 1e6, 2) if bps else None


async def open_ports(host, ports, timeout=2.0):
    """Public servers often run only a few ports of a big range: find the open ones first."""
    async def one(p):
        try:
            _, w = await asyncio.wait_for(asyncio.open_connection(host, p), timeout)
            w.close()
            return p
        except Exception:  # noqa: BLE001
            return None
    return [p for p in await asyncio.gather(*(one(p) for p in ports)) if p]


async def iperf_once(host, ports, reverse, parallel, duration, tries=3):
    ports = list(ports)
    if len(ports) > 4:
        ports = await open_ports(host, ports) or ports
    random.shuffle(ports)
    errors = []
    for port in ports[:tries]:
        cmd = ["iperf3", "-c", host, "-4", "-p", str(port), "--connect-timeout", "4000",
               "-P", str(parallel), "-t", str(duration), "-J"] + (["-R"] if reverse else [])
        try:
            p = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except FileNotFoundError:
            return {"ok": False, "error": "iperf3 ist nicht installiert"}
        try:
            out, _ = await asyncio.wait_for(p.communicate(), duration + 12)
        except asyncio.TimeoutError:
            p.kill()
            errors.append(f"{port}: Timeout")
            continue
        try:
            data = json.loads(out)
        except Exception:  # noqa: BLE001
            errors.append(f"{port}: keine Antwort")
            continue
        if data.get("error"):
            errors.append(f"{port}: {str(data['error'])[:70]}")
            continue
        mbps = iperf_mbps(data, reverse)
        if mbps is None:
            errors.append(f"{port}: keine Bitrate")
            continue
        return {"ok": True, "mbps": mbps, "host": host, "port": port}
    return {"ok": False, "error": " | ".join(errors[-2:]) or "kein Port erreichbar"}


async def iperf_stable(host, ports, reverse, parallel, duration):
    """A suspiciously low single result is measured twice more; the median counts."""
    first = await iperf_once(host, ports, reverse, parallel, duration)
    if not first["ok"] or first["mbps"] >= IPERF_OUTLIER_MBPS:
        return first
    samples = [first] + [await iperf_once(host, ports, reverse, parallel, duration) for _ in range(2)]
    vals = [x["mbps"] for x in samples if x["ok"]]
    return {**first, "mbps": statistics.median(vals), "rechecked": True,
            "samples": [x.get("mbps") for x in samples]}


async def region(args):
    """Download and upload to one country, using the first of its public servers that answers."""
    out = {"country": args["country"]}
    for key, reverse in (("down", True), ("up", False)):
        last = None
        for srv in args["servers"]:
            last = await iperf_stable(srv["host"], srv["ports"], reverse,
                                      args.get("parallel", 4), args.get("duration", 3))
            if last["ok"]:
                out[key], out[key + "_server"] = last["mbps"], srv["host"]
                if last.get("rechecked"):
                    out[key + "_samples"] = last["samples"]
                break
        else:
            out[key], out[key + "_error"] = None, (last or {}).get("error", "kein Server")
    return out


COMMANDS = {"region": region, "matrix": matrix, "swarm": swarm, "info": info, "throughput": throughput}

if __name__ == "__main__":
    payload = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(asyncio.run(COMMANDS[sys.argv[1]](payload))))
