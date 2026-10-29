"""One bounded HTTP GET for the overlays that poll a remote service (`rss`,
`weather`).

`requests`' timeout bounds each socket read, not the response: a peer that
sends a byte inside every timeout, or a body without end, would hold the poll
thread past the overlay's teardown and grow its memory without limit. Here the
whole body has a byte cap and a deadline.
"""

from __future__ import annotations

import time
from urllib.parse import urljoin

import requests
import urllib3

MAX_BODY_BYTES = 4 * 1024 * 1024
BODY_DEADLINE_S = 20.0
MAX_REDIRECTS = 5
_READ_SIZE = 64 * 1024


class BodyLimitExceeded(Exception):
    """The response ran past `MAX_BODY_BYTES` or `BODY_DEADLINE_S`."""


def get_body(url: str, *, timeout: float, headers: dict[str, str] | None = None) -> bytes:
    """The body of a successful GET of `url`, read under the byte cap and the
    deadline. Raises `requests.RequestException` for a transport or HTTP
    error and `BodyLimitExceeded` for a response past either limit."""
    deadline = time.monotonic() + BODY_DEADLINE_S
    # The body is asked for uncompressed and read undecoded: urllib3's decoder
    # loops inside one read1() for as long as a compressed stream yields no
    # output, out of reach of the deadline.
    headers = {**(headers or {}), "Accept-Encoding": "identity"}
    for _ in range(MAX_REDIRECTS + 1):
        try:
            r = requests.get(
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
                return _read_capped(r.raw, url, deadline)
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


def _read_capped(raw: urllib3.BaseHTTPResponse, url: str, deadline: float) -> bytes:
    body = bytearray()
    # read1 returns whatever one socket read brings, so the deadline is
    # checked between reads; read()/iter_content wait to fill the chunk.
    while chunk := raw.read1(_READ_SIZE, decode_content=False):
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            raise BodyLimitExceeded(f"{url}: body over {MAX_BODY_BYTES} bytes")
        if time.monotonic() > deadline:
            raise BodyLimitExceeded(f"{url}: body still arriving after {BODY_DEADLINE_S:g} s")
    return bytes(body)
