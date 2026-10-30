"""One bounded HTTP GET for the overlays that poll a remote service (`rss`,
`weather`).

`requests`' timeout bounds each socket read, not the response: a peer that
sends a byte inside every timeout — during the TLS handshake, the status line,
the headers or the body — or a body without end, would hold the poll thread
past the overlay's teardown and grow its memory without limit. Here the body
has a byte cap and the whole exchange a deadline, enforced by shutting down
the call's sockets when it passes.
"""

from __future__ import annotations

import contextlib
import socket
import threading
from functools import cache
from urllib.parse import urljoin

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection
from urllib3.connectionpool import HTTPConnectionPool

MAX_BODY_BYTES = 4 * 1024 * 1024
RESPONSE_DEADLINE_S = 20.0
MAX_REDIRECTS = 5
_READ_SIZE = 64 * 1024


class BodyLimitExceeded(Exception):
    """The response ran past `MAX_BODY_BYTES` or `RESPONSE_DEADLINE_S`."""


def get_body(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> bytes:
    """The body of a successful GET of `url`, read under the byte cap and the
    deadline. Raises `requests.RequestException` for a transport or HTTP
    error and `BodyLimitExceeded` for a response past either limit."""
    # The body is asked for uncompressed and read undecoded, so the byte cap
    # bounds what is held: a compressed stream decodes to many times its size.
    headers = {**(headers or {}), "Accept-Encoding": "identity"}
    watchdog = _Watchdog(RESPONSE_DEADLINE_S)
    _calls.watchdog = watchdog
    try:
        with requests.Session() as session:
            adapter = _WatchedAdapter()
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            body = _follow(session, url, timeout, headers)
    except (requests.RequestException, urllib3.exceptions.HTTPError) as e:
        if watchdog.fired:
            raise _past_deadline(url) from e
        raise
    finally:
        _calls.watchdog = None
        watchdog.close()
    if watchdog.fired:
        raise _past_deadline(url)
    return body


def _past_deadline(url: str) -> BodyLimitExceeded:
    return BodyLimitExceeded(f"{url}: no complete response within {RESPONSE_DEADLINE_S:g} s")


def _follow(session: requests.Session, url: str, timeout: float, headers: dict[str, str]) -> bytes:
    for _ in range(MAX_REDIRECTS + 1):
        try:
            r = session.get(
                url,
                timeout=timeout,
                headers=headers,
                stream=True,
                allow_redirects=False,
                hooks={"response": _refuse_redirect},
            )
        except _Redirect as redirect:
            url = urljoin(url, redirect.location)
            continue
        with r:
            r.raise_for_status()
            # r.raw raises urllib3's own errors; requests translates them only
            # inside iter_content, which this read cannot use.
            try:
                return _read_capped(r.raw, url)
            except urllib3.exceptions.ReadTimeoutError as e:
                raise requests.ReadTimeout(e) from e
            except urllib3.exceptions.HTTPError as e:
                raise requests.ConnectionError(e) from e
    raise requests.TooManyRedirects(f"{url}: more than {MAX_REDIRECTS} redirects")


class _Redirect(Exception):
    def __init__(self, location: str):
        super().__init__(location)
        self.location = location


def _refuse_redirect(r: requests.Response, **_kwargs) -> None:
    # requests reads a redirect's whole body, past both limits, even with
    # allow_redirects=False (Session.send builds Response.next from it); the
    # response hook is the last point before that read.
    # get_redirect_target re-decodes a raw UTF-8 Location that http.client
    # read as Latin-1.
    if location := _REDIRECTS.get_redirect_target(r):
        r.close()
        raise _Redirect(location)


_REDIRECTS = requests.sessions.SessionRedirectMixin()


def _read_capped(raw: urllib3.BaseHTTPResponse, url: str) -> bytes:
    body = bytearray()
    while chunk := raw.read1(_READ_SIZE, decode_content=False):
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise BodyLimitExceeded(f"{url}: body over {MAX_BODY_BYTES} bytes")
    return bytes(body)


class _Watchdog:
    def __init__(self, deadline_s: float):
        self.fired = False
        self._lock = threading.Lock()
        self._socks: list[socket.socket] = []
        self._timer = threading.Timer(deadline_s, self._fire)
        self._timer.daemon = True
        self._timer.start()

    def watch(self, sock: socket.socket) -> None:
        # A duplicate, because an HTTPS connection detaches the socket it
        # wraps: shutdown on either descriptor ends the connection.
        dup = sock.dup()
        with self._lock:
            self._socks.append(dup)
            if self.fired:
                _shutdown(dup)

    def close(self) -> None:
        self._timer.cancel()
        self._timer.join()
        with self._lock:
            for sock in self._socks:
                sock.close()
            self._socks.clear()

    def _fire(self) -> None:
        with self._lock:
            self.fired = True
            for sock in self._socks:
                _shutdown(sock)


def _shutdown(sock: socket.socket) -> None:
    with contextlib.suppress(OSError):
        sock.shutdown(socket.SHUT_RDWR)


_calls = threading.local()


class _WatchedConnection(HTTPConnection):
    def _new_conn(self) -> socket.socket:
        sock = super()._new_conn()
        _calls.watchdog.watch(sock)
        return sock


@cache
def _watched_pool(pool_cls: type[HTTPConnectionPool]) -> type[HTTPConnectionPool]:
    conn_cls = type(
        pool_cls.ConnectionCls.__name__, (_WatchedConnection, pool_cls.ConnectionCls), {}
    )
    return type(pool_cls.__name__, (pool_cls,), {"ConnectionCls": conn_cls})


def _watch_pools(manager: urllib3.PoolManager) -> None:
    manager.pool_classes_by_scheme = {
        scheme: _watched_pool(cls) for scheme, cls in manager.pool_classes_by_scheme.items()
    }


class _WatchedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs) -> None:
        super().init_poolmanager(*args, **kwargs)
        _watch_pools(self.poolmanager)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        manager = super().proxy_manager_for(proxy, **proxy_kwargs)
        _watch_pools(manager)
        return manager
