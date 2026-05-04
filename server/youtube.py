"""
youtube.py
All YouTube data fetching and downloading is isolated here behind plain
function signatures.  If yt-dlp ever stops working, only this file changes.

Public API
----------
get_search_results(query, page, per_page)  -> dict
get_video_detail(video_id)                 -> dict
download_video(video_id, max_res)          -> str   (local file path)
download_thumbnail(video_id, dest_path)    -> bool
"""

import os
import logging
import subprocess
import urllib.request
from typing import Optional

import yt_dlp
from PIL import Image

from .config import get_config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ydl_quiet_opts() -> dict:
    return {
        "quiet":           True,
        "no_warnings":     True,
        "noplaylist":      True,
        "extract_flat":    False,
    }


def _format_selector(max_res: int) -> str:
    """
    Pick the best single video+audio stream whose height <= max_res.
    Falls back to best available if nothing fits.
    """
    return (
        f"bestvideo[height<={max_res}][ext=mp4]+bestaudio[ext=m4a]"
        f"/bestvideo[height<={max_res}]+bestaudio"
        f"/best[height<={max_res}]"
        f"/best"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_search_results(query: str, page: int = 1, per_page: int = 5) -> dict:
    """
    Returns:
        {
            "total_pages": int,       # estimated; YouTube doesn't give exact counts
            "page":        int,
            "results": [
                {"id": str, "title": str, "thumb_url": str},
                ...
            ]
        }
    On error returns {"error": str}.
    """
    try:
        # We fetch enough results to serve the requested page.
        # yt-dlp search: "ytsearch<n>:query"
        fetch_count = page * per_page
        search_query = f"ytsearch{fetch_count}:{query}"

        opts = {
            **_ydl_quiet_opts(),
            "extract_flat": "in_playlist",
        }

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_query, download=False)

        entries = info.get("entries", [])
        if not entries:
            return {"error": "NO_RESULTS"}

        # Slice to the requested page window
        start = (page - 1) * per_page
        end   = start + per_page
        page_entries = entries[start:end]

        if not page_entries:
            return {"error": "NO_RESULTS"}

        total_fetched = len(entries)
        # Estimate total pages conservatively
        total_pages   = max(page, (total_fetched + per_page - 1) // per_page)
        # If we got a full page there are likely more results
        if len(page_entries) == per_page and total_fetched == fetch_count:
            total_pages = max(total_pages, page + 1)

        results = []
        for e in page_entries:
            vid_id = e.get("id") or e.get("url", "").split("v=")[-1]
            thumb  = _best_thumbnail(e.get("thumbnails") or [])
            results.append({
                "id":        vid_id,
                "title":     e.get("title", "Unknown Title"),
                "thumb_url": thumb,
            })

        return {
            "total_pages": total_pages,
            "page":        page,
            "results":     results,
        }

    except Exception as exc:
        logger.exception("get_search_results failed for query=%r", query)
        return {"error": str(exc)}


def get_video_detail(video_id: str) -> dict:
    """
    Returns:
        {
            "id":          str,
            "title":       str,
            "duration":    int,   # seconds
            "description": str,
            "thumb_url":   str,
        }
    On error returns {"error": str}.
    """
    try:
        url  = f"https://www.youtube.com/watch?v={video_id}"
        opts = {**_ydl_quiet_opts()}

        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        thumb = _best_thumbnail(info.get("thumbnails") or [])

        return {
            "id":          video_id,
            "title":       info.get("title",       ""),
            "duration":    info.get("duration",    0),
            "description": info.get("description", ""),
            "thumb_url":   thumb,
        }

    except Exception as exc:
        logger.exception("get_video_detail failed for video_id=%r", video_id)
        return {"error": str(exc)}


def download_video(video_id: str, max_res: int = 480) -> str:
    """
    Downloads the best source video at or below max_res to the configured
    video_store directory.

    Returns the local file path on success, raises on failure.
    The filename is  <video_id>_source.<ext>
    """
    cfg   = get_config()
    outtmpl = os.path.join(cfg.video_store, f"{video_id}_source.%(ext)s")

    opts = {
        **_ydl_quiet_opts(),
        "format":   _format_selector(max_res),
        "outtmpl":  outtmpl,
        "merge_output_format": "mp4",
    }

    logger.info("Downloading source for video_id=%r max_res=%d", video_id, max_res)

    with yt_dlp.YoutubeDL(opts) as ydl:
        info      = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}")
        ext       = info.get("ext", "mp4")

    # yt-dlp may merge to mp4 regardless of ext reported
    candidate_mp4 = os.path.join(cfg.video_store, f"{video_id}_source.mp4")
    candidate_ext = os.path.join(cfg.video_store, f"{video_id}_source.{ext}")

    for path in (candidate_mp4, candidate_ext):
        if os.path.isfile(path):
            logger.info("Source downloaded: %s", path)
            return path

    raise FileNotFoundError(
        f"yt-dlp finished but source file not found for {video_id}"
    )


def download_thumbnail(video_id: str, dest_path: str) -> bool:
    """
    Downloads and resizes the video thumbnail to dest_path as a JPEG.
    Target size comes from settings.ini [thumbnails] width/height (160×90).
    Returns True on success.
    """
    cfg = get_config()

    try:
        detail = get_video_detail(video_id)
        if "error" in detail:
            return False

        thumb_url = detail.get("thumb_url", "")
        if not thumb_url:
            return False

        # Fetch raw image bytes
        req = urllib.request.Request(
            thumb_url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()

        # Write temp file then open with Pillow for resize
        import io
        img = Image.open(io.BytesIO(raw)).convert("RGB")

        # Resize to exact thumbnail dimensions (may crop aspect ratio slightly)
        img = img.resize(
            (cfg.thumb_width, cfg.thumb_height),
            Image.LANCZOS,
        )

        os.makedirs(os.path.dirname(dest_path) if os.path.dirname(dest_path) else ".", exist_ok=True)
        img.save(dest_path, "JPEG", quality=70, optimize=True)
        logger.info("Thumbnail saved: %s (%dx%d)", dest_path, cfg.thumb_width, cfg.thumb_height)
        return True

    except Exception as exc:
        logger.exception("download_thumbnail failed for video_id=%r", video_id)
        return False


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _best_thumbnail(thumbnails: list) -> str:
    """
    Pick a thumbnail URL that is reasonably sized (prefer ~480px wide).
    Falls back to the last entry (usually highest res) if no good match.
    """
    if not thumbnails:
        return ""

    # Prefer a thumbnail with width in range 320-640
    for t in sorted(thumbnails, key=lambda x: x.get("width", 0)):
        w = t.get("width", 0)
        if 320 <= w <= 640:
            return t.get("url", "")

    # Fallback: last item (typically largest)
    return thumbnails[-1].get("url", "")