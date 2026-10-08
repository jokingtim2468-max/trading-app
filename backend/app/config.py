"""Runtime settings, read from environment variables (optionally a local .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    env = Path(__file__).resolve().parent.parent / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()


def _secret(name: str) -> str:
    """Docker-secrets friendly: NAME_FILE=/run/secrets/x wins over NAME=value."""
    path = os.environ.get(f"{name}_FILE")
    if path and Path(path).exists():
        return Path(path).read_text().strip()
    return os.environ.get(name, "")


def _bool(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(int(default))).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str = os.environ.get("BRIDGE_HOST", "127.0.0.1")
    port: int = int(os.environ.get("BRIDGE_PORT", "8765"))
    force_sim: bool = _bool("BRIDGE_SIM")
    api_token: str = _secret("BRIDGE_API_TOKEN")
    tick_poll_s: float = int(os.environ.get("TICK_POLL_MS", "100")) / 1000
    positions_poll_s: float = int(os.environ.get("POSITIONS_POLL_MS", "250")) / 1000
    external_poll_s: float = float(os.environ.get("EXTERNAL_POLL_S", "3"))
    max_deviation: int = int(os.environ.get("MAX_DEVIATION", "20"))
    mt5_path: str = os.environ.get("MT5_PATH", "")
    mt5_portable: bool = _bool("MT5_PORTABLE")
    # Docker mode: MT5 runs under Wine in the `mt5` container, reached over RPyC (mt5linux).
    mt5_rpyc_host: str = os.environ.get("MT5_RPYC_HOST", "")
    mt5_rpyc_port: int = int(os.environ.get("MT5_RPYC_PORT", "18812"))
    # Optional auto-login at startup so the app opens fully connected.
    mt5_login: str = _secret("MT5_LOGIN")
    mt5_password: str = _secret("MT5_PASSWORD")
    mt5_server: str = _secret("MT5_SERVER")
    tv_sessionid: str = _secret("TV_SESSIONID")
    tv_sessionid_sign: str = _secret("TV_SESSIONID_SIGN")
    allowed_origins: tuple[str, ...] = tuple(
        o.strip() for o in os.environ.get("BRIDGE_ALLOWED_ORIGINS", "").split(",") if o.strip())
    health_interval_s: float = float(os.environ.get("HEALTH_INTERVAL_S", "15"))
    # Override the MT5 data folder (Docker: the Wine prefix mounted read-only).
    mt5_data_dir: str = os.environ.get("MT5_DATA_DIR", "")
    frontend_dist: Path = Path(__file__).resolve().parents[2] / "frontend" / "dist"


settings = Settings()
