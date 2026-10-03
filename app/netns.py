"""WireGuard config parsing and per-config isolated network namespaces.

A namespace is held open by a `unshare -n -m` helper process (no bind mounts in
/var/run/netns needed). Probes enter it via `nsenter -t <pid> -n -m`.
"""
import asyncio
import os
import socket
import tempfile
import time
from pathlib import Path


def parse_conf(text: str) -> dict:
    iface, peer, cur = {}, {}, None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        low = line.lower()
        if low == "[interface]":
            cur = iface
        elif low == "[peer]":
            cur = peer if not peer else {}  # only the first peer is used
        elif "=" in line and cur is not None:
            k, v = line.split("=", 1)
            cur[k.strip().lower()] = v.strip()
    if "privatekey" not in iface or "publickey" not in peer or "endpoint" not in peer:
        raise ValueError("not a valid WireGuard client config")
    return {"iface": iface, "peer": peer}


def split_endpoint(ep: str):
    host, _, port = ep.rpartition(":")
    return host.strip("[]"), int(port)


def csv(v: str):
    return [x.strip() for x in v.split(",") if x.strip()]


class TunnelError(Exception):
    pass


async def run(*cmd, check=True, stdin=None):
    try:
        p = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        if check:
            raise TunnelError(f"command not found: {cmd[0]}")
        return 127, ""
    out, err = await p.communicate(stdin.encode() if stdin is not None else None)
    if check and p.returncode:
        raise TunnelError(f"{' '.join(cmd[:4])}: {err.decode().strip() or out.decode().strip()}")
    return p.returncode, out.decode()


class Tunnel:
    def __init__(self, path: Path, idx: int):
        self.path, self.name = path, path.stem
        self.ifname = f"wgb{idx}"
        self.conf = parse_conf(path.read_text())
        self.pid = None
        self.holder = None
        self.tmp = tempfile.mkdtemp(prefix="vpnb-")
        self.handshake_s = None

    async def ns(self, *cmd, check=True, stdin=None):
        return await run("nsenter", "-t", str(self.pid), "-n", "-m", "--", *cmd,
                         check=check, stdin=stdin)

    async def up(self):
        iface, peer = self.conf["iface"], self.conf["peer"]
        host, port = split_endpoint(peer["endpoint"])
        try:
            ip = socket.getaddrinfo(host, port, type=socket.SOCK_DGRAM)[0][4][0]
        except OSError as e:
            raise TunnelError(f"cannot resolve endpoint {host}: {e}")
        endpoint = f"[{ip}]:{port}" if ":" in ip else f"{ip}:{port}"

        dns = [d for d in csv(iface.get("dns", "")) if ":" not in d] or ["1.1.1.1"]
        resolv = os.path.join(self.tmp, "resolv.conf")
        Path(resolv).write_text("".join(f"nameserver {d}\n" for d in dns))
        ready = os.path.join(self.tmp, "ready")

        self.holder = await asyncio.create_subprocess_exec(
            "unshare", "-n", "-m", "sh", "-c",
            f"mount --bind '{resolv}' /etc/resolv.conf && touch '{ready}' && exec sleep 86400",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        self.pid = self.holder.pid
        for _ in range(50):
            if os.path.exists(ready):
                break
            if self.holder.returncode is not None:
                err = (await self.holder.stderr.read()).decode().strip()
                raise TunnelError(f"namespace setup failed (need NET_ADMIN+SYS_ADMIN): {err}")
            await asyncio.sleep(0.1)
        else:
            raise TunnelError("namespace setup timed out")

        wgconf = f"[Interface]\nPrivateKey = {iface['privatekey']}\n[Peer]\n" \
                 f"PublicKey = {peer['publickey']}\nEndpoint = {endpoint}\n" \
                 f"AllowedIPs = {peer.get('allowedips', '0.0.0.0/0, ::/0')}\n"
        if "presharedkey" in peer:
            wgconf += f"PresharedKey = {peer['presharedkey']}\n"
        if "persistentkeepalive" in peer:
            wgconf += f"PersistentKeepalive = {peer['persistentkeepalive']}\n"
        conf_file = os.path.join(self.tmp, "wg.conf")
        Path(conf_file).write_text(wgconf)
        os.chmod(conf_file, 0o600)

        await run("ip", "link", "add", self.ifname, "type", "wireguard")
        await run("ip", "link", "set", self.ifname, "netns", str(self.pid))
        await self.ns("wg", "setconf", self.ifname, conf_file)
        has_v6 = False
        for addr in csv(iface.get("address", "")):
            v6 = ":" in addr
            has_v6 |= v6
            await self.ns("ip", "addr", "add", addr, "dev", self.ifname, check=not v6)
        await self.ns("ip", "link", "set", "lo", "up")
        await self.ns("ip", "link", "set", self.ifname, "mtu", iface.get("mtu", "1420"), "up")
        for net in csv(peer.get("allowedips", "0.0.0.0/0, ::/0")):
            if ":" in net and not has_v6:
                continue
            await self.ns("ip", "route", "add", net, "dev", self.ifname, check=False)

    async def wait_handshake(self, timeout=10):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            await self.ns("ping", "-c", "1", "-W", "1", "1.1.1.1", check=False)
            _, out = await self.ns("wg", "show", self.ifname, "latest-handshakes")
            parts = out.split()
            if len(parts) == 2 and parts[1] != "0":
                self.handshake_s = time.monotonic() - t0
                return self.handshake_s
        raise TunnelError(f"no handshake within {timeout}s (endpoint down / key rejected?)")

    async def mtu_ok(self, payload=1392):
        rc, _ = await self.ns("ping", "-c", "2", "-W", "2", "-M", "do", "-s", str(payload),
                              "1.1.1.1", check=False)
        return rc == 0

    async def worker(self, cmd: str, args: dict, timeout=60):
        import json
        worker = str(Path(__file__).with_name("worker.py"))
        rc, out = await asyncio.wait_for(
            self.ns("python3", worker, cmd, stdin=json.dumps(args)), timeout)
        return json.loads(out)

    async def down(self):
        if self.holder and self.holder.returncode is None:
            self.holder.kill()
            await self.holder.wait()
        # the wg interface is destroyed together with its namespace
        await run("ip", "link", "del", self.ifname, check=False)
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)
