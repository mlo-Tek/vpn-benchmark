"""Config management for the UI: list, upload, delete. Never exposes key material."""
import re
from pathlib import Path

from . import db
from .netns import parse_conf, split_endpoint

MAX_BYTES = 64 * 1024
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def clean(name: str, fallback: str) -> str:
    name = _SAFE.sub("_", name).strip("._")
    return name[:80] or fallback


def cid(path: Path) -> str:
    return str(path.relative_to(db.CONFIGS).with_suffix(""))


def provider(path: Path) -> str:
    parts = path.relative_to(db.CONFIGS).parts
    return parts[0] if len(parts) > 1 else "(root)"


def list_paths():
    return sorted(db.CONFIGS.rglob("*.conf")) if db.CONFIGS.exists() else []


def describe(path: Path) -> dict:
    out = {"id": cid(path), "provider": provider(path), "name": path.stem,
           "size": path.stat().st_size, "valid": True}
    try:
        c = parse_conf(path.read_text())
        host, port = split_endpoint(c["peer"]["endpoint"])
        out.update(endpoint=f"{host}:{port}", ipv6=":" in c["iface"].get("address", ""),
                   dns=c["iface"].get("dns", ""))
    except Exception as e:  # noqa: BLE001
        out.update(valid=False, error=str(e))
    return out


def _inside(path: Path) -> bool:
    root = db.CONFIGS.resolve()
    return root == path.resolve() or root in path.resolve().parents


def save(filename: str, data: bytes, provider_name: str = "") -> dict:
    """filename may carry a relative path (folder upload); its parent folder becomes the
    provider unless one is given explicitly."""
    parts = [p for p in filename.replace("\\", "/").split("/") if p]
    base = parts[-1] if parts else ""
    if not base.lower().endswith(".conf"):
        raise ValueError(f"{base or filename}: nur .conf-Dateien")
    if len(data) > MAX_BYTES:
        raise ValueError(f"{base}: Datei zu groß")
    try:
        parse_conf(data.decode("utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"{base}: {e}")
    prov = clean(provider_name, "") if provider_name else (
        clean(parts[-2], "") if len(parts) > 1 else "")
    target = db.CONFIGS / prov / (clean(base[:-5], "config") + ".conf")
    if not _inside(target):
        raise ValueError("ungültiger Pfad")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(0o600)
    db._own(target.parent)
    db._own(target)
    return {"id": cid(target), "provider": provider(target)}


def delete(config_id: str) -> bool:
    target = db.CONFIGS / (config_id + ".conf")
    if not _inside(target) or not target.is_file():
        return False
    target.unlink()
    parent = target.parent
    if parent != db.CONFIGS.resolve() and parent != db.CONFIGS and not any(parent.iterdir()):
        parent.rmdir()
    return True
