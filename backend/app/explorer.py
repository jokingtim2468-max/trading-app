"""Read-only browser for the MT5 data folder (MQL5/Experts, Indicators, Scripts, Presets).

Never writes, never executes, and refuses any path that escapes <data_dir>/MQL5.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import settings

SECTIONS = ("Experts", "Indicators", "Scripts", "Include", "Presets")
TEXT_EXT = {".mq5", ".mq4", ".mqh", ".set", ".txt", ".log", ".csv", ".ini", ".json"}
MAX_READ = 512 * 1024
MAX_ENTRIES = 5000
SAMPLE_DIR = Path(__file__).resolve().parent.parent / "sample_mql5"


async def resolve_data_dir(engine: Any) -> Path:
    if settings.mt5_data_dir:
        return Path(settings.mt5_data_dir)
    if engine.sim:
        return SAMPLE_DIR
    if not engine.connected:
        raise ValueError("connect MT5 first (or set MT5_DATA_DIR)")
    return Path(await engine.data_path())


def _mql5(root: Path) -> Path:
    base = (root / "MQL5").resolve()
    if not base.is_dir():
        raise ValueError(f"MQL5 folder not found under {root}")
    return base


def list_tree(root: Path) -> dict:
    base = _mql5(root)
    budget = [MAX_ENTRIES]

    def walk(p: Path, depth: int) -> dict:
        node = {"name": p.name, "path": p.relative_to(base).as_posix(), "type": "dir", "children": []}
        if depth > 6:
            return node
        try:
            entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        except OSError:
            return node
        for e in entries:
            if budget[0] <= 0:
                break
            budget[0] -= 1
            if e.is_symlink():
                continue
            if e.is_dir():
                node["children"].append(walk(e, depth + 1))
            else:
                st = e.stat()
                node["children"].append({"name": e.name, "path": e.relative_to(base).as_posix(), "type": "file",
                                         "size": st.st_size, "mtime": int(st.st_mtime),
                                         "kind": _kind(e)})
        return node

    return {"root": str(root), "sections": [walk(base / s, 0) for s in SECTIONS if (base / s).is_dir()]}


def _kind(p: Path) -> str:
    return {".mq5": "source", ".mq4": "source", ".mqh": "header", ".ex5": "compiled", ".ex4": "compiled",
            ".set": "preset"}.get(p.suffix.lower(), "file")


def read_file(root: Path, rel: str) -> dict:
    base = _mql5(root)
    target = (base / rel).resolve()
    if base not in target.parents or not target.is_file():
        raise ValueError("invalid path")
    st = target.stat()
    info = {"path": target.relative_to(base).as_posix(), "size": st.st_size, "mtime": int(st.st_mtime),
            "kind": _kind(target)}
    if target.suffix.lower() not in TEXT_EXT:
        return {**info, "binary": True, "text": ""}
    raw = target.read_bytes()[:MAX_READ]
    # MetaEditor frequently saves UTF-16LE with a BOM.
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    return {**info, "binary": False, "text": text, "truncated": st.st_size > MAX_READ}
