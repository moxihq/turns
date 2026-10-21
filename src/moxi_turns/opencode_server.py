"""One `opencode serve` per OpenCode host, and the HTTP and event-stream client for it.

    opencode serve --hostname 127.0.0.1 --port 0 --print-logs --log-level INFO

Measured on OpenCode 1.18.34, and each one a rule here:

  * **One server per host.** A session's tools run in the server's own process, so the
    agent's shell inherits the environment the SERVER was started with; a server shared
    between launches would give every session the environment of whoever started it.
  * **Loopback, named, with a password.** The address is given explicitly, and the
    server is started with a per-launch password (`OPENCODE_SERVER_PASSWORD`, HTTP basic
    auth, user `opencode`) that the host holds in memory. Without one, any local process
    can post prompts into the session. The password sits in the server's environment,
    which any process of the same user can read: it keeps out other users and web pages,
    not the same user.
  * **The address comes from the server.** `--port 0` lets the server choose, and it
    says where it listens on its log (`opencode server listening on http://...`).
  * **The log is read, and drained, continuously.** A pipe nobody drains would stall the
    server's logging. Each line goes to `on_line` (the host logs it at debug level).
  * **The child dies with the host**, by the parent-death signal (`procs.die_with_parent`),
    and is signalled only through the handle that spawned it.
"""

from __future__ import annotations

import base64
import contextlib
import http.client
import json
import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from .procs import TERM_GRACE, die_with_parent

log = logging.getLogger("moxi.turns.opencode.server")

#: How long the server may take to say where it listens.
LISTEN_WAIT = 60.0
#: The HTTP user OpenCode's basic auth expects.
USERNAME = "opencode"
#: How long one request may take before it counts as unanswered.
REQUEST_TIMEOUT = 10.0
#: The longest an event stream may stay silent before it counts as cut. The server sends
#: `server.heartbeat` every 10 s (measured on 1.18.34, also while a tool runs), so this
#: is three heartbeats.
SILENCE = 30.0

LISTENING = re.compile(r"listening on (http://[0-9A-Za-z.:\[\]-]+)")


def serve_argv(binary: str) -> list:
    return [binary, "serve", "--hostname", "127.0.0.1", "--port", "0", "--print-logs",
            "--log-level", "INFO"]


class Unreachable(OSError):
    """The request never reached the server: nothing was sent."""


class NoAnswer(OSError):
    """The request was sent and no answer came: the server may have acted on it."""


class Server:
    """One supervised `opencode serve` child. `start`, `alive`, `stop`, and the log."""

    def __init__(self, binary: str, *, env: dict, cwd: str, log_path=None, on_line=None):
        self.binary = binary
        self.env = env
        self.cwd = cwd
        self.log_path = Path(log_path) if log_path else None
        self.on_line = on_line
        self.process: subprocess.Popen | None = None
        self.url: str | None = None
        self.starts = 0
        self._listening = threading.Event()

    def start(self) -> "Server":
        self.url = None
        self._listening.clear()
        self.process = subprocess.Popen(
            serve_argv(self.binary), cwd=self.cwd, env=self.env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
            preexec_fn=die_with_parent(os.getpid()))
        self.starts += 1
        log.info("spawned opencode server pid=%s (start #%d)", self.process.pid,
                 self.starts)
        threading.Thread(target=self._drain, args=(self.process,), daemon=True).start()
        return self

    def _drain(self, proc: subprocess.Popen) -> None:
        sink = self.log_path.open("ab") if self.log_path else None
        try:
            for raw in proc.stdout:
                if sink is not None:
                    sink.write(raw)
                    sink.flush()
                line = raw.decode(errors="replace")
                if self.url is None and proc is self.process:
                    m = LISTENING.search(line)
                    if m:
                        self.url = m.group(1).rstrip("/")
                        self._listening.set()
                if self.on_line is not None:
                    try:
                        self.on_line(line)
                    except Exception:                       # noqa: BLE001
                        log.exception("a server log line could not be read")
        finally:
            if sink is not None:
                sink.close()

    def wait_listening(self, timeout: float = LISTEN_WAIT) -> str | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._listening.wait(0.05):
                return self.url
            if not self.alive:
                return None
        return self.url

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.poll() is None

    @property
    def returncode(self):
        return None if self.process is None else self.process.poll()

    def stop(self, grace: float = TERM_GRACE) -> None:
        proc, self.process = self.process, None
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=grace)


class Http:
    """Requests to one server, with its password. Every answer is (status, body)."""

    def __init__(self, url: str, password: str | None, timeout: float = REQUEST_TIMEOUT):
        parts = urlsplit(url)
        self.url = url
        self.host = parts.hostname or "127.0.0.1"
        self.port = parts.port or 80
        self.password = password
        self.timeout = timeout

    def headers(self) -> dict:
        h = {"content-type": "application/json", "accept": "application/json"}
        if self.password:
            token = base64.b64encode(f"{USERNAME}:{self.password}".encode()).decode()
            h["authorization"] = f"Basic {token}"
        return h

    def connect(self, timeout: float | None = None) -> http.client.HTTPConnection:
        conn = http.client.HTTPConnection(self.host, self.port,
                                          timeout=timeout or self.timeout)
        try:
            conn.connect()
        except OSError as e:
            conn.close()
            raise Unreachable(f"{self.url}: {e}") from e
        return conn

    def request(self, method: str, path: str, body=None,
                timeout: float | None = None) -> tuple:
        """`Unreachable` when nothing was sent, `NoAnswer` when it was sent and no
        answer came."""
        conn = self.connect(timeout)
        try:
            data = None if body is None else json.dumps(body).encode()
            try:
                conn.request(method, path, body=data, headers=self.headers())
                resp = conn.getresponse()
                return resp.status, resp.read().decode(errors="replace")
            except (OSError, http.client.HTTPException) as e:
                raise NoAnswer(f"{method} {path}: {e}") from e
        finally:
            conn.close()

    def json(self, method: str, path: str, body=None, timeout: float | None = None):
        """(status, the decoded body or None)."""
        status, raw = self.request(method, path, body, timeout)
        try:
            return status, (json.loads(raw) if raw else None)
        except ValueError:
            return status, None


class EventStream:
    """`GET /event`, read on its own thread. Every event goes to `on_event`; the end of
    the stream, for any reason, goes once to `on_close`. A stream that sends nothing,
    not even a heartbeat, for `silence` seconds is closed as cut."""

    def __init__(self, http_client: Http, *, on_event, on_close, silence: float = SILENCE):
        self.http = http_client
        self.on_event = on_event
        self.on_close = on_close
        self.silence = silence
        self.connected = threading.Event()
        self.closed = threading.Event()
        self.reason: str | None = None
        self._conn = None
        self._sock = None

    def start(self, timeout: float = REQUEST_TIMEOUT) -> "EventStream":
        """Open the stream; raises `Unreachable`/`NoAnswer`, or `NoAnswer` for any
        answer but 200."""
        conn = self.http.connect(timeout)
        try:
            conn.request("GET", "/event", headers=dict(self.http.headers(),
                                                       accept="text/event-stream"))
            # Set before the response is read: the response keeps this socket, and
            # every read on it then gives up after `silence` seconds with nothing.
            sock = conn.sock
            if sock is not None:
                sock.settimeout(self.silence)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise NoAnswer(f"GET /event: {e}") from e
        if resp.status != 200:
            conn.close()
            raise NoAnswer(f"GET /event answered {resp.status}")
        self._conn = conn
        self._sock = sock
        self.connected.set()
        threading.Thread(target=self._read, args=(resp,), daemon=True).start()
        return self

    def _read(self, resp) -> None:
        reason = "the server closed the event stream"
        try:
            while True:
                try:
                    raw = resp.readline()
                except socket.timeout:
                    reason = f"no event for {self.silence:g}s"
                    break
                if not raw:
                    break
                line = raw.decode(errors="replace").rstrip("\r\n")
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                if isinstance(event, dict):
                    try:
                        self.on_event(event)
                    except Exception:                       # noqa: BLE001
                        log.exception("an event could not be handled")
        except Exception as e:                              # noqa: BLE001
            # A close from another thread can leave http.client half torn down, and
            # what it raises then is not always an OSError. Either way the stream ended.
            reason = self.reason or f"the event stream failed: {e!r}"
        finally:
            self.close(reason)

    def close(self, reason: str = "closed by the host") -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        self.connected.clear()
        self.reason = reason
        conn, self._conn = self._conn, None
        sock, self._sock = self._sock, None
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.shutdown(socket.SHUT_RDWR)   # the reader's next read ends at once
        if conn is not None:
            with contextlib.suppress(OSError):
                conn.close()
        try:
            self.on_close(reason)
        except Exception:                                   # noqa: BLE001
            log.exception("the stream's close could not be handled")


__all__ = ["EventStream", "Http", "LISTEN_WAIT", "NoAnswer", "REQUEST_TIMEOUT", "SILENCE",
           "Server", "Unreachable", "USERNAME", "serve_argv"]
