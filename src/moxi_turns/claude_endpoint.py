"""The Claude Code endpoint: the endpoint core with the Claude half (`ClaudeHarness`).

`moxi-turns-endpoint` runs this. The runner plugin spawns it at `session.start` with the
absolute path `endpoint.json` names; it lives for the session and inherits the
session's environment. It speaks to its runner over the leg in `claude_leg` and to every
caller over the endpoint socket (`contracts/socket-protocol.md`).

The tie rule: each plugin submit has one window, opened when the endpoint hands
the runner its `submit` line and closed exactly once, at the first end it accepts: the
runner's `submit.close` report (`resolve`, `drop` or `fail`), the runner's `submit.bound`
report while the prompt is not admitted, a report the runner could not deliver, the
core's expiry, or the endpoint's own end. Inside the window the endpoint holds the events
of the last main-loop turn whose `turn.start` was reported there; a later `turn.start`
releases the held one as the person's. At the close:

- resolve: the delivery is tied to the held turn; with none held it reads `Lost`
  (`untied: …`);
- drop: a held turn is released; the delivery reads `Refused` with the reason the
  endpoint recorded, else the plugins' own code from the runner's report (`gone`,
  `unsupported`), else `dropped` for another plugin's drop;
- fail: a held turn is released; the delivery reads `Lost` (`untied: …`).

The endpoint hands the next submit only once the window has closed. The ledger opens and
closes turns on the reports at once; only the events and the delivery's answers wait.

The entry rule: a runner prompt enters only as the prompt of the window now open,
admitted once by the endpoint. Every other runner prompt is dropped at entry, logged, and
never runs. The endpoint mints a random window nonce (128 bits) at each hand-over, the
runner puts it on the prompt's first line, and the gate asks `/gate/entry` with it for
every runner prompt. The endpoint admits the prompt only if the nonce names the open
window and that window has admitted nothing; for a start it re-checks there. Admission
and the bound's fail are both decided here, under one lock, in the order the gate's ask
and the runner's report arrive, so no window is both admitted and failed by the bound.

An admitted window's liveness bound: from admission the 5 s
bound no longer fails it. It closes at its submit's settle, by a report the runner could
not deliver, at the endpoint's end, or at the core's expiry, `PENDING_TTL_S` (300 s) from
the hand-over, when the delivery reads `Lost('expired: …')` and the window closes and frees
the slot. A settings hook that hangs, a modal opened between the gate's pass and the
entry, or a Claude Code fault can hold an admitted window that long; nothing is lost, and
a caller that will not wait that long frees itself with its own `wait_*` timeout.

The endpoint's life, a rule: it ends on its own SIGTERM, an EPIPE on its stdout, or
a change of parent, and never on a `session.end` hook. It takes its session id from the
runner on every load, and opens its caller socket (answers `hello`) once it has the id.
"""

from __future__ import annotations

import itertools
import logging
import os
import secrets
import select
import signal
import sys
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, BinaryIO

from . import claude_leg as leg
from .harness import CommandRequest, Delivery, Harness, Live
from .results import (CAPABILITIES, COMMAND_REFUSALS, LOST_UNTIED, OUTCOMES, Failed,
                      Passed, Ran, Refused, StopFacts)

log = logging.getLogger("moxi.turns.claude")

#: How long `live()` waits for the runner's `/live` answer. Past it the live facts are
#: unreadable: a start that asked for the draft or subagent refusal refuses, and a
#: command refuses `draft` (an unreadable box holds).
LIVE_WAIT_S = 1.0
#: How often the endpoint looks at its parent when no pidfd can watch it.
PARENT_POLL_S = 0.5
#: How many closed windows the endpoint remembers, to name a late entry's delivery.
CLOSED_KEPT = 64


@dataclass
class _Window:
    """One plugin submit's window."""

    delivery: str
    kind: str
    #: The window nonce: random, 128 bits, minted at the hand-over.
    nonce: str = field(default_factory=lambda: secrets.token_hex(16))
    #: The endpoint admitted the window's prompt (`/gate/entry` passed it). From here the
    #: runner's bound no longer fails the window.
    admitted: bool = False
    opened_at: float = field(default_factory=time.monotonic)
    #: The turn held in the window: the last main-loop `turn.start` reported in it.
    held: str | None = None
    held_set: bool = False


class ClaudeHarness(Harness):
    """The Claude half of an endpoint: the runner's leg under the endpoint core."""

    name = "claude"
    can = frozenset(CAPABILITIES)

    def __init__(self, out: BinaryIO, *, pid: int | None = None, cwd: str | None = None,
                 on_end=None):
        self.pid = os.getppid() if pid is None else pid
        self.cwd = os.getcwd() if cwd is None else cwd
        self.mod_version: str | None = None
        self._lines = leg.LineWriter(out, on_broken=on_end)
        self._lock = threading.RLock()
        self._window: _Window | None = None
        self._pending: list[Delivery] = []
        self._last_seq = 0
        self._asks = itertools.count(1)
        self._live: dict[int, list] = {}
        self._commands: dict[str, CommandRequest] = {}
        self._clear_passed: str | None = None
        #: Delivery ids the gate refused, for a `drop` close.
        self._gate_refused: set[str] = set()
        #: Closed windows by nonce: (delivery, how it closed), to name a late entry.
        self._closed: OrderedDict[str, tuple[str, str]] = OrderedDict()
        self.session_known = threading.Event()
        self.server: leg.LegServer | None = None

    # -- the leg ------------------------------------------------------------------------

    @property
    def broken(self) -> bool:
        return self._lines.broken

    def serve(self, path: str) -> None:
        """Listen on `mod.sock` at `path`."""
        self.server = leg.LegServer(path, self.handle).start()
        os.chmod(path, 0o600)

    def first_line(self) -> bool:
        ep = self.endpoint
        return self._lines.write({
            "op": leg.LINE_HELLO, "leg": leg.LEG, "mod_sock": str(self.server.path),
            "hold_wait_ms": int(ep.hold_wait_s * 1000),
            "listen_wait_ms": int(ep.listen_wait_s * 1000),
            "endpoint_version": ep.version, "pid": os.getpid()})

    def handle(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path == leg.PATH_REPORT:
            self.report(body)
            return {}
        if path == leg.PATH_LIVE:
            self._live_answer(body)
            return {}
        if path == leg.PATH_STOP:
            return {"reason": self.endpoint.on_stop(_stop_facts(body))}
        if path == leg.PATH_CONTEXT:
            return {"context": self._context(body)}
        if path == leg.PATH_GATE_ENTRY:
            return self._gate_entry(body)
        if path == leg.PATH_GATE_COMMAND:
            return self._gate_command(body)
        raise leg.BadRequest(f"no such path {path!r}")

    def close(self) -> None:
        if self.server is not None:
            try:
                self.server.close()
            except OSError:
                pass

    # -- the core calls these -----------------------------------------------------------

    def live(self) -> Live:
        ask = next(self._asks)
        slot: list = [threading.Event(), None]
        with self._lock:
            self._live[ask] = slot
        try:
            if not self._lines.write({"op": leg.LINE_LIVE, "ask": ask}):
                raise OSError("the runner's line is closed")
            if not slot[0].wait(LIVE_WAIT_S):
                raise TimeoutError(f"the runner did not answer live within {LIVE_WAIT_S} s")
        finally:
            with self._lock:
                self._live.pop(ask, None)
        answer = slot[1] or {}
        if answer.get("error") is not None:
            raise OSError(f"the runner could not read the live facts: {answer['error']}")
        agents, draft = answer.get("agents"), answer.get("draft")
        # `agents` null: the runner could not list the subagents. Unknown, which counts
        # as busy and never reads as 0.
        if (isinstance(agents, bool) or not (agents is None or isinstance(agents, int))
                or not isinstance(draft, bool)):
            raise OSError(f"the runner's live answer is malformed: {answer!r}")
        return Live(agents=agents, draft=draft)

    def submit(self, delivery: Delivery) -> None:
        with self._lock:
            self._pending.append(delivery)
            self._next_window()

    def expired(self, delivery_id: str) -> None:
        """The core expired the delivery (`PENDING_TTL_S`): its window closes now, admitted
        or not, so it holds no turn and no later submit (the liveness bound, above)."""
        with self._lock:
            w = self._window
            if w is not None and w.delivery == delivery_id:
                log.warning("window of %s (%s) expired; it closes", delivery_id,
                            "admitted" if w.admitted else "not admitted")
                self._close(leg.END_FAIL, "expired: the submit never settled")

    def append(self, delivery: Delivery) -> None:
        if not self._lines.write({"op": leg.LINE_APPEND, "delivery": delivery.id,
                                  "text": delivery.text}):
            raise OSError("the runner's line is closed")

    def command(self, request: CommandRequest) -> None:
        with self._lock:
            self._commands[request.id] = request
        if not self._lines.write({"op": leg.LINE_COMMAND, "request": request.id,
                                  "name": request.name}):
            raise OSError("the runner's line is closed")

    # -- the window ---------------------------------------------------------------------

    def _next_window(self) -> None:
        """Under the lock: hand the runner the next pending submit, once no window is
        open. A delivery the core settled meanwhile (refused, expired) is skipped."""
        while self._window is None and self._pending:
            d = self._pending.pop(0)
            if not self.endpoint.is_waiting(d.id):
                continue
            # The window counts as open from here, a few ms before the runner's call.
            w = self._window = _Window(d.id, d.kind)
            if not self._lines.write({"op": leg.LINE_SUBMIT, "delivery": d.id,
                                      "kind": d.kind, "text": d.text, "nonce": w.nonce}):
                self._close(leg.END_FAIL, "the runner's line is closed")

    def _close(self, end: str, reason: str | None) -> None:
        """Under the lock: the open window closes by `end`."""
        w, self._window = self._window, None
        if w is None:
            return
        self._closed[w.nonce] = (w.delivery, end)
        while len(self._closed) > CLOSED_KEPT:
            self._closed.popitem(last=False)
        ep = self.endpoint
        if end == leg.END_RESOLVE:
            if w.held_set:
                ep.turn_tied(w.held, w.delivery)
            else:
                ep.lost(w.delivery, f"{LOST_UNTIED}: the submit resolved with no turn "
                                    "started in its window")
        else:
            if w.held_set:
                ep.turn_released(w.held)
            if end == leg.END_DROP:
                # The reason the endpoint recorded stands; else the plugins' own code
                # (the gate's `gone`, the runner's `unsupported`); else another plugin's
                # drop, `dropped`.
                refused_by_gate = w.delivery in self._gate_refused
                if not refused_by_gate:
                    ep.refused(w.delivery, reason if reason in leg.GATE_CODES
                               else "dropped")
                log.info("submit %s dropped%s: %s", w.delivery,
                         " by the gate" if refused_by_gate else "", reason)
            else:
                ep.lost(w.delivery, f"{LOST_UNTIED}: the submit failed"
                                    + (f": {reason}" if reason else ""))
        self._gate_refused.discard(w.delivery)
        self._next_window()

    # -- reports ------------------------------------------------------------------------

    def report(self, body: dict[str, Any]) -> None:
        kind = body.get("kind")
        if kind not in leg.REPORTS:
            raise leg.BadRequest(f"unknown report kind {kind!r}")
        seq = body.get("seq")
        with self._lock:
            if isinstance(seq, int) and not isinstance(seq, bool):
                if seq <= self._last_seq:
                    return                      # applied already: a resend
                if seq > self._last_seq + 1 and self._window is not None:
                    log.warning("reports %d to %d from the runner were lost; the open "
                                "submit window fails", self._last_seq + 1, seq - 1)
                    self._close(leg.END_FAIL, "a report from the runner was lost")
                self._last_seq = seq
            self._apply(kind, body)

    def _apply(self, kind: str, body: dict[str, Any]) -> None:
        ep = self.endpoint
        if kind == leg.R_SESSION:
            version = body.get("mod_version")
            if isinstance(version, str):
                self.mod_version = version
            self._learn(_opt_str(body, "session_id"))
        elif kind == leg.R_TURN_START:
            turn_id = _opt_str(body, "turn_id")
            w = self._window
            if w is None:
                ep.turn_started(turn_id)
                return
            if w.held_set:
                ep.turn_released(w.held)   # only the last can be the delivery's
            ep.turn_started(turn_id, held=True)
            w.held, w.held_set = turn_id, True
        elif kind == leg.R_TURN_COMPLETE:
            ep.turn_ended(_opt_str(body, "turn_id"), _outcome(body))
        elif kind == leg.R_SUBMIT_CLOSE:
            delivery, end = body.get("delivery"), body.get("end")
            if end not in leg.ENDS:
                raise leg.BadRequest(f"unknown submit end {end!r}")
            if self._window is None or self._window.delivery != delivery:
                log.info("submit %s settled (%s) after its window closed; it changes "
                         "nothing", delivery, end)
                return
            self._close(end, _opt_str(body, "reason"))
        elif kind == leg.R_SUBMIT_BOUND:
            delivery, w = body.get("delivery"), self._window
            if w is None or w.delivery != delivery:
                log.info("the bound of submit %s came after its window closed; it changes "
                         "nothing", delivery)
            elif w.admitted:
                log.info("the bound of submit %s came after its prompt was admitted; the "
                         "window waits for the settle", delivery)
            else:
                self._close(leg.END_FAIL, _opt_str(body, "reason") or "the runner's bound")
        elif kind == leg.R_APPEND_FAILED:
            delivery = body.get("delivery")
            if isinstance(delivery, str):
                ep.refused(delivery, "idle")
        elif kind == leg.R_COMMAND_DONE:
            self._command_done(body)

    def _learn(self, session_id: str | None) -> None:
        if session_id:
            self.endpoint.learn_session(session_id)
            self.session_known.set()

    # -- asks ---------------------------------------------------------------------------

    def _live_answer(self, body: dict[str, Any]) -> None:
        with self._lock:
            slot = self._live.get(body.get("ask"))
        if slot is not None:
            slot[1] = body
            slot[0].set()

    def _context(self, body: dict[str, Any]) -> str | None:
        event = body.get("event")
        session_id = _opt_str(body, "session_id")
        if event == "ready":
            source = _opt_str(body, "source")
            self._learn(session_id)
            if source == "clear":
                self._clear_ran(session_id)
            return self.endpoint.ready(source, session_id)
        if event == "prompt":
            return self.endpoint.prompt()
        raise leg.BadRequest(f"unknown context event {event!r}")

    def _gate_entry(self, body: dict[str, Any]) -> dict[str, Any]:
        """The gate asks for a runner prompt as it enters. Under the lock
        that applies the runner's reports, so the ask and the bound's report are decided
        in the order they arrive. The answer is recorded before it is sent."""
        nonce = body.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            raise leg.BadRequest('needs "nonce"')
        ep = self.endpoint
        with self._lock:
            w = self._window
            if w is None or w.nonce != nonce or w.admitted:
                if w is not None and w.nonce == nonce:
                    what = f"window of {w.delivery} admitted already"
                elif nonce in self._closed:
                    delivery, end = self._closed[nonce]
                    what = f"window of {delivery} closed by {end}; its answer stands"
                else:
                    what = "a nonce this endpoint never minted (an earlier endpoint's?)"
                log.warning("late entry: a runner prompt for a %s; dropped", what)
                return _drop(leg.LATE, None)
            reason = ep.entering(w.delivery)
            if reason is None and not ep.is_waiting(w.delivery):
                reason = "gone"           # the core settled it meanwhile
            if reason is None and w.kind == "start":
                spec = ep._deliveries[w.delivery].spec
                if spec.refuse_draft and body.get("draft") is not False:
                    reason = "draft"      # a draft, or a box the gate could not read
                elif spec.refuse_agents and _busy_agents(body.get("agents")):
                    reason = "busy-agents"
                if reason is not None:
                    ep.refused(w.delivery, reason)
            if reason is not None:
                self._gate_refused.add(w.delivery)
                log.info("entry of %s refused: %s", w.delivery, reason)
                return _drop(reason, w.kind)
            w.admitted = True
            log.info("entry of %s admitted", w.delivery)
        return {"verdict": "pass"}

    def _gate_command(self, body: dict[str, Any]) -> dict[str, Any]:
        request_id = body.get("request")
        ep = self.endpoint
        with self._lock:
            request = self._commands.get(request_id) if isinstance(request_id, str) else None
        if request is None:
            reason = "unknown"
        else:
            reason = ep.command_entering(request.id)
            if reason is None:
                if body.get("draft") is not False:
                    reason = "draft"
                elif _busy_agents(body.get("agents")):
                    reason = "busy-agents"
                if reason is not None:
                    ep.command_settled(request.id, Refused(reason))
        if reason is not None:
            return {"verdict": "drop", "reason": reason,
                    "text": f"moxi-turns-gate: /{request.name if request else '?'} was "
                            f"refused ({reason})"}
        # The decision is recorded before the gate passes it.
        if request.name == "exit":
            ep.command_settled(request.id, Passed())
        else:
            with self._lock:
                self._clear_passed = request.id
        return {"verdict": "pass"}

    def _clear_ran(self, new_session_id: str | None) -> None:
        with self._lock:
            request_id, self._clear_passed = self._clear_passed, None
        if request_id is not None:
            self.endpoint.command_settled(request_id, Ran(new_session_id))

    def _command_done(self, body: dict[str, Any]) -> None:
        request_id, state = body.get("request"), body.get("state")
        with self._lock:
            request = self._commands.pop(request_id, None) if isinstance(request_id,
                                                                        str) else None
        if request is None:
            return
        reason = _opt_str(body, "reason") or state or "unknown"
        ep = self.endpoint
        if state == "ran":
            if request.name == "clear":
                new_id = _opt_str(body, "new_session_id")
                self._learn(new_id)
                with self._lock:
                    if self._clear_passed == request.id:
                        self._clear_passed = None
                ep.command_settled(request.id, Ran(new_id or ep.ledger.session_id))
            else:
                ep.command_settled(request.id, Passed())
        elif state == "refused":
            ep.command_settled(request.id, Refused(reason if reason in COMMAND_REFUSALS
                                                   else "gone"))
        else:
            with self._lock:
                if self._clear_passed == request.id:
                    self._clear_passed = None
            ep.command_settled(request.id, Failed(reason))


def _drop(reason: str, kind: str | None) -> dict[str, Any]:
    """A `/gate/entry` drop: the reason, and the one line the person sees, which the runner
    reads the reason back from."""
    what = {"start": "the start was refused", "deliver": "the delivery was refused"}.get(
        kind or "", "a late prompt was dropped")
    return {"verdict": "drop", "reason": reason, "text": f"moxi-turns-gate: {what} ({reason})"}


def _opt_str(body: dict[str, Any], key: str) -> str | None:
    value = body.get(key)
    return value if isinstance(value, str) and value else None


def _busy_agents(agents: Any) -> bool:
    """A subagent pending, running or waiting, or a count the gate could not read."""
    return isinstance(agents, bool) or not isinstance(agents, int) or agents > 0


def _outcome(body: dict[str, Any]) -> str:
    if body.get("is_aborted") is True:
        return "aborted"
    reason = body.get("reason")
    return reason if reason in OUTCOMES else "error"


def _stop_facts(body: dict[str, Any]) -> StopFacts:
    tasks = body.get("task_types")
    crons = body.get("crons")
    active = body.get("stop_hook_active")
    return StopFacts(
        turn_id=_opt_str(body, "turn_id"), session_id=_opt_str(body, "session_id"),
        stop_hook_active=active if isinstance(active, bool) else None,
        task_types=tuple(t for t in tasks if isinstance(t, str))
        if isinstance(tasks, list) else None,
        crons=crons if isinstance(crons, int) and not isinstance(crons, bool) else None)


# -- the process ------------------------------------------------------------------------


class Life:
    """What ends the endpoint: its SIGTERM, an EPIPE on its stdout, a change of parent."""

    def __init__(self):
        self.ended = threading.Event()
        self.reason: str | None = None
        self._lock = threading.Lock()

    def end(self, reason: str) -> None:
        with self._lock:
            if self.reason is None:
                self.reason = reason
        self.ended.set()

    def wait(self, *events: threading.Event) -> None:
        """Until the end, or one of `events` is set. Wakes often, so a signal's handler
        runs promptly on the main thread."""
        while not self.ended.is_set() and not any(e.is_set() for e in events):
            self.ended.wait(0.2)

    def watch_parent(self, ppid: int) -> threading.Thread:
        thread = threading.Thread(target=self._watch_parent, args=(ppid,), daemon=True,
                                  name="turns-claude-parent")
        thread.start()
        return thread

    def _watch_parent(self, ppid: int) -> None:
        fd = None
        try:
            fd = os.pidfd_open(ppid)
        except (AttributeError, OSError):
            fd = None
        poller = None
        if fd is not None:
            poller = select.poll()
            poller.register(fd, select.POLLIN)
        try:
            while not self.ended.is_set():
                if os.getppid() != ppid:
                    self.end(f"the endpoint's parent {ppid} is gone (now {os.getppid()})")
                    return
                if poller is not None:
                    if poller.poll(int(PARENT_POLL_S * 1000)):
                        self.end(f"the endpoint's parent {ppid} ended")
                        return
                else:
                    time.sleep(PARENT_POLL_S)
        finally:
            if fd is not None:
                os.close(fd)


def main(argv: list[str] | None = None) -> int:
    """`moxi-turns-endpoint`: spawned by the runner, never typed by a person."""
    # The leg owns the real stdout. Anything else that prints (a listener, a library)
    # goes to stderr, so it can never break a line the runner reads.
    leg_out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="moxi-turns-endpoint[%(process)d] %(levelname)s %(message)s")
    life = Life()
    # Only the three ends count: a terminal's interrupt or hangup is not one.
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, lambda *_: life.end("SIGTERM: Claude Code ended the "
                                                      "endpoint"))
    ppid = os.getppid()
    harness = ClaudeHarness(leg_out, pid=ppid, on_end=life.end)

    from moxi.config import ConfigError

    from . import protocol
    from .endpoint import Endpoint

    try:
        ep = Endpoint(harness)
    except ConfigError as exc:
        log.error("turns' config cannot be read; no endpoint: %s", exc)
        leg.LineWriter(leg_out).write({"op": leg.LINE_FAILED, "leg": leg.LEG,
                                       "error": str(exc)})
        return 2
    ep.prepare()
    try:
        harness.serve(str(ep.dir / protocol.MOD_SOCK))
    except OSError as exc:
        log.error("cannot listen on %s: %s", ep.dir / protocol.MOD_SOCK, exc)
        leg.LineWriter(leg_out).write({"op": leg.LINE_FAILED, "leg": leg.LEG,
                                       "error": f"mod.sock: {exc}"})
        ep.stop(f"mod.sock: {exc}")
        return 2
    life.watch_parent(ppid)
    harness.first_line()
    # Callers find the endpoint only once it has its session id.
    life.wait(harness.session_known)
    if not life.ended.is_set():
        try:
            ep.open()
        except OSError as exc:
            life.end(f"cannot open the endpoint socket: {exc}")
    life.wait()
    ep.stop(life.reason or "the endpoint ended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
