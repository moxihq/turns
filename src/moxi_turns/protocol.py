"""The wire under the library: JSON lines over an endpoint's Unix socket `sock`.

One JSON object per line, both ways. Every request carries the protocol version `v` and
an `op`; a request that wants an answer carries an `id`, which the answer echoes. The ops
and their answers are turns' contract, written in `contracts/socket-protocol.md`; this
module holds the framing, the version, the names and the liveness probe they share.

A socket file is not liveness: a socket file survives its server's crash, and a connect
to it is refused. So a caller that wants to know whether an endpoint lives connects
(`probe`), and only the two errnos that mean "nothing listens here" count as dead.
"""

from __future__ import annotations

import errno
import json
import socket
import threading
from pathlib import Path
from typing import Any

#: The protocol version this package speaks, and every version an endpoint answers.
VERSION = 1
VERSIONS = frozenset({VERSION})

#: The longest line either side reads; a longer one ends the connection.
MAX_LINE = 4 * 1024 * 1024

#: The endpoint directory's files.
SOCK = "sock"
HELLO = "hello.json"
#: A Claude endpoint's second socket, its runner's leg; no caller uses it.
MOD_SOCK = "mod.sock"
#: The temporary name `hello.json` is written under before it replaces the old one.
HELLO_TMP = ".hello.json.tmp"
#: The OpenCode host's `instructions` file, which a sweep of a dead host removes too.
INSTRUCTIONS = "instructions.md"
#: Every file an endpoint directory may hold, for the sweep.
ENDPOINT_FILES = (HELLO, SOCK, MOD_SOCK, HELLO_TMP, INSTRUCTIONS)

#: The ops of the contract.
OPS = ("hello", "facts", "start", "deliver", "steer", "wait", "command", "watch", "hold")
#: A holder's answer to a Stop ask, sent on its `hold` connection.
STOP_ANSWER = "stop-answer"

#: `probe`'s verdicts. Only `STALE` says a path may be cleared.
LIVE, STALE, UNKNOWN = "live", "stale", "unknown"
_STALE_ERRNOS = (errno.ECONNREFUSED, errno.ENOENT)

#: A blocking connect onto a full backlog waits forever; every connect is bounded.
CONNECT_TIMEOUT = 2.0


class ProtocolError(Exception):
    """A frame that is not one JSON object on one line, or a line that is too long."""


def probe(path: Path | str, timeout: float = 0.5) -> tuple[str, OSError | None]:
    """Is anything listening at `path`? `(LIVE | STALE | UNKNOWN, error)`.

    Only ECONNREFUSED and ENOENT are `STALE`. Anything else (a full backlog, a foreign
    owner, a path too long, a timeout) is `UNKNOWN` with its error, and the caller must not
    clear the path.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except OSError as exc:
        if exc.errno in _STALE_ERRNOS:
            return STALE, None
        return UNKNOWN, exc
    else:
        return LIVE, None
    finally:
        sock.close()


def connect(path: Path | str, timeout: float = CONNECT_TIMEOUT) -> "LineConn":
    """A connection to the endpoint socket at `path`. Raises `OSError`."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(path))
    except OSError:
        sock.close()
        raise
    sock.settimeout(None)
    return LineConn(sock)


class LineConn:
    """Newline-delimited JSON both ways, with a write lock."""

    def __init__(self, sock: socket.socket):
        self.sock = sock
        self._wlock = threading.Lock()
        self._buf = b""
        self._closed = False

    def send(self, obj: dict[str, Any]) -> None:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._wlock:
            if self._closed:
                raise ProtocolError("connection closed")
            self.sock.sendall(data)

    def recv(self, timeout: float | None = None) -> dict[str, Any] | None:
        """The next frame, or None at end of stream. Raises `TimeoutError` past `timeout`
        and `ProtocolError` on a frame that does not parse."""
        try:
            self.sock.settimeout(timeout)
        except OSError:
            return None                      # closed under us: the end of the stream
        try:
            while b"\n" not in self._buf:
                try:
                    chunk = self.sock.recv(65536)
                except (TimeoutError, socket.timeout):
                    raise TimeoutError("no frame in time") from None
                except (OSError, ValueError):
                    return None
                if not chunk:
                    return None
                self._buf += chunk
                if len(self._buf) > MAX_LINE and b"\n" not in self._buf:
                    raise ProtocolError(f"a line longer than {MAX_LINE} bytes")
            line, _, self._buf = self._buf.partition(b"\n")
        finally:
            try:
                self.sock.settimeout(None)
            except OSError:
                pass
        if not line.strip():
            return self.recv(timeout)
        try:
            frame = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ProtocolError(f"a frame that is not JSON: {exc}") from None
        if not isinstance(frame, dict):
            raise ProtocolError("a frame that is not a JSON object")
        return frame

    def close(self) -> None:
        with self._wlock:
            self._closed = True
        # shutdown() first: a thread blocked in recv() keeps the file open, so close()
        # alone neither wakes it nor sends the peer its end of stream.
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self) -> "LineConn":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


__all__ = ["VERSION", "VERSIONS", "MAX_LINE", "SOCK", "HELLO", "MOD_SOCK", "HELLO_TMP",
           "INSTRUCTIONS", "ENDPOINT_FILES", "OPS", "STOP_ANSWER", "LIVE", "STALE", "UNKNOWN",
           "CONNECT_TIMEOUT", "ProtocolError", "probe", "connect", "LineConn"]
