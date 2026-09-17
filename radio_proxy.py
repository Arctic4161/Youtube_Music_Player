"""Per-player loopback CONNECT tunnel to the configured SOCKS proxy.

The native players keep the original HTTPS URL and perform TLS themselves.
Only encrypted bytes pass through this bounded, memory-only tunnel.
"""

from __future__ import annotations

import contextlib
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from yt_dlp.networking._helper import make_socks_proxy_opts
from yt_dlp.socks import sockssocket


class _TunnelServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False


class RadioProxy:
    """Own a loopback proxy for one HTTPS media host and its CDN redirects."""

    def __init__(self, media_url: str, proxy_url: str) -> None:
        media = urlsplit(media_url)
        if media.scheme != "https" or not media.hostname or media.port not in (None, 443):
            raise ValueError("Radio requires an HTTPS audio stream.")
        self._host = media.hostname.lower()
        self._options = make_socks_proxy_opts(proxy_url)
        if not self._options["addr"]:
            raise ValueError("Radio requires a valid SOCKS proxy host.")
        self._closed = threading.Event()
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            # No request/URL logging: media URLs may contain signed tokens.
            def log_message(self, *_args):
                return

            def setup(self):
                self.request.settimeout(20)
                super().setup()

            def do_CONNECT(self):
                try:
                    target = urlsplit("//" + self.path)
                    host, port = target.hostname, target.port
                    if (
                        port != 443 or not host or target.path or target.query
                        or target.username or target.password
                        or not owner._allows(host)
                    ):
                        self.send_error(403)
                        return
                    owner._track(self.connection)
                    upstream = sockssocket()
                    try:
                        owner._track(upstream)
                        upstream.settimeout(20)
                        upstream.setproxy(**owner._options)
                        upstream.connect((host, port))
                        self.send_response(200, "Connection established")
                        self.end_headers()
                        self.wfile.flush()
                        owner._relay(self.connection, upstream)
                    finally:
                        owner._forget(upstream)
                        upstream.close()
                except (OSError, EOFError, ValueError):
                    # Closing the connection propagates failure to the player;
                    # there is intentionally no direct-network fallback.
                    return
                finally:
                    owner._forget(self.connection)
                    self.close_connection = True

        self._server = _TunnelServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_port
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.1),
            name="RadioProxy", daemon=True,
        )
        try:
            self._thread.start()
        except Exception:
            self._server.server_close()
            raise

    def _allows(self, host: str) -> bool:
        host = host.lower()
        return host == self._host or (
            self._host.endswith(".googlevideo.com") and host.endswith(".googlevideo.com")
        )

    def _track(self, connection: socket.socket) -> None:
        with self._lock:
            if self._closed.is_set():
                raise OSError("Radio proxy is closed")
            self._sockets.add(connection)

    def _forget(self, connection: socket.socket) -> None:
        with self._lock:
            self._sockets.discard(connection)

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        peers = {client: upstream, upstream: client}
        while not self._closed.is_set():
            ready, _, _ = select.select(list(peers), [], [], 0.25)
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                peers[source].sendall(data)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        with self._lock:
            sockets = tuple(self._sockets)
        for connection in sockets:
            with contextlib.suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=1)
