"""Decoding a JSON body that came from another machine.

``json.loads`` and ``requests.Response.json()`` signal an undecodable body two
ways. Malformed text raises ``ValueError``, but a body nested deeper than the
decoder's recursion budget raises ``RecursionError``: about 60,000 unclosed
``[`` characters, 60 KB that any device or browser on the LAN can send. That is
a ``RuntimeError``, so a handler that catches ``ValueError`` or
``requests.RequestException`` lets it through, and a reader documented as
never raising raises into its caller.

:func:`decode_json` raises a single type for both cases,
``requests.exceptions.JSONDecodeError``. That type subclasses ``ValueError``
and ``requests.RequestException`` alike, so whichever of the two a caller
already catches is now enough.
"""

from __future__ import annotations

import json
from typing import Protocol

from requests.exceptions import JSONDecodeError


class _JsonResponse(Protocol):
    def json(self) -> object: ...


def decode_json(source: str | bytes | bytearray | _JsonResponse) -> object:
    """The decoded JSON value of ``source``: a document, or a response whose
    ``.json()`` decodes its body.

    Raises ``requests.exceptions.JSONDecodeError`` for any body that does not
    decode, including one nested too deeply to decode at all."""
    try:
        if isinstance(source, (str, bytes, bytearray)):
            return json.loads(source)
        return source.json()
    except JSONDecodeError:
        raise
    except json.JSONDecodeError as e:
        raise JSONDecodeError(e.msg, e.doc, e.pos) from e
    except (ValueError, RecursionError) as e:
        raise JSONDecodeError(f"undecodable JSON body ({type(e).__name__}: {e})", "", 0) from e
