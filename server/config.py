"""
config.py
Loads and exposes settings.ini and users.ini as typed, easy-to-use objects.
All other modules import from here — never parse ini files themselves.
"""

import configparser
import os
import logging
from dataclasses import dataclass, field
from typing import Dict

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FormatDef:
    key: str
    label: str
    ffmpeg_args: str
    extension: str          # includes leading dot, e.g. ".mov"
    max_source_res: int     # e.g. 480


@dataclass
class UserDef:
    username: str
    password: str
    quota_mb: int


@dataclass
class ServerConfig:
    # [server]
    tcp_port: int
    ftp_port: int
    host: str
    max_clients: int
    log_level: str

    # [paths]
    video_store: str
    thumb_store: str
    user_db_dir: str
    log_dir: str

    # [ftp]
    ftp_user: str
    ftp_password: str

    # [thumbnails]
    thumb_width: int
    thumb_height: int

    # [workers]
    thread_count: int

    # [formats]  key -> FormatDef
    formats: Dict[str, FormatDef] = field(default_factory=dict)

    # users.ini  username -> UserDef
    users: Dict[str, UserDef] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_config(
    settings_path: str = "settings.ini",
    users_path: str = "users.ini",
) -> ServerConfig:
    """Parse both ini files and return a fully populated ServerConfig."""

    s = configparser.ConfigParser()
    if not s.read(settings_path):
        raise FileNotFoundError(f"Cannot read settings file: {settings_path}")

    u = configparser.ConfigParser()
    if not u.read(users_path):
        raise FileNotFoundError(f"Cannot read users file: {users_path}")

    # ---- [server] ----------------------------------------------------------
    srv = s["server"]
    pth = s["paths"]
    ftp = s["ftp"]
    tmb = s["thumbnails"]
    wrk = s["workers"]

    cfg = ServerConfig(
        tcp_port     = int(srv.get("tcp_port",    "40123")),
        ftp_port     = int(srv.get("ftp_port",    "40124")),
        host         = srv.get("host",            "0.0.0.0"),
        max_clients  = int(srv.get("max_clients", "10")),
        log_level    = srv.get("log_level",       "INFO"),

        video_store  = pth.get("video_store",  "./videos"),
        thumb_store  = pth.get("thumb_store",  "./thumbs"),
        user_db_dir  = pth.get("user_db_dir",  "./userdata"),
        log_dir      = pth.get("log_dir",      "./logs"),

        ftp_user     = ftp.get("ftp_user",     "share"),
        ftp_password = ftp.get("ftp_password", "changeme"),

        thumb_width  = int(tmb.get("width",  "160")),
        thumb_height = int(tmb.get("height", "90")),

        thread_count = int(wrk.get("thread_count", "4")),
    )

    # ---- [formats] ---------------------------------------------------------
    for key, raw in s["formats"].items():
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 4:
            raise ValueError(
                f"Format '{key}' in settings.ini must have exactly 4 pipe-"
                f"separated fields (label|ffmpeg_args|extension|max_res). "
                f"Got: {raw!r}"
            )
        label, ffmpeg_args, ext, max_res = parts
        cfg.formats[key] = FormatDef(
            key            = key,
            label          = label,
            ffmpeg_args    = ffmpeg_args,
            extension      = ext,
            max_source_res = int(max_res),
        )

    # ---- users.ini ---------------------------------------------------------
    for username in u.sections():
        cfg.users[username.lower()] = UserDef(
            username = username.lower(),
            password = u[username].get("password", ""),
            quota_mb = int(u[username].get("quota_mb", "100")),
        )

    # ---- ensure directories exist ------------------------------------------
    for directory in (cfg.video_store, cfg.thumb_store, cfg.user_db_dir, cfg.log_dir):
        os.makedirs(directory, exist_ok=True)

    return cfg


# ---------------------------------------------------------------------------
# Module-level singleton — import and call get_config() everywhere
# ---------------------------------------------------------------------------

_config: ServerConfig | None = None


def get_config() -> ServerConfig:
    global _config
    if _config is None:
        raise RuntimeError("Config not loaded. Call init_config() first.")
    return _config


def get_lan_ip() -> str:
    """
    Return the LAN IP address the server is reachable on.
    Used to build FTP URLs sent to clients.
    Falls back to 127.0.0.1 if detection fails.
    """
    import socket as _socket
    try:
        # Trick: connect a UDP socket (no data sent) to resolve outbound IP
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def init_config(settings_path: str = "settings.ini", users_path: str = "users.ini") -> ServerConfig:
    global _config
    _config = load_config(settings_path, users_path)
    return _config
