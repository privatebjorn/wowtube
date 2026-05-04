"""
handlers.py
One handler function per protocol command.

Each function receives the already-split token fields (list of strings)
and returns a complete wire response string WITHOUT the trailing CRLF
(the TCP server adds that).

Convention
----------
  handle_XYZ(parts: list[str]) -> str

  parts[0] is always the command name (already upper-cased by the dispatcher).
  For authenticated commands parts[1] is the session token.

All handlers are pure functions with no socket I/O — easy to unit test.

Status vocabulary (5 words used everywhere)
-------------------------------------------
  QUEUED       job is waiting in the worker pool
  DOWNLOADING  source video is being fetched from YouTube
  CONVERTING   ffmpeg is running
  AVAILABLE    file is ready on the FTP server
  ERROR        something went wrong (job can be re-requested)

Form → command mapping
----------------------
  LoginForm      LOGIN
  SearchForm     SEARCH
  DescriptionForm DETAIL, REQUEST
  MyVideosForm   MYVIDEOS, JOBSTATUS, DELETE, STREAMURL
  PlayerForm     STREAMURL  (full FTP URL returned, plug straight into OpenURLMovie)
"""

import logging
import os

from . import auth
from . import database as db
from . import youtube as yt
from . import worker
from .config import get_config

logger = logging.getLogger(__name__)

PIPE          = "|"
RESULTS_PER_PAGE = 3          # matches the 3-thumbnail array in SearchForm

# Internal DB status  ->  wire status sent to client
_STATUS_MAP = {
    "QUEUED":      "QUEUED",
    "DOWNLOADING": "DOWNLOADING",
    "CONVERTING":  "CONVERTING",
    "READY":       "AVAILABLE",   # "READY" internally, "AVAILABLE" on the wire
    "ERROR":       "ERROR",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ok(*parts) -> str:
    return PIPE.join(["OK"] + [str(p) for p in parts])


def _err(*parts) -> str:
    return PIPE.join(["ERR"] + [str(p) for p in parts])


def _require_auth(parts: list[str], min_len: int = 2) -> tuple[str | None, str | None]:
    """
    Validate token in parts[1].
    Returns (username, None) on success or (None, error_response) on failure.
    """
    if len(parts) < min_len:
        return None, _err("MALFORMED_REQUEST")
    username = auth.validate_token(parts[1])
    if username is None:
        return None, _err("AUTH_FAILED")
    return username, None


def _encode_description(text: str) -> str:
    """Replace newlines with {{NL}} so the description fits on one wire line."""
    return text.replace("\r\n", "{{NL}}").replace("\r", "{{NL}}").replace("\n", "{{NL}}")


def _wire_status(db_status: str) -> str:
    """Translate internal DB status to the 5-word client vocabulary."""
    return _STATUS_MAP.get(db_status, "ERROR")


def _ftp_url(relative_path: str) -> str:
    """
    Build a full FTP URL the RealBasic client can pass directly to OpenURLMovie().
    e.g.  ftp://share:NetShare123@192.168.1.10:40124/videos/alice/abc_qt_20k.mov
    Uses the actual LAN IP (not 0.0.0.0) and the FTP port from settings.ini
    so remote clients can reach it without any hardcoding on the client side.
    """
    from .config import get_lan_ip
    cfg = get_config()
    ip  = get_lan_ip()
    return f"ftp://{cfg.ftp_user}:{cfg.ftp_password}@{ip}:{cfg.ftp_port}/{relative_path}"


def _thumb_ftp_url(video_id: str) -> str:
    """Full FTP URL for a thumbnail — client can use directly in OpenURLMovie()."""
    return _ftp_url(f"thumbs/{video_id}.jpg")


# ---------------------------------------------------------------------------
# LOGIN
# ---------------------------------------------------------------------------

def handle_login(parts: list[str]) -> str:
    """
    LOGIN|<user>|<pass>
    -> OK|TOKEN:<token>|FORMATS:<n>|<key1>:<label1>|...
    -> ERR|INVALID_CREDENTIALS
    -> ERR|SERVER_FULL
    """
    if len(parts) < 3:
        return _err("MALFORMED_REQUEST")

    cfg      = get_config()
    username = parts[1].strip()
    password = parts[2].strip()

    # Check server capacity (rough guard — each TCP connection is one client)
    if auth.active_session_count() >= cfg.max_clients:
        return _err("SERVER_FULL")

    token = auth.validate_login(username, password)
    if token is None:
        return _err("INVALID_CREDENTIALS")

    # Build format list for handshake
    fmt_parts = [f"{k}:{v.label}" for k, v in cfg.formats.items()]
    fmt_count = len(fmt_parts)

    return _ok(f"TOKEN:{token}", f"FORMATS:{fmt_count}", *fmt_parts)


# ---------------------------------------------------------------------------
# SEARCH
# ---------------------------------------------------------------------------

def handle_search(parts: list[str]) -> str:
    """
    SEARCH|<token>|<query>|<page>

    Returns exactly 3 results per page to match the 3 MoviePlayer/title
    arrays in SearchForm. Empty strings pad slots when fewer results exist.

    -> OK|RESULTS|<total_pages>|<id1>|<title1>|<thumb_ftp_url1>|
                               |<id2>|<title2>|<thumb_ftp_url2>|
                               |<id3>|<title3>|<thumb_ftp_url3>
    -> ERR|NO_RESULTS
    -> ERR|AUTH_FAILED

    RealBasic usage (SearchSocket DataAvailable):
      Dim parts() As String = Split(data, "|")
      ' parts(2) = total_pages
      ' Slot i (0-based): id=parts(3+i*3), title=parts(4+i*3), thumb=parts(5+i*3)
      For i = 0 To 2
        s_result_title(i).Text  = parts(4 + i*3)
        m_result_thumbnail(i).Movie = OpenURLMovie(parts(5 + i*3))
      Next
    """
    username, err = _require_auth(parts, min_len=3)
    if err:
        return err

    if len(parts) < 4:
        return _err("MALFORMED_REQUEST")

    query = parts[2].strip()
    try:
        page = max(1, int(parts[3]))
    except ValueError:
        return _err("MALFORMED_REQUEST")

    logger.info("SEARCH user=%r query=%r page=%d", username, query, page)
    result = yt.get_search_results(query, page=page, per_page=RESULTS_PER_PAGE)

    if "error" in result:
        return _err("NO_RESULTS")

    results     = result["results"]
    total_pages = result["total_pages"]

    # Always emit exactly RESULTS_PER_PAGE triplets; pad with empty strings
    cfg   = get_config()
    flat  = []
    for i in range(RESULTS_PER_PAGE):
        if i < len(results):
            r         = results[i]
            vid_id    = r["id"]
            title     = r["title"].replace("|", "-")
            thumb_url = _thumb_ftp_url(vid_id)

            # Pre-fetch thumbnail in background so it's ready when client asks
            thumb_path = os.path.join(cfg.thumb_store, f"{vid_id}.jpg")
            if not os.path.isfile(thumb_path) and r.get("thumb_url"):
                import threading
                threading.Thread(
                    target = yt.download_thumbnail,
                    args   = (vid_id, thumb_path),
                    daemon = True,
                ).start()

            flat += [vid_id, title, thumb_url]
        else:
            flat += ["", "", ""]

    return _ok("RESULTS", total_pages, *flat)


# ---------------------------------------------------------------------------
# DETAIL
# ---------------------------------------------------------------------------

def handle_detail(parts: list[str]) -> str:
    """
    DETAIL|<token>|<video_id>

    Returns everything DescriptionForm needs in a single atomic response:
    title, duration, description, and the thumbnail FTP URL.

    -> OK|DETAIL|<video_id>|<title>|<duration_secs>|<thumb_ftp_url>|<description>
    -> ERR|NOT_FOUND
    -> ERR|AUTH_FAILED

    RealBasic usage (DetailSocket DataAvailable):
      Dim parts() As String = Split(data, "|")
      s_result_title.Text       = parts(3)
      ' parts(4) = duration in seconds (ignored or formatted as you like)
      m_result_thumbnail.Movie  = OpenURLMovie(parts(5))
      s_result_description.Text = ReplaceAll(parts(6), "{{NL}}", Chr(13))
    """
    username, err = _require_auth(parts, min_len=3)
    if err:
        return err

    video_id = parts[2].strip()
    logger.info("DETAIL user=%r video_id=%r", username, video_id)

    info = yt.get_video_detail(video_id)
    if "error" in info:
        return _err("NOT_FOUND")

    title       = info["title"].replace("|", "-")
    duration    = int(info.get("duration") or 0)
    description = _encode_description(info.get("description", ""))
    description = description[:2000]   # cap for TextArea
    thumb_url   = _thumb_ftp_url(video_id)

    # Ensure thumbnail is cached for the FTP URL to resolve
    cfg        = get_config()
    thumb_path = os.path.join(cfg.thumb_store, f"{video_id}.jpg")
    if not os.path.isfile(thumb_path):
        import threading
        threading.Thread(
            target = yt.download_thumbnail,
            args   = (video_id, thumb_path),
            daemon = True,
        ).start()

    return _ok("DETAIL", video_id, title, duration, thumb_url, description)


# ---------------------------------------------------------------------------
# REQUEST
# ---------------------------------------------------------------------------

def handle_request(parts: list[str]) -> str:
    """
    REQUEST|<token>|<video_id>|<format_key>

    format_key must be one of the keys received in the LOGIN handshake
    (e.g. qt_20k, qt_40k, mp3_128).

    -> OK|QUEUED|<job_id>
    -> OK|AVAILABLE|<job_id>      already converted, ready to stream/save
    -> ERR|ALREADY_QUEUED|<job_id>
    -> ERR|QUOTA_EXCEEDED|<used_mb>|<total_mb>
    -> ERR|UNKNOWN_FORMAT
    -> ERR|AUTH_FAILED

    RealBasic usage (DetailSocket DataAvailable after b_request click):
      Dim parts() As String = Split(data, "|")
      If parts(0) = "OK" Then
        MsgBox "Request " + parts(1) + " — job " + parts(2)
      Else
        MsgBox "Error: " + parts(1)
      End If
    """
    username, err = _require_auth(parts, min_len=4)
    if err:
        return err

    video_id   = parts[2].strip()
    format_key = parts[3].strip()
    cfg        = get_config()

    if format_key not in cfg.formats:
        return _err("UNKNOWN_FORMAT")

    # Check for existing job
    existing = db.find_job(username, video_id, format_key)
    if existing:
        status = existing["status"]
        job_id = existing["job_id"]
        if status == "READY":
            return _ok("AVAILABLE", job_id)
        elif status == "ERROR":
            # Allow re-request after error — drop old row and fall through
            db.delete_job(username, job_id)
        else:
            return _err("ALREADY_QUEUED", job_id)

    # Quota check
    user_def = cfg.users.get(username)
    quota_mb = user_def.quota_mb if user_def else 100
    used_kb  = db.used_disk_kb(username)
    used_mb  = used_kb // 1024
    if used_mb >= quota_mb:
        return _err("QUOTA_EXCEEDED", used_mb, quota_mb)

    # Fetch title for the job record (best-effort; falls back to video_id)
    info  = yt.get_video_detail(video_id)
    title = info.get("title", video_id).replace("|", "-") if "error" not in info else video_id

    job_id = db.create_job(username, video_id, title, format_key)
    worker.submit_job(username, job_id, video_id, format_key)

    logger.info("REQUEST queued: user=%r job_id=%s video_id=%s fmt=%s",
                username, job_id, video_id, format_key)
    return _ok("QUEUED", job_id)


# ---------------------------------------------------------------------------
# STATUS
# ---------------------------------------------------------------------------

def handle_jobstatus(parts: list[str]) -> str:
    """
    JOBSTATUS|<token>|<job_id>

    Lightweight single-job poll. MyVideosForm calls this periodically for any
    row whose status is not yet AVAILABLE or ERROR, then updates just that row.

    -> OK|JOBSTATUS|<job_id>|<status>|<pct>
       status is one of: QUEUED DOWNLOADING CONVERTING AVAILABLE ERROR
       pct is 0-100

    -> ERR|UNKNOWN_JOB
    -> ERR|AUTH_FAILED

    RealBasic usage (ContentSocket DataAvailable on a timer):
      Dim parts() As String = Split(data, "|")
      ' Find the matching ListBox row by job_id stored in column 0
      Dim i As Integer
      For i = 0 To l_myvideos.ListCount - 1
        If l_myvideos.Cell(i, 0) = parts(2) Then
          l_myvideos.Cell(i, 3) = parts(3)   ' Status column
          Exit For
        End If
      Next
    """
    username, err = _require_auth(parts, min_len=3)
    if err:
        return err

    job_id = parts[2].strip()
    job    = db.get_job(username, job_id)
    if job is None:
        return _err("UNKNOWN_JOB")

    return _ok("JOBSTATUS", job_id, _wire_status(job["status"]), job["pct"])


# ---------------------------------------------------------------------------
# MYVIDEOS
# ---------------------------------------------------------------------------

def handle_myvideos(parts: list[str]) -> str:
    """
    MYVIDEOS|<token>

    Returns all jobs for the user. Each job maps directly to one row in the
    4-column ListBox (l_myvideos):

      Col 0  job_id     (hidden — used as row key for JOBSTATUS/STREAMURL/DELETE)
      Col 1  title
      Col 2  format     (human label from settings.ini)
      Col 3  status     (QUEUED / DOWNLOADING / CONVERTING / AVAILABLE / ERROR)

    -> OK|VIDEOS|<count>|<job_id>|<title>|<fmt_label>|<status>|<job_id>|...
    -> ERR|NO_VIDEOS
    -> ERR|AUTH_FAILED

    RealBasic usage (ContentSocket DataAvailable after b_refresh click):
      Dim parts() As String = Split(data, "|")
      Dim count As Integer = Val(parts(2))
      l_myvideos.DeleteAllRows
      Dim base As Integer = 3
      Dim i As Integer
      For i = 0 To count - 1
        l_myvideos.AddRow parts(base + i*4)       ' job_id (col 0)
        l_myvideos.Cell(l_myvideos.LastIndex, 1) = parts(base + i*4 + 1)  ' title
        l_myvideos.Cell(l_myvideos.LastIndex, 2) = parts(base + i*4 + 2)  ' format
        l_myvideos.Cell(l_myvideos.LastIndex, 3) = parts(base + i*4 + 3)  ' status
      Next
    """
    username, err = _require_auth(parts, min_len=2)
    if err:
        return err

    jobs = db.get_user_jobs(username)
    if not jobs:
        return _err("NO_VIDEOS")

    cfg  = get_config()
    flat = []
    for j in jobs:
        fmt_key   = j["format_key"]
        fmt_label = cfg.formats[fmt_key].label if fmt_key in cfg.formats else fmt_key
        title     = j["title"].replace("|", "-")
        flat += [
            j["job_id"],
            title,
            fmt_label,
            _wire_status(j["status"]),
        ]

    return _ok("VIDEOS", len(jobs), *flat)


# ---------------------------------------------------------------------------
# STREAMURL
# ---------------------------------------------------------------------------

def handle_streamurl(parts: list[str]) -> str:
    """
    STREAMURL|<token>|<job_id>

    Returns the full FTP URL for an AVAILABLE job. The client passes this
    string directly to OpenURLMovie() — no string building needed.

    -> OK|STREAMURL|<job_id>|<full_ftp_url>
    -> ERR|NOT_AVAILABLE        job exists but is not yet AVAILABLE
    -> ERR|NOT_FOUND
    -> ERR|AUTH_FAILED

    RealBasic usage (b_stream click in MyVideosForm):
      ' Get job_id from hidden column 0 of selected ListBox row
      Dim job_id As String = l_myvideos.Cell(l_myvideos.ListIndex, 0)
      ContentSocket.Write "STREAMURL|" + App.Token + "|" + job_id + Chr(13)+Chr(10)

    RealBasic usage (PlayerForm ContentSocket DataAvailable):
      Dim parts() As String = Split(data, "|")
      If parts(0) = "OK" Then
        m_streamplayer.Movie = OpenURLMovie(parts(3))
      Else
        MsgBox "Cannot stream: " + parts(1)
      End If
    """
    username, err = _require_auth(parts, min_len=3)
    if err:
        return err

    job_id = parts[2].strip()
    job    = db.get_job(username, job_id)

    if job is None:
        return _err("NOT_FOUND")
    if job["status"] != "READY":
        return _err("NOT_AVAILABLE")

    full_url = _ftp_url(job["ftp_path"])
    return _ok("STREAMURL", job_id, full_url)


# ---------------------------------------------------------------------------
# DELETE
# ---------------------------------------------------------------------------

def handle_delete(parts: list[str]) -> str:
    """
    DELETE|<token>|<job_id>

    Removes the converted file from disk and drops the job from the list.
    Only allowed when status is AVAILABLE or ERROR (not mid-job).

    -> OK|DELETED|<job_id>|<freed_kb>
    -> ERR|NOT_FOUND
    -> ERR|JOB_IN_PROGRESS
    -> ERR|AUTH_FAILED

    RealBasic usage (b_delete click in MyVideosForm):
      Dim job_id As String = l_myvideos.Cell(l_myvideos.ListIndex, 0)
      ContentSocket.Write "DELETE|" + App.Token + "|" + job_id + Chr(13)+Chr(10)

    RealBasic usage (ContentSocket DataAvailable):
      Dim parts() As String = Split(data, "|")
      If parts(0) = "OK" Then
        l_myvideos.RemoveRow l_myvideos.ListIndex
      Else
        MsgBox "Cannot delete: " + parts(1)
      End If
    """
    username, err = _require_auth(parts, min_len=3)
    if err:
        return err

    job_id = parts[2].strip()
    job    = db.get_job(username, job_id)

    if job is None:
        return _err("NOT_FOUND")

    if job["status"] in ("QUEUED", "DOWNLOADING", "CONVERTING"):
        return _err("JOB_IN_PROGRESS")

    # Remove physical file if present
    ftp_path = job.get("ftp_path", "")
    if ftp_path:
        abs_path = os.path.join(os.getcwd(), ftp_path)
        if os.path.isfile(abs_path):
            try:
                os.remove(abs_path)
                logger.info("Deleted file: %s", abs_path)
            except OSError as exc:
                logger.warning("Could not delete file %s: %s", abs_path, exc)

    freed_kb = db.delete_job(username, job_id)
    return _ok("DELETED", job_id, max(0, freed_kb))


# ---------------------------------------------------------------------------
# QUOTA
# ---------------------------------------------------------------------------

def handle_quota(parts: list[str]) -> str:
    """
    QUOTA|<token>
    -> OK|QUOTA|<used_mb>|<total_mb>
    -> ERR|AUTH_FAILED
    """
    username, err = _require_auth(parts, min_len=2)
    if err:
        return err

    cfg      = get_config()
    user_def = cfg.users.get(username)
    quota_mb = user_def.quota_mb if user_def else 100
    used_kb  = db.used_disk_kb(username)
    used_mb  = round(used_kb / 1024, 1)

    return _ok("QUOTA", used_mb, quota_mb)


# ---------------------------------------------------------------------------
# PING
# ---------------------------------------------------------------------------

def handle_ping(parts: list[str]) -> str:
    """PING|<token>  ->  OK|PONG"""
    username, err = _require_auth(parts, min_len=2)
    if err:
        return err
    return _ok("PONG")


# ---------------------------------------------------------------------------
# QUIT  (no auth needed — client may call this before or after login)
# ---------------------------------------------------------------------------

def handle_quit(parts: list[str], token: str | None = None) -> str:
    """QUIT  ->  OK|BYE"""
    if token:
        auth.revoke_token(token)
    return _ok("BYE")


# ---------------------------------------------------------------------------
# Dispatcher  (called by the TCP server for each line received)
# ---------------------------------------------------------------------------

COMMAND_MAP = {
    "LOGIN":     handle_login,
    "SEARCH":    handle_search,
    "DETAIL":    handle_detail,
    "REQUEST":   handle_request,
    "JOBSTATUS": handle_jobstatus,
    "MYVIDEOS":  handle_myvideos,
    "STREAMURL": handle_streamurl,
    "DELETE":    handle_delete,
    "QUOTA":     handle_quota,
    "PING":      handle_ping,
}


def dispatch(raw_line: str, current_token: str | None = None) -> tuple[str, str | None]:
    """
    Parse a raw line, route to the correct handler.
    Returns (response_string, updated_token).
    Token is set after LOGIN, cleared after QUIT.
    """
    line = raw_line.strip()
    if not line:
        return _err("EMPTY_COMMAND"), current_token

    parts   = line.split("|")
    command = parts[0].upper()

    if command == "QUIT":
        return handle_quit(parts, token=current_token), None

    handler = COMMAND_MAP.get(command)
    if handler is None:
        return _err("UNKNOWN_COMMAND", command), current_token

    response = handler(parts)

    # Capture token from a successful LOGIN
    new_token = current_token
    if command == "LOGIN" and response.startswith("OK|TOKEN:"):
        try:
            new_token = response.split("|")[1].split(":")[1]
        except IndexError:
            pass

    return response, new_token
