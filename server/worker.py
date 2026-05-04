"""
worker.py
Thread pool that processes download + conversion jobs.
Each job goes through:  QUEUED -> DOWNLOADING -> CONVERTING -> READY (or ERROR)

The worker is intentionally simple:
  - One ThreadPoolExecutor sized from settings.ini
  - Jobs are submitted by the command handler (REQUEST command)
  - Progress is written back to the per-user CSV via database.py
  - Converted files land in  videos/<username>/
  - Thumbnails are pre-fetched here too if not already cached
"""

import os
import shlex
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .config import get_config
from .database import update_job, delete_job
from . import youtube as yt

logger = logging.getLogger(__name__)

_pool: ThreadPoolExecutor | None = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def start_pool() -> None:
    global _pool
    cfg  = get_config()
    _pool = ThreadPoolExecutor(
        max_workers  = cfg.thread_count,
        thread_name_prefix = "rtworker",
    )
    logger.info("Worker pool started (%d threads)", cfg.thread_count)


def stop_pool() -> None:
    global _pool
    if _pool:
        _pool.shutdown(wait=False)
        logger.info("Worker pool shut down")


# ---------------------------------------------------------------------------
# Submit a job
# ---------------------------------------------------------------------------

def submit_job(username: str, job_id: str, video_id: str, format_key: str) -> None:
    """Queue a download+convert job. Returns immediately."""
    if _pool is None:
        raise RuntimeError("Worker pool not started. Call start_pool() first.")
    _pool.submit(_run_job, username, job_id, video_id, format_key)
    logger.info("Job submitted: user=%r job_id=%s video_id=%s fmt=%s",
                username, job_id, video_id, format_key)


# ---------------------------------------------------------------------------
# Job execution (runs inside a worker thread)
# ---------------------------------------------------------------------------

def _run_job(username: str, job_id: str, video_id: str, format_key: str) -> None:
    cfg = get_config()
    fmt = cfg.formats.get(format_key)

    if fmt is None:
        _fail(username, job_id, f"Unknown format key: {format_key!r}")
        return

    # ---- ensure thumbnail is cached ----------------------------------------
    thumb_path = os.path.join(cfg.thumb_store, f"{video_id}.jpg")
    if not os.path.isfile(thumb_path):
        logger.info("[%s] Fetching thumbnail for %s", job_id, video_id)
        yt.download_thumbnail(video_id, thumb_path)

    # ---- DOWNLOADING --------------------------------------------------------
    update_job(username, job_id, status="DOWNLOADING", pct=0)

    try:
        source_path = _find_existing_source(video_id, cfg.video_store)

        if source_path:
            logger.info("[%s] Reusing cached source: %s", job_id, source_path)
            update_job(username, job_id, pct=50)
        else:
            logger.info("[%s] Downloading source for %s (max %dp)",
                        job_id, video_id, fmt.max_source_res)
            source_path = yt.download_video(video_id, fmt.max_source_res)
            update_job(username, job_id, pct=50)

    except Exception as exc:
        _fail(username, job_id, f"Download failed: {exc}")
        return

    # ---- CONVERTING ---------------------------------------------------------
    update_job(username, job_id, status="CONVERTING", pct=50)

    user_video_dir = os.path.join(cfg.video_store, username)
    os.makedirs(user_video_dir, exist_ok=True)

    out_filename = f"{video_id}_{format_key}{fmt.extension}"
    out_path     = os.path.join(user_video_dir, out_filename)

    try:
        _run_ffmpeg(source_path, fmt.ffmpeg_args, out_path, job_id)
    except Exception as exc:
        _fail(username, job_id, f"Conversion failed: {exc}")
        return

    # ---- READY --------------------------------------------------------------
    size_kb  = max(1, os.path.getsize(out_path) // 1024)
    ftp_path = f"videos/{username}/{out_filename}"

    update_job(
        username, job_id,
        status   = "READY",
        pct      = 100,
        size_kb  = size_kb,
        ftp_path = ftp_path,
    )

    logger.info("[%s] Job complete — %s (%d KB)", job_id, out_filename, size_kb)


# ---------------------------------------------------------------------------
# ffmpeg helper
# ---------------------------------------------------------------------------

def _run_ffmpeg(src: str, args_template: str, dest: str, job_id: str) -> None:
    """
    Build and run an ffmpeg command.
    args_template is the middle section from settings.ini (no -i or output).
    """
    # Strip any surrounding quotes that may come from the ini value
    args_str = args_template.strip().strip('"').strip("'")

    cmd = ["ffmpeg", "-y", "-i", src] + shlex.split(args_str) + [dest]

    logger.info("[%s] ffmpeg: %s", job_id, " ".join(cmd))

    result = subprocess.run(
        cmd,
        stdout = subprocess.PIPE,
        stderr = subprocess.PIPE,
        timeout = 3600,   # 1 hour max per conversion
    )

    if result.returncode != 0:
        err = result.stderr.decode(errors="replace")[-800:]   # last 800 chars
        raise RuntimeError(f"ffmpeg exited {result.returncode}: {err}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_existing_source(video_id: str, video_store: str) -> str | None:
    """Return path to already-downloaded source file, or None."""
    for ext in ("mp4", "mkv", "webm", "m4v"):
        candidate = os.path.join(video_store, f"{video_id}_source.{ext}")
        if os.path.isfile(candidate):
            return candidate
    return None


def _fail(username: str, job_id: str, msg: str) -> None:
    logger.error("[%s] Job failed: %s", job_id, msg)
    update_job(username, job_id, status="ERROR", error_msg=msg[:200])