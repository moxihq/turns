"""Codex hook flags, their per-launch trust, and the host's ping server.

Hook commands carry their socket path on the command line because Codex runs them with
the session's saved environment. SessionStart and UserPromptSubmit return listener text
through ``moxi_turns.hooks``. Stop uses a two-phase block: the ping flushes the block to
stdout and acknowledges that write. No hook closes a turn: Stop and Interrupt are Codex
asking or telling, and the turn ends on Codex's own word (see ``codex``). A
Stop answered unblocked marks the turn ending, so it takes no more steers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shlex
import socket
import threading
from dataclasses import dataclass
from pathlib import Path

from . import protocol
from .results import StopFacts

log = logging.getLogger("moxi.turns.codex.hooks")

KEY_SOURCE = "/<session-flags>/config.toml"
TRUSTED = "trusted"
DEFAULT_TIMEOUT = 600
INTERRUPT_TIMEOUT = 3
PING_TIMEOUT = 3.0
PRINTED = "printed"


@dataclass(frozen=True)
class HookEvent:
    event: str
    label: str
    timeout: int
    source: str | None = None


EVENTS = (
    HookEvent("SessionStart", "session_start", DEFAULT_TIMEOUT),
    HookEvent("UserPromptSubmit", "user_prompt_submit", DEFAULT_TIMEOUT),
    HookEvent("Stop", "stop", DEFAULT_TIMEOUT),
    HookEvent("Interrupt", "interrupt", INTERRUPT_TIMEOUT),
    HookEvent("PostCompact", "post_compact", DEFAULT_TIMEOUT, "compact"),
)
EVENT_NAMES = tuple(event.event for event in EVENTS)


class HooksUntrusted(RuntimeError):
    """The app-server did not report every launch hook as trusted."""


def hooks_socket(directory) -> Path:
    return Path(directory) / "codex-hooks.sock"


def command(ping: list[str], socket_path, event: HookEvent,
            wait_seconds: float = PING_TIMEOUT) -> str:
    argv = [*ping, "--socket", str(socket_path), "--wait-seconds",
            f"{wait_seconds:g}", event.event]
    if event.source:
        argv += ["--source", event.source]
    return shlex.join(argv)


def handler(cmd: str, timeout: int) -> dict:
    return {"type": "command", "command": cmd, "timeout": timeout, "async": False}


def hook_key(label: str) -> str:
    return f"{KEY_SOURCE}:{label}:0:0"


def trusted_hash(label: str, one: dict) -> str:
    blob = json.dumps({"event_name": label, "hooks": [one]}, sort_keys=True,
                      separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()


def _toml(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ",".join(_toml(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{_toml_key(key)}={_toml(item)}" for key, item in value.items()
        ) + "}"
    raise TypeError(f"no TOML form for {value!r}")


def _toml_key(key: str) -> str:
    bare = key.replace("_", "").replace("-", "").isalnum()
    return key if bare else json.dumps(key)


def session_flags(ping: list[str], socket_path, *, hold_wait_s: float = PING_TIMEOUT - 1,
                  listen_wait_s: float = PING_TIMEOUT - 1) \
        -> tuple[list[str], dict[str, str]]:
    argv: list[str] = []
    keys: dict[str, str] = {}
    for event in EVENTS:
        if event.event == "Stop":
            wait_seconds = hold_wait_s + 1
        elif event.event == "Interrupt":
            wait_seconds = PING_TIMEOUT
        else:
            wait_seconds = listen_wait_s + 1
        one = handler(command(ping, socket_path, event, wait_seconds), event.timeout)
        argv += ["-c", f"hooks.{event.event}={_toml([{'hooks': [one]}])}"]
        keys[hook_key(event.label)] = trusted_hash(event.label, one)
    state = {key: {"trusted_hash": value} for key, value in keys.items()}
    argv += ["-c", f"hooks.state={_toml(state)}"]
    return argv, keys


def trust_problem(result, keys: dict[str, str]) -> str | None:
    listed: dict = {}
    for entry in (result or {}).get("data") or []:
        for hook in entry.get("hooks") or []:
            if hook.get("key") in keys:
                listed[hook["key"]] = hook
    for key in keys:
        hook = listed.get(key)
        if hook is None:
            return f"Codex does not list turns' hook {key}"
        if not hook.get("enabled"):
            return f"Codex lists turns' hook {key} as disabled"
        if hook.get("trustStatus") != TRUSTED:
            return (f"Codex lists turns' hook {key} as {hook.get('trustStatus')!r}, "
                    f"not trusted")
    return None


def hook_fields(data: dict, *, source: str | None = None) -> dict:
    fields: dict = {}
    for key in ("session_id", "turn_id"):
        value = data.get(key)
        if isinstance(value, str) and value:
            fields[key] = value
    value = data.get("source")
    if isinstance(value, str) and value:
        fields["source"] = value
    elif source:
        fields["source"] = source
    active = data.get("stop_hook_active")
    if isinstance(active, bool):
        fields["stop_hook_active"] = active
    tasks = data.get("background_tasks")
    if isinstance(tasks, list):
        fields["task_types"] = tuple(
            item.get("type") if isinstance(item, dict)
            and isinstance(item.get("type"), str) else "unknown" for item in tasks
        )
    crons = data.get("session_crons")
    if isinstance(crons, list):
        fields["crons"] = sum(
            1 for cron in crons
            if not (isinstance(cron, dict) and cron.get("recurring") is True)
        )
    return fields


class HookServer:
    """One Unix socket receiving the five hook events for a Codex host."""

    def __init__(self, harness, socket_path):
        self.harness = harness
        self.socket_path = Path(socket_path)
        self.bound = False
        self._server: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._event_lock = threading.RLock()

    def start(self) -> "HookServer":
        if self.socket_path.exists():
            verdict, error = protocol.probe(self.socket_path)
            if verdict == protocol.LIVE:
                raise RuntimeError(f"a hook server already listens on {self.socket_path}")
            if verdict != protocol.STALE:
                raise RuntimeError(f"could not prove {self.socket_path} stale: {error}")
            self.socket_path.unlink()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.socket_path.parent, 0o700)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.socket_path))
        self.bound = True
        os.chmod(self.socket_path, 0o600)
        server.listen(8)
        server.settimeout(0.2)
        self._server = server
        self._thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="turns-codex-hooks",
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)
        if self.bound:
            self.socket_path.unlink(missing_ok=True)
            self.bound = False

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                sock, _ = self._server.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(sock,), daemon=True).start()

    def _serve(self, sock: socket.socket) -> None:
        conn = protocol.LineConn(sock)
        try:
            frame = conn.recv(PING_TIMEOUT)
            if not isinstance(frame, dict):
                return
            with self._event_lock:
                reply = self._dispatch(frame, conn)
            if reply is not None:
                conn.send(reply)
        except (OSError, TimeoutError, protocol.ProtocolError):
            pass
        finally:
            conn.close()

    def _dispatch(self, frame: dict, conn: protocol.LineConn) -> dict | None:
        event = frame.get("event")
        if event not in EVENT_NAMES:
            return {"ok": False, "error": f"unknown Codex hook event {event!r}"}
        session_id = frame.get("session_id")
        if session_id != self.harness.session_id:
            self.harness.foreign_hook(event, session_id)
            return {"ok": True}
        endpoint = self.harness.endpoint
        if event in ("SessionStart", "PostCompact"):
            source = frame.get("source")
            source = source if isinstance(source, str) else None
            # The accepted load raised this start's or resume's Ready.
            loaded = event == "SessionStart" and source in ("startup", "resume")
            context = endpoint.ready(source, self.harness.session_id, announce=not loaded)
            return {"ok": True, "context": context}
        if event == "UserPromptSubmit":
            return {"ok": True, "context": endpoint.prompt()}
        # A hook answers what it is asked and closes no turn. Stop is Codex
        # asking whether the turn may end: the turn runs on until Codex's own
        # turn/completed, which carries the outcome (`interrupted` for Esc).
        if event == "Interrupt":
            return {"ok": True}
        facts = self.harness.stop_facts(frame)
        reason = endpoint.on_stop(facts)
        if reason:
            conn.send({"ok": True, "block": reason})
            # The ping still says when it has printed the block. Whether it did or not,
            # the turn's end is Codex's to report.
            try:
                conn.recv(PING_TIMEOUT)
            except (TimeoutError, protocol.ProtocolError):
                pass
            return None
        # Unblocked: once Codex reads this answer it may complete the turn at once, and
        # a steer it takes then can be written into the turn and never answered. The
        # turn is marked ending before the answer goes out.
        self.harness.stop_answered(facts.turn_id)
        return {"ok": True}


class _Ping:
    def __init__(self, path: Path, event: str, source: str | None,
                 wait_seconds: float):
        self.path = path
        self.event = event
        self.source = source
        self.wait_seconds = wait_seconds
        self.conn: protocol.LineConn | None = None
        self.block = False

    def body(self, data: dict) -> str | None:
        self.conn = protocol.connect(self.path, PING_TIMEOUT)
        self.conn.send({"event": self.event, **hook_fields(data, source=self.source)})
        reply = self.conn.recv(self.wait_seconds)
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise RuntimeError((reply or {}).get("error", "the hook host did not answer"))
        reason = reply.get("block")
        if isinstance(reason, str) and reason:
            self.block = True
            return reason
        context = reply.get("context")
        return context if isinstance(context, str) and context else None

    def emitted(self, text) -> None:
        try:
            if self.block and self.conn is not None:
                self.conn.send({"op": PRINTED})
        finally:
            self.close()

    def error(self, exc, *, where) -> None:
        log.warning("Codex hook %s failed at %s: %s", self.event, where, exc)
        self.close()

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None


def _block(event: str, reason: str) -> str:
    return json.dumps({"decision": "block", "reason": reason}, separators=(",", ":"))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="moxi-turns-ping")
    parser.add_argument("event", choices=EVENT_NAMES)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--source")
    parser.add_argument("--wait-seconds", type=float, default=PING_TIMEOUT,
                        help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.wait_seconds <= 0:
        parser.error("--wait-seconds must be greater than zero")

    from .hooks.run import run

    ping = _Ping(Path(args.socket), args.event, args.source, args.wait_seconds)
    render = _block if args.event == "Stop" else None
    kwargs = {"render": render} if render is not None else {}
    return run(args.event, ping.body, seconds=args.wait_seconds, on_error=ping.error,
               on_emitted=ping.emitted, **kwargs)


__all__ = [
    "EVENTS", "EVENT_NAMES", "HookEvent", "HookServer", "HooksUntrusted", "KEY_SOURCE",
    "TRUSTED", "command", "handler", "hook_fields", "hook_key", "hooks_socket", "main",
    "session_flags", "trust_problem", "trusted_hash",
]
