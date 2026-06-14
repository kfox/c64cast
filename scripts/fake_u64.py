#!/usr/bin/env python3
"""Minimal Ultimate 64 stub for ensemble-verification testing.

Accepts the wire surface c64cast uses (Socket DMA + REST), drops
every DMA write into a JSONL log so the test can assert that a
broadcast actually reached this fake system. Stdlib only.

Usage:
    python scripts/fake_u64.py \\
        --dma-port 8064 --http-port 8080 \\
        --writes-log /tmp/fake-u64-writes.jsonl

The DMA service uses opcodes from c64cast/socket_dma.py:
  0xFF0E IDENTIFY   →  responds 1-byte length + ASCII product string
  0xFF1F AUTHENTICATE → responds 1 byte (always 0x01 = accepted)
  0xFF06 DMAWRITE  →  no response; recorded to the writes log
  0xFF04 RESET     →  no response
  0xFF03 KEYB      →  no response

The HTTP shim covers:
  GET  /                          → 200  (used by Ultimate64API.probe)
  GET  /v1/machine:readmem        → zeros (keyboard poller reads $028D)
  PUT  /v1/machine:reset          → 200
  POST /v1/runners:run_prg        → 200  (BASIC clear loop)
  POST /v1/runners:sidplay        → 200
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import signal
import socket
import socketserver
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("fake_u64")


# Mirror of c64cast/socket_dma.py opcodes. Duplicated here so the
# stub stays standalone (no c64cast import — useful when the stub
# is run on a different machine for genuine multi-host testing).
CMD_KEYB = 0xFF03
CMD_RESET = 0xFF04
CMD_DMAWRITE = 0xFF06
CMD_IDENTIFY = 0xFF0E
CMD_AUTHENTICATE = 0xFF1F


class WriteLog:
    """Thread-safe append-only JSONL log of DMAWRITE events. One line per
    write so the test can iterate with `jq` or plain readlines."""

    def __init__(self, path: str | None):
        self._path = path
        self._lock = threading.Lock()
        # Process-lifetime handle (closed in self.close() at shutdown);
        # a with-block doesn't fit the open-in-__init__/close-in-close
        # ownership pattern.
        self._fh = (
            open(path, "w", buffering=1)  # noqa: SIM115
            if path
            else None
        )
        self._count = 0

    def record(self, addr: int, data: bytes) -> None:
        with self._lock:
            self._count += 1
            if self._fh is None:
                return
            self._fh.write(
                json.dumps(
                    {
                        "t": time.time(),
                        "addr": f"{addr:04X}",
                        "len": len(data),
                    }
                )
                + "\n"
            )

    @property
    def count(self) -> int:
        return self._count

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None


class DMAHandler(socketserver.BaseRequestHandler):
    """One TCP connection per c64cast Ultimate64API instance. Loops
    reading commands until the client disconnects."""

    def handle(self) -> None:
        sock: socket.socket = self.request
        sock.settimeout(None)
        peer = sock.getpeername()
        log.info("DMA: client connected from %s:%d", *peer)
        try:
            while True:
                header = self._recv_exact(sock, 4)
                if header is None:
                    log.info("DMA: client %s:%d disconnected", *peer)
                    return
                opcode, length = struct.unpack("<HH", header)
                payload = self._recv_exact(sock, length) if length else b""
                if payload is None:
                    log.warning("DMA: short payload for opcode %04X", opcode)
                    return
                self._dispatch(sock, opcode, payload)
        except OSError as e:
            log.info("DMA: socket error from %s:%d: %s", peer[0], peer[1], e)

    def _recv_exact(self, sock: socket.socket, n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = sock.recv(n - len(buf))
            except OSError:
                return None
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _dispatch(self, sock: socket.socket, opcode: int, payload: bytes) -> None:
        server: FakeU64DMAServer = self.server  # type: ignore[assignment]
        if opcode == CMD_DMAWRITE:
            if len(payload) < 2:
                log.warning("DMA: short DMAWRITE payload (%d bytes)", len(payload))
                return
            (addr,) = struct.unpack("<H", payload[:2])
            data = payload[2:]
            server.writes.record(addr, data)
        elif opcode == CMD_IDENTIFY:
            product = server.product
            sock.sendall(bytes([len(product)]) + product)
        elif opcode == CMD_AUTHENTICATE:
            sock.sendall(b"\x01")
        elif opcode == CMD_RESET:
            log.info("DMA: client requested RESET (ignored)")
        elif opcode == CMD_KEYB:
            log.debug("DMA: client requested KEYB %r (ignored)", payload)
        else:
            log.warning("DMA: unknown opcode %04X (len=%d) — dropping", opcode, len(payload))


class FakeU64DMAServer(socketserver.ThreadingTCPServer):
    """Threading TCP server so multiple Ultimate64API instances (e.g.
    one render + one audio socket per system, or multiple fakes
    multiplexed on different ports) can all run against this stub
    process — though c64cast itself only opens one socket per
    Ultimate64API today."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host: str, port: int, writes: WriteLog, product: bytes):
        super().__init__((host, port), DMAHandler)
        self.writes = writes
        self.product = product


class HTTPHandler(BaseHTTPRequestHandler):
    """REST shim covering the endpoints Ultimate64API actually calls."""

    server_version = "FakeU64/0.1"

    def log_message(self, fmt: str, *args) -> None:
        # Quiet the default per-request stderr noise; we have our own
        # logger that's easier to grep.
        log.debug("HTTP: " + fmt, *args)

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/":
            # Probe.
            self._ok(b"FakeU64\n", "text/plain")
            return
        if parts.path == "/v1/machine:readmem":
            qs = parse_qs(parts.query)
            length = int(qs.get("length", ["1"])[0])
            # Return zeros. The keyboard poller reads $028D at 10 Hz
            # and treats zero as "no modifier keys pressed", which is
            # what we want — no spurious pause/resume from the fake.
            self._ok(bytes(length), "application/octet-stream")
            return
        self.send_error(404)

    def do_PUT(self) -> None:
        parts = urlsplit(self.path)
        if parts.path == "/v1/machine:reset":
            log.info("HTTP: PUT reset (ignored)")
            self._ok(b"")
            return
        self.send_error(404)

    def do_POST(self) -> None:
        # Drain the request body even if we ignore it — clients hang
        # waiting for the response otherwise.
        length = int(self.headers.get("Content-Length", "0"))
        if length:
            self.rfile.read(length)
        parts = urlsplit(self.path)
        if parts.path == "/v1/runners:run_prg":
            log.info("HTTP: POST run_prg (BASIC clear loop, ignored)")
            self._ok(b"")
            return
        if parts.path == "/v1/runners:sidplay":
            log.info("HTTP: POST sidplay (ignored)")
            self._ok(b"")
            return
        self.send_error(404)

    def _ok(self, body: bytes, content_type: str = "application/octet-stream") -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)


class FakeU64HTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Ultimate 64 stub for c64cast ensemble verification testing."
    )
    p.add_argument("--host", default="127.0.0.1", help="Interface to bind (default localhost only)")
    p.add_argument("--dma-port", type=int, default=8064, help="TCP port for the Socket DMA service")
    p.add_argument("--http-port", type=int, default=8080, help="TCP port for the REST API")
    p.add_argument(
        "--writes-log", default=None, metavar="PATH", help="JSONL file to append every DMA write to"
    )
    p.add_argument("--product", default="FakeU64-Ensemble-Stub", help="String returned on IDENTIFY")
    p.add_argument("-v", "--verbose", action="store_true", help="Log every HTTP request at DEBUG")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    writes = WriteLog(args.writes_log)
    dma = FakeU64DMAServer(args.host, args.dma_port, writes, args.product.encode("utf-8"))
    http = FakeU64HTTPServer((args.host, args.http_port), HTTPHandler)

    stop = threading.Event()

    def _shutdown(_signum, _frame):
        log.info("shutting down (writes recorded: %d)", writes.count)
        stop.set()

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    dma_thread = threading.Thread(target=dma.serve_forever, name="fake-u64-dma", daemon=True)
    http_thread = threading.Thread(target=http.serve_forever, name="fake-u64-http", daemon=True)
    dma_thread.start()
    http_thread.start()
    log.info(
        "DMA listening on %s:%d, HTTP on %s:%d (writes log: %s)",
        args.host,
        args.dma_port,
        args.host,
        args.http_port,
        args.writes_log or "(none)",
    )

    try:
        stop.wait()
    finally:
        with contextlib.suppress(Exception):
            dma.shutdown()
            dma.server_close()
        with contextlib.suppress(Exception):
            http.shutdown()
            http.server_close()
        writes.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
