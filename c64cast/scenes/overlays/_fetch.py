"""One bounded HTTP GET for the overlays that poll a remote service (`rss`,
`weather`).

`requests`' timeout bounds each socket read, not the response: a peer that
sends a byte inside every timeout, or a body without end, would hold the poll
thread past the overlay's teardown and grow its memory without limit. Here the
whole body has a byte cap and a deadline.
"""

from __future__ import annotations

import time

import requests

MAX_BODY_BYTES = 4 * 1024 * 1024
BODY_DEADLINE_S = 20.0
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
    with requests.get(url, timeout=timeout, headers=headers, stream=True) as r:
        r.raise_for_status()
        body = bytearray()
        # read1 returns whatever one socket read brings, so the deadline is
        # checked between reads; read()/iter_content wait to fill the chunk.
        while chunk := r.raw.read1(_READ_SIZE, decode_content=False):
            body += chunk
            if len(body) > MAX_BODY_BYTES:
                raise BodyLimitExceeded(f"{url}: body over {MAX_BODY_BYTES} bytes")
            if time.monotonic() > deadline:
                raise BodyLimitExceeded(f"{url}: body still arriving after {BODY_DEADLINE_S:g} s")
        return bytes(body)
