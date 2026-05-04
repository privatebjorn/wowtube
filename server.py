"""
server.py
Main entry point for WowTube Server.

Startup sequence
----------------
1. Load config (settings.ini + users.ini)
2. Configure logging
3. Start FTP server thread
4. Start worker thread pool
5. Start maintenance thread (purge expired sessions)
6. Listen for TCP connections on tcp_port
7. Spawn a client thread for each connection

Each client thread runs a simple read-line / dispatch / write-response loop.
The server is intentionally synchronous per-client — RealBasic sends one
command and waits for the response before sending another.
"""

import logging
import os
import signal
import socket
import threading
import time

from server.config import init_config, get_config, ServerConfig
from server.handlers import dispatch
from server.ftp_server import start_ftp_server, stop_ftp_server
from server.worker import start_pool, stop_pool
from server import auth

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

def _setup_logging(log_dir: str, level_name: str) -> None:
    os.makedirs(log_dir, exist_ok=True)
    level = getattr(logging, level_name.upper(), logging.INFO)

    fmt = logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(threadName)-18s  %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)

    # Console handler
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # File handler
    fh = logging.FileHandler(os.path.join(log_dir, "wowtube.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# TCP server
# ---------------------------------------------------------------------------

def _handle_client(sock: socket.socket, addr: tuple[str, int]) -> None:
    """
    Handle one client connection. Read lines, dispatch commands, send responses.
    """
    logger.info("Client connected: %s:%d", addr[0], addr[1])

    try:
        reader = sock.makefile("r", encoding="utf-8", newline="\r\n")
        writer = sock.makefile("w", encoding="utf-8", newline="\r\n")

        current_token = None

        while True:
            line = reader.readline()
            if not line:
                break  # EOF

            line = line.strip()
            if not line:
                continue

            logger.debug("Received: %r", line)
            response, current_token = dispatch(line, current_token)
            logger.debug("Response: %r", response)
            writer.write(response + "\r\n")
            writer.flush()

    except Exception as e:
        logger.exception("Error handling client %s:%d", addr[0], addr[1])
    finally:
        try:
            sock.close()
        except:
            pass
        logger.info("Client disconnected: %s:%d", addr[0], addr[1])


def _tcp_server(cfg: ServerConfig) -> None:
    """
    Run the TCP command server. Listen on tcp_port, spawn threads for clients.
    """
    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_sock.bind((cfg.host, cfg.tcp_port))
    server_sock.listen(cfg.max_clients)
    logger.info("TCP server listening on %s:%d", cfg.host, cfg.tcp_port)

    try:
        while True:
            client_sock, addr = server_sock.accept()
            thread = threading.Thread(target=_handle_client, args=(client_sock, addr))
            thread.daemon = True
            thread.start()
    except KeyboardInterrupt:
        pass
    finally:
        server_sock.close()


# ---------------------------------------------------------------------------
# Maintenance
# ---------------------------------------------------------------------------

def _maintenance_thread(cfg: ServerConfig) -> None:
    """
    Background thread to purge expired sessions and clean up old jobs.
    """
    while True:
        time.sleep(300)  # 5 minutes
        auth.purge_expired_sessions()
        # TODO: purge old jobs from database?


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    try:
        # ---- Config ------------------------------------------------------------
        cfg = init_config("settings.ini", "users.ini")
        _setup_logging(cfg.log_dir, cfg.log_level)

        logger.info("=" * 60)
        logger.info("WowTube Server starting up")
        logger.info("Formats loaded: %s", ", ".join(cfg.formats.keys()))
        logger.info("Users loaded:   %s", ", ".join(cfg.users.keys()))

        # ---- FTP ---------------------------------------------------------------
        start_ftp_server(root_dir=os.getcwd())

        # ---- Worker ------------------------------------------------------------
        start_pool()

        # ---- Maintenance -------------------------------------------------------
        maint_thread = threading.Thread(target=_maintenance_thread, args=(cfg,))
        maint_thread.daemon = True
        maint_thread.start()

        # ---- TCP ---------------------------------------------------------------
        _tcp_server(cfg)

    except KeyboardInterrupt:
        logger.info("Shutdown requested")
    except Exception as e:
        logger.exception("Fatal error")
    finally:
        stop_pool()
        stop_ftp_server()
        logger.info("WowTube Server stopped. Goodbye.")


if __name__ == "__main__":
    main()