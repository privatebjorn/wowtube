"""
ftp_server.py
Serves the thumbs/ and videos/ directories over FTP using pyftpdlib.

A single shared read-only account (configured in settings.ini) is used.
Write access is intentionally disabled — the Python server writes files,
the RealBasic client only reads them.

The FTP root is the project working directory so that paths like
  ftp://share:pass@host/thumbs/<id>.jpg
  ftp://share:pass@host/videos/<user>/<id>_<fmt>.mov
work directly from protocol responses.
"""

import logging
import threading
import os

from pyftpdlib.handlers  import FTPHandler
from pyftpdlib.servers   import FTPServer
from pyftpdlib.authorizers import DummyAuthorizer

from .config import get_config

logger = logging.getLogger(__name__)

_ftp_thread: threading.Thread | None = None
_ftp_server: FTPServer | None        = None


def start_ftp_server(root_dir: str | None = None) -> None:
    """
    Start the FTP server in a daemon thread.
    root_dir defaults to the current working directory.
    """
    global _ftp_thread, _ftp_server

    cfg = get_config()

    if root_dir is None:
        root_dir = os.getcwd()

    # ---- Authorizer: one shared read-only account --------------------------
    authorizer = DummyAuthorizer()
    authorizer.add_user(
        cfg.ftp_user,
        cfg.ftp_password,
        homedir    = root_dir,
        perm       = "elr",    # e=change dir, l=list, r=retrieve (read-only)
    )

    # ---- Handler -----------------------------------------------------------
    handler               = FTPHandler
    handler.authorizer    = authorizer
    handler.passive_ports = range(40200, 40300)   # passive mode port range
    handler.banner        = "WowTube FTP ready."

    # Suppress pyftpdlib's verbose per-transfer logging
    logging.getLogger("pyftpdlib").setLevel(logging.WARNING)

    # ---- Server ------------------------------------------------------------
    _ftp_server = FTPServer((cfg.host, cfg.ftp_port), handler)
    _ftp_server.max_cons        = 64
    _ftp_server.max_cons_per_ip = 8

    _ftp_thread = threading.Thread(
        target      = _ftp_server.serve_forever,
        name        = "ftp-server",
        daemon      = True,
    )
    _ftp_thread.start()
    logger.info("FTP server listening on %s:%d  root=%s  user=%s",
                cfg.host, cfg.ftp_port, root_dir, cfg.ftp_user)


def stop_ftp_server() -> None:
    global _ftp_server
    if _ftp_server:
        _ftp_server.close_all()
        logger.info("FTP server stopped")