"""Probe worker. Runs INSIDE a network namespace (or on the host for the baseline).

Usage: python worker.py <cmd>   (JSON args on stdin, JSON result on stdout)
Pure stdlib so it can be started via `nsenter` without any extra setup.
"""
import asyncio
import json
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


async def info(_):
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


async def throughput(args):
    """Multi-stream down/up against Cloudflare's speed endpoints."""
    duration, streams = args.get("duration", 8), args.get("streams", 8)
    ctx = ssl.create_default_context()
    host = "speed.cloudflare.com"

    async def run(direction):
        counter = [0]
        stop = time.monotonic() + duration

        async def down():
            while time.monotonic() < stop:
                req = (f"GET /__down?bytes=25000000 HTTP/1.1\r\nHost: {host}\r\n"
                       "Connection: close\r\n\r\n").encode()
                try:
                    r, w = await https_request(host, req, ctx)
                    while time.monotonic() < stop:
                        chunk = await asyncio.wait_for(r.read(65536), 5)
                        if not chunk:
                            break
                        counter[0] += len(chunk)
                    w.close()
                except Exception:
                    await asyncio.sleep(0.2)

        async def up():
            size, block = 10_000_000, b"0" * 65536
            while time.monotonic() < stop:
                req = (f"POST /__up HTTP/1.1\r\nHost: {host}\r\nContent-Length: {size}\r\n"
                       "Content-Type: application/octet-stream\r\nConnection: close\r\n\r\n").encode()
                try:
                    r, w = await https_request(host, req, ctx)
                    sent = 0
                    while sent < size and time.monotonic() < stop:
                        n = min(len(block), size - sent)
                        w.write(block[:n])
                        await asyncio.wait_for(w.drain(), 5)
                        sent += n
                        counter[0] += n
                    w.close()
                except Exception:
                    await asyncio.sleep(0.2)

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
        return {"mbps": mbps, "stability": (min(steady) / mbps) if mbps else 0.0}

    return {"dl": await run("down"), "ul": await run("up")}


COMMANDS = {"matrix": matrix, "swarm": swarm, "info": info, "throughput": throughput}

if __name__ == "__main__":
    payload = json.loads(sys.stdin.read() or "{}")
    print(json.dumps(asyncio.run(COMMANDS[sys.argv[1]](payload))))
