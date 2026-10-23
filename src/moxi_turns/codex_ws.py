"""Synchronous WebSocket JSON-RPC client for one Codex app-server socket."""

from __future__ import annotations

import itertools
import json
import logging
import threading
import time

from websockets.sync.client import unix_connect

from . import codex_protocol as proto

log = logging.getLogger("moxi.turns.codex.ws")

CLIENT_NAME = "moxi-turns"
CLIENT_VERSION = "0.1.0"


AppServerOffline = proto.AppServerOffline
NoReply = proto.NoReply


class AppServerClient:
    """One app-server connection. Requests block; notifications call back."""

    def __init__(self, socket_path, *, on_notification=None, on_close=None,
                 timeout: float = 30.0, client_name: str = CLIENT_NAME,
                 client_version: str = CLIENT_VERSION):
        self.socket_path = str(socket_path)
        self.on_notification = on_notification
        self.on_close = on_close
        self.timeout = timeout
        self.client_name = client_name
        self.client_version = client_version
        self._conn = None
        self._ids = itertools.count(1)
        self._pending: dict = {}
        self._cv = threading.Condition()
        self._reader: threading.Thread | None = None
        self._closed = True

    def connect(self, open_timeout: float = 15.0) -> "AppServerClient":
        try:
            self._conn = unix_connect(
                self.socket_path, uri="ws://localhost/", open_timeout=open_timeout,
                compression=None, max_size=None, legacy=True,
            )
        except Exception as exc:  # websockets exposes several connection failures
            raise AppServerOffline(
                f"Codex app-server unreachable at {self.socket_path}: {exc}"
            ) from exc
        self._closed = False
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name="turns-codex-ws",
        )
        self._reader.start()
        return self

    def initialize(self) -> dict:
        result = self.call(
            "initialize", proto.initialize_params(self.client_name, self.client_version)
        )
        self.notify("initialized")
        return result

    def close(self) -> None:
        self._closed = True
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        with self._cv:
            self._cv.notify_all()

    @property
    def connected(self) -> bool:
        return self._conn is not None and not self._closed

    def _read_loop(self) -> None:
        conn = self._conn
        try:
            for raw in conn:
                if isinstance(raw, bytes):
                    raw = raw.decode()
                # Only "\n" ends a frame. str.splitlines() also splits on U+0085,
                # U+2028, U+2029 and \x1c to \x1e, which Codex leaves raw inside a
                # JSON string, and so cut and dropped such a frame.
                for line in raw.split("\n"):
                    if line.strip():
                        self._dispatch(line)
        except Exception:
            pass
        finally:
            self._closed = True
            with self._cv:
                self._cv.notify_all()
            if self.on_close:
                self.on_close()

    def _dispatch(self, line: str) -> None:
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log.warning("app-server sent a non-JSON frame (%d bytes)", len(line))
            return
        if "id" in message and ("result" in message or "error" in message):
            with self._cv:
                self._pending[message["id"]] = message
                self._cv.notify_all()
            return
        method = message.get("method")
        if method and self.on_notification:
            try:
                self.on_notification(method, message.get("params") or {},
                                     message.get("id"))
            except Exception:
                log.exception("notification handler failed for %s", method)

    def call(self, method: str, params=None, timeout: float | None = None) -> dict:
        if not self.connected:
            raise AppServerOffline(f"Codex app-server not connected: {self.socket_path}")
        rid = next(self._ids)
        try:
            self._conn.send(json.dumps(proto.request(rid, method, params)))
        except Exception as exc:
            self._closed = True
            raise AppServerOffline(f"Codex app-server send failed: {exc}") from exc
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        with self._cv:
            while rid not in self._pending:
                # From here the request is out: a raise is NoReply, since Codex may
                # have acted on it. Before the send it is AppServerOffline.
                if self._closed:
                    raise NoReply("Codex app-server closed the connection")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._pending.pop(rid, None)
                    raise NoReply(f"Codex app-server timed out on {method}")
                self._cv.wait(remaining)
            message = self._pending.pop(rid)
        if "error" in message:
            raise proto.classify_error(message["error"] or {})
        return message.get("result") or {}

    def notify(self, method: str, params=None) -> None:
        if not self.connected:
            return
        try:
            self._conn.send(json.dumps(proto.notification(method, params)))
        except Exception:
            self._closed = True


__all__ = ["AppServerClient", "AppServerOffline", "NoReply", "CLIENT_NAME",
           "CLIENT_VERSION"]
