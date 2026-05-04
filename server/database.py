"""
database.py
Per-user job/history database backed by a simple CSV file per user.
All access is through this module — callers never touch the CSV directly.

Schema (one row per requested video/format combination)
-------------------------------------------------------
job_id        : str   unique hex id for this job
video_id      : str   YouTube video id
title         : str   video title at request time
format_key    : str   e.g. qt_20k
status        : str   QUEUED | DOWNLOADING | CONVERTING | READY | ERROR
pct           : int   0-100 progress
size_kb       : int   0 until READY
ftp_path      : str   relative FTP path, empty until READY
requested_at  : str   ISO-8601 timestamp
error_msg     : str   empty unless ERROR
"""

import os
import secrets
import threading
import logging
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from .config import get_config

logger = logging.getLogger(__name__)

# One lock per user CSV to prevent concurrent write corruption
_locks: dict[str, threading.Lock] = {}
_locks_meta = threading.Lock()

COLUMNS = [
    "job_id", "video_id", "title", "format_key",
    "status", "pct", "size_kb", "ftp_path",
    "requested_at", "error_msg",
]

VALID_STATUSES = {"QUEUED", "DOWNLOADING", "CONVERTING", "READY", "ERROR"}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _db_path(username: str) -> str:
    cfg = get_config()
    return os.path.join(cfg.user_db_dir, f"{username}.csv")


def _get_lock(username: str) -> threading.Lock:
    with _locks_meta:
        if username not in _locks:
            _locks[username] = threading.Lock()
        return _locks[username]


def _load(username: str) -> pd.DataFrame:
    path = _db_path(username)
    if not os.path.isfile(path):
        return pd.DataFrame(columns=COLUMNS)
    try:
        df = pd.read_csv(path, dtype=str).fillna("")
        # Ensure all columns present (forward-compat)
        for col in COLUMNS:
            if col not in df.columns:
                df[col] = ""
        return df[COLUMNS]
    except Exception as exc:
        logger.error("Failed to load DB for %r: %s", username, exc)
        return pd.DataFrame(columns=COLUMNS)


def _save(username: str, df: pd.DataFrame) -> None:
    path = _db_path(username)
    df[COLUMNS].to_csv(path, index=False)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_user_jobs(username: str) -> list[dict]:
    """Return all jobs for a user as a list of dicts, newest first."""
    with _get_lock(username):
        df = _load(username)
    if df.empty:
        return []
    return df.iloc[::-1].to_dict(orient="records")


def get_job(username: str, job_id: str) -> Optional[dict]:
    """Return a single job dict or None."""
    with _get_lock(username):
        df = _load(username)
    row = df[df["job_id"] == job_id]
    if row.empty:
        return None
    return row.iloc[0].to_dict()


def find_job(username: str, video_id: str, format_key: str) -> Optional[dict]:
    """Find an existing job for this video+format combo, or None."""
    with _get_lock(username):
        df = _load(username)
    row = df[(df["video_id"] == video_id) & (df["format_key"] == format_key)]
    if row.empty:
        return None
    return row.iloc[0].to_dict()


def create_job(username: str, video_id: str, title: str, format_key: str) -> str:
    """
    Insert a new QUEUED job. Returns the new job_id.
    Caller should check find_job() first to avoid duplicates.
    """
    job_id = secrets.token_hex(4)   # 8-char hex, short enough for protocol

    new_row = {
        "job_id":       job_id,
        "video_id":     video_id,
        "title":        title,
        "format_key":   format_key,
        "status":       "QUEUED",
        "pct":          "0",
        "size_kb":      "0",
        "ftp_path":     "",
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "error_msg":    "",
    }

    with _get_lock(username):
        df = _load(username)
        df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
        _save(username, df)

    logger.info("Job created: user=%r job_id=%s video_id=%s fmt=%s",
                username, job_id, video_id, format_key)
    return job_id


def update_job(username: str, job_id: str, **kwargs) -> bool:
    """
    Update fields on an existing job.
    Allowed kwargs: status, pct, size_kb, ftp_path, error_msg
    Returns True on success, False if job not found.
    """
    allowed = {"status", "pct", "size_kb", "ftp_path", "error_msg"}
    bad = set(kwargs) - allowed
    if bad:
        raise ValueError(f"Unknown job fields: {bad}")

    if "status" in kwargs and kwargs["status"] not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {kwargs['status']!r}")

    with _get_lock(username):
        df = _load(username)
        mask = df["job_id"] == job_id
        if not mask.any():
            return False
        for k, v in kwargs.items():
            df.loc[mask, k] = str(v)
        _save(username, df)

    return True


def delete_job(username: str, job_id: str) -> int:
    """
    Delete a job row and return size_kb that was freed.
    Returns -1 if not found.
    """
    with _get_lock(username):
        df = _load(username)
        row = df[df["job_id"] == job_id]
        if row.empty:
            return -1
        size_kb = int(row.iloc[0].get("size_kb", 0) or 0)
        df = df[df["job_id"] != job_id]
        _save(username, df)

    return size_kb


def used_disk_kb(username: str) -> int:
    """Sum of size_kb for all READY jobs."""
    with _get_lock(username):
        df = _load(username)
    if df.empty:
        return 0
    ready = df[df["status"] == "READY"]
    return int(ready["size_kb"].apply(lambda x: int(x) if str(x).isdigit() else 0).sum())