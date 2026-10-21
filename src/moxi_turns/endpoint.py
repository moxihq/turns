"""The endpoint core: one per session turns can reach, one socket, one turn ledger.

An endpoint is a small turns process (the Claude Code endpoint the runner spawns, or the
Codex or OpenCode host). This module is its harness-free core; an adapter
(`harness.Harness`) supplies the harness half. The core:

- makes the endpoint's directory `RUNTIME/turns/<harness>-<nonce>/` (0700), binds `sock`
  in it (0600) and writes `hello.json` (§5.3), keyed by the launch's nonce, not the
  session id, so a `/clear` keeps the endpoint and `hello.json` follows the id;
- answers the socket protocol (`contracts/socket-protocol.md`): `hello`, `facts`,
  `start`, `deliver`, `steer`, `wait`, `command`, `watch`, `hold`;
- keeps the turn ledger the adapter feeds (§5.4), ties deliveries to turns, holds a
  delivery made while a turn runs until that turn ends, and answers `wait` from the
  ledger alone;
- checks a session command's consent and its idle snapshot (RULE T4);
- asks the session's holder at each Stop, bounded by `hold.wait_ms` ([R29]);
- asks the registered listeners at `ready` and `prompt` and passes back what they
  answer, and tells them of `turn` and `gone` (§5.11).

turns decides nothing about content or timing: it delivers what it is handed, when it is
handed it, under the preconditions the caller names.

`open()` opens the endpoint; `stop()` (or the adapter's `gone()`) ends it. The caller's
ops are methods of the same names (`start`, `deliver`, `steer`, `wait`, `command`,
`facts`), so a test can drive the core without the socket.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import os
import queue
import secrets
import socket
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from moxi.paths import Layout

from . import config as turns_config
from . import listeners as turns_listeners
from . import protocol
from .harness import CommandRequest, Delivery, Harness, Live
from .ledger import Reading, Turn, TurnLedger
from .results import (COMMANDS, OUTCOMES, Ended, Facts, Failed, Gone, Lost, Passed, Ran,
                      Ready, Refused, SessionEvent, Started, StopFacts, TurnEnded,
                      TurnStarted)

log = logging.getLogger("moxi.turns.endpoint")

#: One session command accepted per this many seconds ([R31], mod-control as built).
COMMAND_RATE_S = 60.0
#: How long a `command` waits for the harness to settle it, unless the caller says.
COMMAND_WAIT_S = 30.0
#: How many settled deliveries an endpoint remembers for a late `wait`.
SETTLED_KEPT = 256
#: How long a delivery or command handed to the adapter may stay unsettled. Past it the
#: endpoint settles it (a delivery `Lost`, a command `Failed`) and frees its slot, so an
#: adapter that drops one cannot block the endpoint for its life (mod-control's
#: `PENDING_TTL_MS` bounds its pending request the same way).
PENDING_TTL_S = 300.0
#: The reason an expired delivery or command settles with, and `entering` returns.
EXPIRED = "expired"

_QUEUED, _INFLIGHT, _STARTED, _DONE = "queued", "in-flight", "started", "done"


def package_version() -> str:
    try:
        from importlib.metadata import version

        return version("moxi-turns")
    except Exception:  # noqa: BLE001
        return "0+unknown"


@dataclass
class Handed:
    """One delivery as the endpoint keeps it."""

    spec: Delivery
    state: str
    version_seen: int | None = None
    turn: Turn | None = None
    started: Started | Refused | Lost | None = None
    ended: Ended | Refused | Lost | None = None
    #: When it went in flight (monotonic), for the expiry.
    handed_at: float | None = None
    expired: bool = False

    @property
    def id(self) -> str:
        return self.spec.id


@dataclass
class _Command:
    request: CommandRequest
    version_seen: int
    result: Ran | Failed | Refused | Passed | None = None
    handed_at: float = field(default_factory=time.monotonic)
    expired: bool = False


@dataclass
class _Holder:
    conn: protocol.LineConn
    asks: dict[int, list] = field(default_factory=dict)


class _Watcher:
    """One `watch` connection: events go out from a thread of its own, in ledger order,
    so a slow reader never holds up the harness."""

    def __init__(self, conn: protocol.LineConn):
        self.conn = conn
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.thread = threading.Thread(target=self._run, daemon=True,
                                       name="turns-watcher")
        self.thread.start()

    def put(self, frame: dict | None) -> None:
        self.queue.put(frame)

    def _run(self) -> None:
        while True:
            frame = self.queue.get()
            if frame is None:
                break
            try:
                self.conn.send(frame)
            except (OSError, protocol.ProtocolError):
                break
        self.conn.close()


class Endpoint:
    """The core of one endpoint."""

    def __init__(self, harness: Harness, *, env: Mapping[str, str] | None = None,
                 registry: turns_listeners.Registry | None = None,
                 session_id: str | None = None):
        self.harness = harness
        #: The session's environment: the endpoint inherits it, and listeners get it.
        self.env: dict[str, str] = dict(os.environ if env is None else env)
        self.layout = Layout(turns_config.MODULE)
        self._config = turns_config.reader(self.env)
        settings = self._config.start()          # a ConfigError here refuses the start
        self.hold_wait_s = max(0, int(settings["hold.wait_ms"])) / 1000
        self.listen_wait_s = max(0, int(settings["listen.wait_ms"])) / 1000
        #: The bound on an unsettled delivery or command (`PENDING_TTL_S`).
        self.pending_ttl_s = PENDING_TTL_S
        self.registry = registry if registry is not None else turns_listeners.load()
        self.nonce = secrets.token_hex(6)
        self.version = package_version()
        self.started_at = time.time()
        self.dir = self.layout.runtime_dir(self.env) / f"{harness.name}-{self.nonce}"
        self.sock_path = self.dir / protocol.SOCK
        self.hello_path = self.dir / protocol.HELLO

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self.ledger = TurnLedger(session_id or getattr(harness, "session_id", None))
        self._ids = itertools.count(1)
        self._deliveries: dict[str, Handed] = {}
        self._settled: deque[str] = deque()
        self._queue: deque[str] = deque()
        self._inflight: str | None = None
        self._commands: dict[str, _Command] = {}
        self._command_inflight: str | None = None
        self._command_accepted_at: float | None = None
        self._watchers: list[_Watcher] = []
        #: Turns whose events wait for the tie (§5.4 step 2), oldest first.
        self._held: list[Turn] = []
        self._attached = False
        self._holder: _Holder | None = None
        self._asks = itertools.count(1)
        self._gone: str | None = None
        self._closed = threading.Event()
        self._stopping = threading.Event()
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    # -- life --------------------------------------------------------------------------

    def prepare(self) -> "Endpoint":
        """Attach the adapter and make the endpoint's directory (0700), without opening
        the socket. An adapter that needs the directory before callers may find the
        endpoint (the Claude half's `mod.sock`) calls this first; `open()` calls it too.
        Safe to repeat."""
        if not self._attached:
            self.harness.attach(self)
            self._attached = True
        root = self.dir.parent
        root.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(root, 0o700)
        self.dir.mkdir(mode=0o700, exist_ok=True)
        os.chmod(self.dir, 0o700)
        return self

    def open(self) -> "Endpoint":
        """Make the directory, bind the socket, write `hello.json`, start answering."""
        self.prepare()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(str(self.sock_path))
            os.chmod(self.sock_path, 0o600)
            server.listen(16)
        except OSError:
            server.close()
            self._remove_files()
            raise
        server.settimeout(0.2)
        self._server = server
        # hello.json is written after the socket listens, so a finder that reads it
        # finds a socket that answers, or a dead one it may sweep.
        self._write_hello()
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True,
                                               name="turns-endpoint-accept")
        self._accept_thread.start()
        threading.Thread(target=self._expiry_loop, daemon=True,
                         name="turns-endpoint-expiry").start()
        log.info("endpoint %s for %s pid %s at %s", self.nonce, self.harness.name,
                 self.harness.pid, self.dir)
        return self

    def stop(self, reason: str = "the endpoint stopped") -> None:
        """End the endpoint: every open delivery answers `Lost`, watchers get `Gone`, the
        listeners are told `gone`, and the directory is removed. Safe to repeat."""
        with self._lock:
            if self._gone is not None:
                return
            # A held turn's window fails with the endpoint (§5.7, the tie's third end):
            # its events go out as the person's before `Gone`.
            for turn in list(self._held):
                self._decide(turn, None)
            self._gone = reason
            self._stopping.set()
            lost = Lost(reason)
            for d in list(self._deliveries.values()):
                if d.state != _DONE:
                    self._settle(d, started=lost if d.started is None else None,
                                 ended=lost)
            for c in self._commands.values():
                if c.result is None:
                    c.result = Failed(reason)
            self._command_inflight = None
            self._queue.clear()
            self._inflight = None
            self._emit(Gone(reason))
            for w in self._watchers:
                w.put(None)
            self._watchers.clear()
            holder, self._holder = self._holder, None
            if holder is not None:
                for slot in holder.asks.values():
                    slot[0].set()
            self._cond.notify_all()
        if holder is not None:
            holder.conn.close()
        turns_listeners.ask(self.registry, self._event("gone", reason=reason),
                            self.listen_wait_s, wait=True)
        if self._server is not None:
            with contextlib.suppress(OSError):
                self._server.close()
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)
        self._remove_files()
        with contextlib.suppress(Exception):
            self.harness.close()
        self._closed.set()
        log.info("endpoint %s ended: %s", self.nonce, reason)

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self._closed.wait(timeout)

    @property
    def is_gone(self) -> bool:
        return self._gone is not None

    def hello(self) -> dict[str, Any]:
        with self._lock:
            hello = {
                "harness": self.harness.name,
                "pid": self.harness.pid,
                "session_id": self.ledger.session_id,
                "earlier": list(self.ledger.earlier),
                "cwd": self.harness.cwd,
                "can": sorted(self.harness.can),
                "nonce": self.nonce,
                "started": self.started_at,
                "endpoint_version": self.version,
                "mod_version": self.harness.mod_version,
                "protocol": sorted(protocol.VERSIONS),
            }
            trouble = getattr(self.harness, "trouble", None)
            if trouble:
                hello["trouble"] = trouble
            return hello

    def _write_hello(self) -> None:
        data = json.dumps(self.hello(), indent=1, sort_keys=True) + "\n"
        tmp = self.dir / protocol.HELLO_TMP
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
        os.replace(tmp, self.hello_path)

    def _remove_files(self) -> None:
        for name in protocol.ENDPOINT_FILES:
            with contextlib.suppress(OSError):
                (self.dir / name).unlink()
        with contextlib.suppress(OSError):
            self.dir.rmdir()

    # -- the adapter's side ------------------------------------------------------------

    def identify(self, session_id: str | None, *, ready: str | None = None) -> None:
        """Update the launch's session id. With `ready`, also raise ``Ready`` with that
        source, and ask no listener: a host's accepted load (RULE T7).

        A host can learn its id before the harness fires SessionStart. Addressing must
        follow that id at once, while listener context still belongs to the later hook.
        """
        with self._lock:
            if self._gone is not None:
                return
            if self.ledger.new_session(session_id):
                self._rewrite_hello()
            if ready is not None:
                self._emit(Ready(ready, self.ledger.session_id))

    def load_changed(self, *, release: bool = True) -> None:
        """Publish changed load health and release delivery queued at the load gate."""
        self._rewrite_hello()
        if release:
            self._pump()

    def ready(self, source: str | None, session_id: str | None, *,
              announce: bool = True) -> str | None:
        """A session started, resumed, was cleared or compacted. Returns the listeners'
        added context for the event. `announce=False` asks the listeners without raising
        ``Ready``, for an event whose ``Ready`` the host already raised."""
        with self._lock:
            if self._gone is not None:
                return None
            if self.ledger.new_session(session_id):
                self._rewrite_hello()
            if announce:
                self._emit(Ready(source, self.ledger.session_id))
            event = self._event("ready", source=source)
        return turns_listeners.ask(self.registry, event, self.listen_wait_s)

    def prompt(self) -> str | None:
        """A prompt entered (UserPromptSubmit). Returns the listeners' added context."""
        if self._gone is not None:
            return None
        return turns_listeners.ask(self.registry, self._event("prompt"), self.listen_wait_s)

    def turn_started(self, turn_id: str | None, delivery_id: str | None = None, *,
                     held: bool = False) -> None:
        """A main-loop turn started. `delivery_id` is the adapter's tie (§5.7): the
        delivery that took this turn, or None for the person's own turn.

        `held`: which delivery took the turn is decided only after it started (Claude
        Code's tie, §5.4 step 2). The ledger opens the turn now, so `facts()` and a
        start's re-check see it, but its `TurnStarted` (and its `TurnEnded`, if it ends
        first) wait for `turn_tied` or `turn_released`."""
        if held:
            with self._lock:
                if self._gone is not None:
                    return
                previous, turn = self.ledger.started(turn_id, None, held=True)
                if previous is not None:
                    log.warning("turn %s started while turn %s was open; that one ends as "
                                "error", turn_id, previous.turn_id)
                    self._turn_closed(previous)
                self._held.append(turn)
                self._cond.notify_all()
            return
        with self._lock:
            if self._gone is not None:
                return
            d = self._deliveries.get(delivery_id) if delivery_id else None
            if delivery_id and (d is None or d.state not in (_QUEUED, _INFLIGHT)):
                log.warning("turn %s names delivery %s, which is not waiting to start; "
                            "it counts as the person's turn", turn_id, delivery_id)
                delivery_id, d = None, None
            previous, turn = self.ledger.started(turn_id, delivery_id)
            if previous is not None:
                log.warning("turn %s started while turn %s was open; that one ends as "
                            "error", turn_id, previous.turn_id)
                self._turn_closed(previous)
            if d is not None:
                d.state = _STARTED
                d.turn = turn
                d.started = Started(turn_id)
                if self._inflight == d.id:
                    self._inflight = None
                with contextlib.suppress(ValueError):
                    self._queue.remove(d.id)
            self._emit(TurnStarted(turn_id, turn.session_id, delivery_id))
            self._cond.notify_all()

    def turn_ended(self, turn_id: str | None, outcome: str) -> None:
        """The open main-loop turn ended with `outcome` (one of `results.OUTCOMES`)."""
        if outcome not in OUTCOMES:
            log.warning("turn %s ended with outcome %r, not one of %s; it counts as "
                        "error", turn_id, outcome, ", ".join(OUTCOMES))
            outcome = "error"
        with self._lock:
            if self._gone is not None:
                return
            turn = self.ledger.ended(outcome)
            if turn is None:
                log.warning("turn %s ended while no turn was open; ignored", turn_id)
                return
            if turn_id is not None and turn.turn_id is not None and turn_id != turn.turn_id:
                log.warning("turn %s ended while the ledger held turn %s open; the open "
                            "one ends", turn_id, turn.turn_id)
            self._turn_closed(turn)
            self._cond.notify_all()
        self._pump()

    def turn_ending(self, turn_id: str | None) -> None:
        """The harness was told the open turn may end (on Codex, an unblocked Stop
        answer). The turn stays open until `turn_ended`, so `turn_open` holds and a start
        is still refused, but it takes no more steers: a `deliver(steer=True)` waits and
        starts a new turn after it, and a plain `steer` reads `idle`. Codex can write a
        text it takes in that gap into the turn and complete the turn without answering
        it."""
        with self._lock:
            turn = self.ledger.open
            if self._gone is not None or turn is None:
                return
            if turn_id is not None and turn.turn_id is not None and turn_id != turn.turn_id:
                log.warning("turn %s is ending while the ledger holds turn %s open; the "
                            "open one takes steers still", turn_id, turn.turn_id)
                return
            self.ledger.ending()

    def turn_tied(self, turn_id: str | None, delivery_id: str) -> bool:
        """The held turn `turn_id` took `delivery_id` (the tie decided): its events go out
        in order, naming the delivery, and the delivery reads `Started` (and `Ended`, if
        the turn has ended). True when the tie took; False when there was no such held
        turn or the delivery was no longer waiting to start, and then the turn, if held,
        is released as the person's."""
        with self._lock:
            turn = self._held_turn(turn_id)
            if turn is None:
                log.warning("tie of delivery %s to turn %s: no such held turn",
                            delivery_id, turn_id)
                return False
            d = self._deliveries.get(delivery_id)
            if d is None or d.state not in (_QUEUED, _INFLIGHT):
                log.warning("tie of turn %s to delivery %s, which is not waiting to "
                            "start; the turn counts as the person's", turn_id, delivery_id)
                d = None
            self._decide(turn, d)
        self._pump()
        return d is not None

    def turn_released(self, turn_id: str | None) -> None:
        """The held turn `turn_id` took no delivery: its events go out as the person's."""
        with self._lock:
            turn = self._held_turn(turn_id)
            if turn is not None:
                self._decide(turn, None)

    def lost(self, delivery_id: str, reason: str) -> None:
        """The adapter cannot say which turn, if any, took a delivery it was handed (on
        Claude Code a submit's window closed with no turn tied; on Codex a request Codex
        was sent, a `turn/start` or `turn/steer`, that the host could not tie to a turn:
        no answer came, or no turn it had not already reported was in progress; the
        reason starts `untied`). It answers `Lost` and frees its slot. A steer is first
        taken off the turn it was steered into, as a refused one is."""
        with self._lock:
            d = self._deliveries.get(delivery_id)
            if d is None or d.state == _DONE:
                return
            if d.turn is not None and delivery_id in d.turn.steered:
                d.turn.steered.remove(delivery_id)
                d.turn = None
                d.started = None
            lost = Lost(reason)
            self._settle(d, started=lost if d.started is None else None, ended=lost)
            if self._inflight == d.id:
                self._inflight = None
            with contextlib.suppress(ValueError):
                self._queue.remove(d.id)
        self._pump()

    def is_waiting(self, delivery_id: str) -> bool:
        """Whether a delivery handed to the adapter still waits to start (not tied,
        refused, lost or expired)."""
        with self._lock:
            d = self._deliveries.get(delivery_id)
            return d is not None and d.state in (_QUEUED, _INFLIGHT) and not d.expired

    def learn_session(self, session_id: str | None) -> bool:
        """The adapter learned the session's id (on Claude Code the runner sends it on
        every load). True when it changed; `hello.json` follows it."""
        with self._lock:
            if self._gone is not None or not self.ledger.new_session(session_id):
                return False
            self._rewrite_hello()
            return True

    def entering(self, delivery_id: str) -> str | None:
        """A start is about to take effect: re-check it against the ledger (§5.4 step 4).
        A reason refuses it (the delivery answers `Refused`); None lets it in. Any other
        delivery passes untouched."""
        with self._lock:
            d = self._deliveries.get(delivery_id)
            if d is not None and d.expired:
                return EXPIRED
            if d is None or d.state != _INFLIGHT:
                return "gone" if self._gone is not None else None
            if d.spec.kind != "start":
                return None
            reason = _moved(self.ledger.read(), d.version_seen)
            if reason is not None:
                self._refuse(d, reason)
        if reason is not None:
            self._pump()
        return reason

    def refused(self, delivery_id: str, reason: str) -> None:
        """The adapter refused a delivery it was handed (a failed check where the start
        took effect, a harness that would not take the text)."""
        with self._lock:
            d = self._deliveries.get(delivery_id)
            if d is None or d.state == _DONE:
                return
            if d.turn is not None and delivery_id in d.turn.steered:
                d.turn.steered.remove(delivery_id)
                d.turn = None
                d.started = None
            self._refuse(d, reason)
        self._pump()

    def steer_missed(self, delivery_id: str) -> bool:
        """Move a steer that atomically missed its turn onto the deliver path.

        The adapter calls this before it reports the racing turn's end. The delivery is
        detached from that turn, then waits behind it like an ordinary delivery. False
        means the delivery no longer names that open turn and nothing changed.
        """
        with self._lock:
            d = self._deliveries.get(delivery_id)
            if (d is None or not d.spec.fallback or d.state != _STARTED or d.turn is None
                    or delivery_id not in d.turn.steered):
                return False
            d.turn.steered.remove(delivery_id)
            d.turn = None
            d.started = None
            d.ended = None
            d.spec = replace(d.spec, kind="deliver", fallback=False)
            d.state = _QUEUED
            call = self._admit(d)
            self._cond.notify_all()
        if call is not None:
            self._hand(d, call)
        return True

    def on_stop(self, facts: StopFacts) -> str | None:
        """A Stop of the main loop: ask the holder. A string holds the turn open with
        that text as the reason; None (no holder, no answer in time) lets it end."""
        with self._lock:
            holder = self._holder
            if holder is None or self._gone is not None:
                return None
            ask_id = next(self._asks)
            slot: list = [threading.Event(), None]
            holder.asks[ask_id] = slot
        try:
            holder.conn.send({"v": protocol.VERSION, "op": "stop", "ask": ask_id,
                              "facts": facts.to_wire()})
            slot[0].wait(self.hold_wait_s)
        except (OSError, protocol.ProtocolError):
            pass
        finally:
            with self._lock:
                holder.asks.pop(ask_id, None)
        if not slot[0].is_set():
            log.info("the holder did not answer Stop %s within %d ms; the turn ends",
                     ask_id, int(self.hold_wait_s * 1000))
            return None
        answer = slot[1]
        return answer if isinstance(answer, str) and answer else None

    def command_entering(self, request_id: str) -> str | None:
        """A session command is about to take effect: the re-check (RULE T4). A reason
        refuses it."""
        with self._lock:
            c = self._commands.get(request_id)
            if c is not None and c.expired:
                return EXPIRED
            if c is None or c.result is not None:
                return "gone" if self._gone is not None else None
            reason = _moved(self.ledger.read(), c.version_seen)
            if reason is not None:
                self._settle_command(c, Refused(reason))
            return reason

    def command_settled(self, request_id: str, result: Ran | Failed | Refused) -> None:
        """A command the adapter was handed ran, failed or was refused."""
        with self._lock:
            c = self._commands.get(request_id)
            if c is not None and c.result is None:
                self._settle_command(c, result)

    def gone(self, reason: str = "the session ended") -> None:
        """The session ended."""
        self.stop(reason)

    # -- the caller's side -------------------------------------------------------------

    def facts(self) -> Facts:
        first, live, second = self._snapshot()
        return Facts(session_id=second.session_id, epoch=second.epoch,
                     turn_open=second.turn_open,
                     agents=live.agents if live else None,
                     draft=live.draft if live else True,
                     at=time.time(), stale=first.version != second.version)

    def start(self, text: str, *, if_epoch: int | None = None,
              refuse_draft: bool = False, refuse_agents: bool = False) -> Handed:
        """A new turn, only if none is open (and the caller's other preconditions hold)."""
        d = self._new("start", text, refuse_draft=refuse_draft, refuse_agents=refuse_agents)
        reason = self._unable("start")
        if reason is None:
            first, live, second = self._snapshot()
            with self._lock:
                reason = self._idle_refusal(first, second, if_epoch)
                if reason is None and refuse_agents and _busy(live):
                    reason = "busy-agents"
                if reason is None and refuse_draft and (live is None or live.draft):
                    reason = "draft"
                if reason is None:
                    d.version_seen = second.version
                    d.state = _INFLIGHT
                    d.handed_at = time.monotonic()
                    self._inflight = d.id
        if reason is not None:
            with self._lock:
                self._refuse(d, reason)
            return d
        self._hand(d, self.harness.submit)
        return d

    def deliver(self, text: str, *, steer: bool = False) -> Handed:
        """A new turn now if the session is idle, else when the running turn ends. With
        `steer` on a harness that can, a running turn takes the text at once."""
        if self.harness.loading():
            # Held at the load gate. A steer-first deliver keeps that intent, so the
            # accepted load's running turn takes it (RULE T7).
            d = self._new("deliver", text, fallback=steer and "steer" in self.harness.can)
            with self._lock:
                call = self._admit(d)
            if call is not None:
                self._hand(d, call)
            return d
        if steer and "steer" in self.harness.can:
            # The steer and its fallback are decided under one hold of the lock, so a
            # turn that ends just before cannot bounce the text as `idle`: no open turn
            # means this is a plain deliver, a new turn now or after the one in flight.
            return self._steer(self._new("steer", text), or_deliver=True)
        d = self._new("deliver", text)
        with self._lock:
            call = self._admit(d)
        if call is not None:
            self._hand(d, call)
        return d

    def steer(self, text: str) -> Handed:
        """The text into the running turn; `idle` when none runs."""
        d = self._new("steer", text)
        with self._lock:
            reason = self._unable("steer")
            if reason is None and not self.ledger.read().turn_open:
                reason = "idle"
            if reason is not None:
                self._refuse(d, reason)
                return d
        return self._steer(d)

    def wait(self, delivery_id: str, until: str, timeout: float | None):
        """`Started | Refused | Lost` (until "started") or `Ended | Refused | Lost`
        (until "ended"), or None when `timeout` passes first."""
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._cond:
            while True:
                d = self._deliveries.get(delivery_id)
                if d is None:
                    return Lost("unknown delivery: it was not made at this endpoint, or "
                                "it settled long ago")
                result = d.started if until == "started" else d.ended
                if result is not None:
                    return result
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return None
                self._cond.wait(remaining)

    def command(self, name: str, *, if_epoch: int | None = None,
                timeout: float | None = COMMAND_WAIT_S) -> Ran | Failed | Refused:
        """Run `/clear` or `/exit` (RULE T4): consented, one in flight, one accepted per
        60 s, from an idle snapshot with no open turn, no live subagent and no draft."""
        if name not in COMMANDS:
            return Refused("unsupported")
        reason = self._unable(name)
        if reason is None:
            allow = turns_config.allowed(self._config.use("commands.allow"), self.env)
            if name not in allow:
                reason = "not-allowed"
        if reason is None:
            reason = self._command_slot_refusal()
        if reason is None:
            first, live, second = self._snapshot()
            with self._lock:
                reason = self._idle_refusal(first, second, if_epoch)
                if reason is None and (live is None or live.draft):
                    reason = "draft"         # an unreadable box holds
                if reason is None and _busy(live):
                    reason = "busy-agents"   # an unreadable list counts as busy
                if reason is None:
                    reason = self._command_slot_refusal()
                if reason is None:
                    request = CommandRequest(f"{self.nonce}-c{next(self._ids)}", name)
                    c = self._commands[request.id] = _Command(request, second.version)
                    self._command_inflight = request.id
                    self._command_accepted_at = time.monotonic()
        if reason is not None:
            return Refused(reason)
        try:
            self.harness.command(request)
        except Exception as exc:  # noqa: BLE001
            log.warning("the adapter failed to run /%s: %s", name, exc)
            with self._lock:
                self._settle_command(c, Failed(f"the adapter failed: {exc}"))
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        with self._cond:
            while c.result is None:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return Failed("no answer in time; the command may still run")
                self._cond.wait(remaining)
            return c.result

    # -- internals ---------------------------------------------------------------------

    def _new(self, kind: str, text: str, **checks) -> Handed:
        d = Handed(Delivery(f"{self.nonce}-{next(self._ids)}", text, kind, **checks),
                   _QUEUED)
        with self._lock:
            self._deliveries[d.id] = d
        return d

    def _unable(self, capability: str) -> str | None:
        if self._gone is not None:
            return "gone"
        if capability not in self.harness.can:
            return "unsupported"
        if capability in ("start", "steer") and self.harness.loading():
            return "loading"
        return None

    def _idle_refusal(self, first: Reading, second: Reading,
                      if_epoch: int | None) -> str | None:
        """The ledger half of an idle snapshot, under the lock."""
        if self._gone is not None:
            return "gone"
        if first.version != second.version:
            return "stale-turn"
        # A delivery in flight or held is a turn about to open.
        if second.turn_open or self._inflight or self._queue:
            return "busy-turn"
        if if_epoch is not None and if_epoch != second.epoch:
            return "stale-turn"
        return None

    def _command_slot_refusal(self) -> str | None:
        with self._lock:
            if self._command_inflight is not None:
                return "in-flight"
            if (self._command_accepted_at is not None
                    and time.monotonic() - self._command_accepted_at < COMMAND_RATE_S):
                return "rate"
            return None

    def _live(self) -> Live | None:
        try:
            return self.harness.live()
        except Exception as exc:  # noqa: BLE001 - an unreadable fact refuses, never passes
            log.warning("reading the live facts failed: %s", exc)
            return None

    def _snapshot(self) -> tuple[Reading, Live | None, Reading]:
        """The ledger, the live facts, the ledger again: one observation, or two readings
        that differ (mod-control's snapshot)."""
        with self._lock:
            first = self.ledger.read()
        live = self._live()
        with self._lock:
            second = self.ledger.read()
        return first, live, second

    def _admit(self, d: Handed) -> Callable[[Delivery], None] | None:
        """A deliver, under the lock: refused, held behind the running turn or the one in
        flight, or put in flight now (returns the adapter call to hand it to)."""
        if self._gone is None and "start" in self.harness.can and self.harness.loading():
            self._queue.append(d.id)
            return None
        reason = self._unable("start")
        if reason is not None:
            self._refuse(d, reason)
            return None
        if self.ledger.read().turn_open or self._inflight or self._queue:
            self._queue.append(d.id)
            return None
        d.state = _INFLIGHT
        d.handed_at = time.monotonic()
        self._inflight = d.id
        return self.harness.submit

    def _steer(self, d: Handed, *, or_deliver: bool = False) -> Handed:
        """The text into the open turn. With no open turn: `idle`, or with `or_deliver`
        (a `deliver(steer=True)`) the deliver path, decided under the same lock."""
        with self._lock:
            turn = self.ledger.steered_into(d.id) if self._gone is None else None
            if turn is None:
                if not or_deliver:
                    self._refuse(d, "gone" if self._gone is not None else "idle")
                    return d
                d.spec = replace(d.spec, kind="deliver")
                call = self._admit(d)
            else:
                if or_deliver:
                    d.spec = replace(d.spec, fallback=True)
                d.state = _STARTED
                d.turn = turn
                d.started = Started(turn.turn_id)
                self._cond.notify_all()
                call = self.harness.append
        if call is not None:
            self._hand(d, call)
        return d

    def _hand(self, d: Handed, call: Callable[[Delivery], None]) -> None:
        try:
            call(d.spec)
        except Exception as exc:  # noqa: BLE001
            log.warning("the adapter could not take delivery %s: %s", d.id, exc)
            self.refused(d.id, "gone")

    def _expiry_loop(self) -> None:
        while not self._stopping.wait(min(1.0, max(0.01, self.pending_ttl_s / 4))):
            self._expire(time.monotonic())

    def _expire(self, now: float) -> None:
        """Settle the delivery or command the adapter has held unsettled past
        `pending_ttl_s`, and free its slot. A late word from the adapter about it is
        ignored, and its `entering` answers `expired`."""
        ttl = self.pending_ttl_s
        freed: str | None = None
        with self._lock:
            if self._gone is not None:
                return
            d = self._deliveries.get(self._inflight) if self._inflight else None
            if (d is not None and d.state == _INFLIGHT and d.handed_at is not None
                    and now - d.handed_at >= ttl):
                log.warning("the harness neither started nor refused delivery %s within "
                            "%d s; it answers lost", d.id, int(ttl))
                d.expired = True
                lost = Lost(f"{EXPIRED}: the harness neither started nor refused it "
                            f"within {ttl:g} s")
                self._settle(d, started=lost, ended=lost)
                self._inflight = None
                freed = d.id
            c = self._commands.get(self._command_inflight) if self._command_inflight else None
            if c is not None and c.result is None and now - c.handed_at >= ttl:
                log.warning("the harness did not settle /%s (%s) within %d s; it answers "
                            "failed", c.request.name, c.request.id, int(ttl))
                c.expired = True
                self._settle_command(c, Failed(f"{EXPIRED}: the harness did not settle it "
                                               f"within {ttl:g} s"))
        if freed is not None:
            try:
                self.harness.expired(freed)
            except Exception:  # noqa: BLE001 - the slot is freed either way
                log.exception("the adapter failed at the expiry of %s", freed)
            self._pump()

    def _pump(self) -> None:
        """Hand the next held delivery to the harness, once no turn runs and none is in
        flight. While a turn runs, a steer-first deliver held at the load gate at the head
        of the queue steers into it (RULE T7), unless that turn is ending: then it waits
        for the turn's end like any other."""
        while True:
            with self._lock:
                if (self._gone is not None or self._inflight or not self._queue
                        or self.harness.loading()):
                    return
                d = self._deliveries[self._queue[0]]
                turn = None
                if self.ledger.read().turn_open:
                    if not d.spec.fallback:
                        return
                    turn = self.ledger.steered_into(d.id)
                    if turn is None:
                        return   # the open turn is ending: no submit while it is open
                self._queue.popleft()
                if turn is None:
                    d.state = _INFLIGHT
                    d.handed_at = time.monotonic()
                    self._inflight = d.id
                    call = self.harness.submit
                else:
                    d.state = _STARTED
                    d.turn = turn
                    d.started = Started(turn.turn_id)
                    self._cond.notify_all()
                    call = self.harness.append
            self._hand(d, call)
            if turn is None:
                return

    def _refuse(self, d: Handed, reason: str) -> None:
        refused = Refused(reason)
        self._settle(d, started=refused if d.started is None else None, ended=refused)
        if self._inflight == d.id:
            self._inflight = None
        with contextlib.suppress(ValueError):
            self._queue.remove(d.id)

    def _settle(self, d: Handed, *, started=None, ended=None) -> None:
        if started is not None:
            d.started = started
        if ended is not None:
            d.ended = ended
        if d.ended is not None and d.state != _DONE:
            d.state = _DONE
            self._settled.append(d.id)
            while len(self._settled) > SETTLED_KEPT:
                self._deliveries.pop(self._settled.popleft(), None)
        self._cond.notify_all()

    def _settle_command(self, c: _Command, result) -> None:
        c.result = result
        if self._command_inflight == c.request.id:
            self._command_inflight = None
        self._cond.notify_all()

    def _held_turn(self, turn_id: str | None) -> Turn | None:
        for turn in self._held:
            if turn.turn_id == turn_id:
                return turn
        return None

    def _decide(self, turn: Turn, d: Handed | None) -> None:
        """Under the lock: a held turn's tie is decided. Raise its held events in order,
        naming `d` (or none: the person's turn), and settle `d` from them."""
        with contextlib.suppress(ValueError):
            self._held.remove(turn)
        turn.held = False
        if d is not None:
            turn.delivery_id = d.id
            d.state = _STARTED
            d.turn = turn
            d.started = Started(turn.turn_id)
            if self._inflight == d.id:
                self._inflight = None
            with contextlib.suppress(ValueError):
                self._queue.remove(d.id)
        self._emit(TurnStarted(turn.turn_id, turn.session_id, turn.delivery_id))
        if turn.ended_held:
            self._turn_closed(turn)
        self._cond.notify_all()

    def _turn_closed(self, turn: Turn) -> None:
        if turn.held:
            turn.ended_held = True   # its TurnEnded waits with its TurnStarted
            return
        outcome = turn.outcome or "error"
        ended = Ended(turn.turn_id, outcome)
        for delivery_id in [turn.delivery_id, *turn.steered]:
            d = self._deliveries.get(delivery_id) if delivery_id else None
            if d is not None and d.state != _DONE:
                self._settle(d, ended=ended)
        self._emit(TurnEnded(turn.turn_id, outcome, turn.session_id, turn.delivery_id,
                             tuple(turn.steered)))
        turns_listeners.ask(self.registry,
                            self._event("turn", turn_id=turn.turn_id, outcome=outcome),
                            self.listen_wait_s)

    def _emit(self, event) -> None:
        frame = {"v": protocol.VERSION, "event": event.to_wire()}
        for w in self._watchers:
            w.put(frame)

    def _event(self, name: str, **extra):
        return SessionEvent(event=name, harness=self.harness.name,
                            session_id=self.ledger.session_id, env=self.env, **extra)

    def _rewrite_hello(self) -> None:
        if self._server is None:
            return                  # not open yet: open() writes it
        try:
            self._write_hello()
        except OSError as exc:
            log.warning("rewriting %s failed: %s", self.hello_path, exc)

    # -- the socket --------------------------------------------------------------------

    def _accept_loop(self) -> None:
        server = self._server
        while self._gone is None and server is not None:
            try:
                sock, _ = server.accept()
            except (TimeoutError, socket.timeout):
                continue
            except OSError:
                break
            sock.settimeout(None)
            threading.Thread(target=self._serve, args=(protocol.LineConn(sock),),
                             daemon=True, name="turns-endpoint-conn").start()

    def _serve(self, conn: protocol.LineConn) -> None:
        handed_over = False
        try:
            while True:
                try:
                    frame = conn.recv()
                except protocol.ProtocolError as exc:
                    with contextlib.suppress(OSError, protocol.ProtocolError):
                        conn.send(_error(None, "bad-request", str(exc)))
                    return
                if frame is None:
                    return
                handed_over = self._dispatch(conn, frame)
                if handed_over:
                    return
        except (OSError, protocol.ProtocolError):
            return
        finally:
            if not handed_over:
                conn.close()

    def _dispatch(self, conn: protocol.LineConn, frame: dict) -> bool:
        """Answer one request. True when the connection went to a watch or a hold, which
        close it themselves."""
        rid = frame.get("id")
        if frame.get("v") not in protocol.VERSIONS:
            conn.send(_error(rid, "version", f"protocol version {frame.get('v')!r} is not "
                             "spoken here", versions=sorted(protocol.VERSIONS)))
            return False
        op = frame.get("op")
        try:
            if op == "hello":
                conn.send(_ok(rid, hello=self.hello()))
            elif op == "facts":
                conn.send(_ok(rid, facts=self.facts().to_wire()))
            elif op == "start":
                d = self.start(_text(frame), if_epoch=_opt_int(frame, "if_epoch"),
                               refuse_draft=_flag(frame, "refuse_draft"),
                               refuse_agents=_flag(frame, "refuse_agents"))
                conn.send(_ok(rid, **_delivery_reply(d)))
            elif op == "deliver":
                d = self.deliver(_text(frame), steer=_flag(frame, "steer"))
                conn.send(_ok(rid, **_delivery_reply(d)))
            elif op == "steer":
                conn.send(_ok(rid, **_delivery_reply(self.steer(_text(frame)))))
            elif op == "wait":
                until, delivery = frame.get("until"), frame.get("delivery")
                if until not in ("started", "ended") or not isinstance(delivery, str):
                    raise _BadRequest('wait needs "delivery" and "until" (started or '
                                      'ended)')
                result = self.wait(delivery, until, _timeout(frame, None))
                conn.send(_ok(rid, pending=True) if result is None
                          else _ok(rid, result=result.to_wire()))
            elif op == "command":
                name = frame.get("name")
                if not isinstance(name, str):
                    raise _BadRequest('command needs "name"')
                result = self.command(name, if_epoch=_opt_int(frame, "if_epoch"),
                                      timeout=_timeout(frame, COMMAND_WAIT_S))
                conn.send(_ok(rid, result=result.to_wire()))
            elif op == "watch":
                return self._watch(conn, rid)
            elif op == "hold":
                return self._hold(conn, rid)
            else:
                conn.send(_error(rid, "unknown-op", f"unknown op {op!r}; the ops are "
                                 + ", ".join(protocol.OPS)))
        except _BadRequest as exc:
            conn.send(_error(rid, "bad-request", str(exc)))
        return False

    def _watch(self, conn: protocol.LineConn, rid) -> bool:
        with self._lock:
            if self._gone is not None:
                conn.send(_ok(rid))
                conn.send({"v": protocol.VERSION, "event": Gone(self._gone).to_wire()})
                return False
            watcher = _Watcher(conn)
            watcher.put(_ok(rid))
            self._watchers.append(watcher)
        # The watcher's own thread writes; this one waits for the caller to hang up.
        with contextlib.suppress(protocol.ProtocolError):
            while conn.recv() is not None:
                pass
        with self._lock:
            if watcher in self._watchers:
                self._watchers.remove(watcher)
                watcher.put(None)
        return True

    def _hold(self, conn: protocol.LineConn, rid) -> bool:
        with self._lock:
            if self._gone is not None:
                conn.send(_error(rid, "gone", self._gone))
                return False
            if self._holder is not None:
                conn.send(_error(rid, "held", "this session already has a holder"))
                return False
            holder = self._holder = _Holder(conn)
            conn.send(_ok(rid))
        try:
            while True:
                try:
                    frame = conn.recv()
                except protocol.ProtocolError:
                    break
                if frame is None:
                    break
                if frame.get("op") != protocol.STOP_ANSWER:
                    continue
                with self._lock:
                    slot = holder.asks.get(frame.get("ask"))
                    if slot is not None:
                        reason = frame.get("reason")
                        slot[1] = reason if isinstance(reason, str) else None
                        slot[0].set()
        finally:
            with self._lock:
                if self._holder is holder:
                    self._holder = None
                for slot in holder.asks.values():
                    slot[0].set()
            conn.close()
        return True


def _busy(live: Live | None) -> bool:
    """A subagent pending, running or waiting, or a list that could not be read: unknown
    counts as busy, never as 0 (§5.7, idle facts)."""
    return live is None or live.agents is None or live.agents > 0


def _moved(reading: Reading, version_seen: int | None) -> str | None:
    """Where a start or command takes effect: a turn opened, or the ledger moved, since
    the snapshot it passed."""
    if reading.turn_open:
        return "busy-turn"
    if reading.version != version_seen:
        return "stale-turn"
    return None


class _BadRequest(Exception):
    pass


def _ok(rid, **fields) -> dict:
    return {"v": protocol.VERSION, "id": rid, "ok": True, **fields}


def _error(rid, code: str, message: str, **fields) -> dict:
    return {"v": protocol.VERSION, "id": rid, "ok": False, "code": code,
            "error": message, **fields}


def _text(frame: dict) -> str:
    text = frame.get("text")
    if not isinstance(text, str) or not text:
        raise _BadRequest('needs a non-empty "text"')
    return text


def _flag(frame: dict, key: str) -> bool:
    value = frame.get(key, False)
    if not isinstance(value, bool):
        raise _BadRequest(f'"{key}" must be true or false')
    return value


def _opt_int(frame: dict, key: str) -> int | None:
    value = frame.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _BadRequest(f'"{key}" must be an integer or null')
    return value


def _timeout(frame: dict, default_s: float | None) -> float | None:
    if "timeout_ms" not in frame:
        return default_s
    value = frame["timeout_ms"]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise _BadRequest('"timeout_ms" must be a number of milliseconds or null')
    return value / 1000


def _delivery_reply(d: Handed) -> dict:
    reply: dict[str, Any] = {"delivery": d.id}
    if isinstance(d.started, (Refused, Lost)):
        reply["result"] = d.started.to_wire()
    return reply


__all__ = ["COMMAND_RATE_S", "COMMAND_WAIT_S", "SETTLED_KEPT", "Endpoint", "Handed",
           "package_version"]
