"""The Claude endpoint's leg to its own runner: turns' internal protocol, not the contract.

The runner (the `moxi-turns` plugin) spawns the endpoint at `session.start` with
`$.process.spawn`. The two talk over two channels:

- **Endpoint to runner: JSON lines on the endpoint's stdout**, one object per line, each
  with an `"op"`. The child's stdin is a fixed string, so nothing comes back that way.
- **Runner (and gate) to endpoint: HTTP POSTs with JSON bodies to `mod.sock`**, the
  second socket in the endpoint's directory, because `$.http.fetch` speaks HTTP. Every
  answer is `200` with a JSON object, or `4xx` with `{"ok": false, "error": …}`.

The runner and the endpoint ship in one package and `hello.json` names both versions, so
this leg changes only with the package. `LEG` is its version; the endpoint's first line
names it and the runner sends nothing to an endpoint whose leg it does not speak.

Lines (endpoint to runner)
--------------------------

The first line, always first, before the runner sends anything:

    {"op": "hello", "leg": 1, "mod_sock": "<absolute path>", "hold_wait_ms": n,
     "listen_wait_ms": n, "endpoint_version": "…", "pid": <endpoint pid>}

or, when the endpoint cannot start (its config is broken), its only line:

    {"op": "failed", "leg": 1, "error": "<words>"}

Then, in any order:

    {"op": "submit", "delivery": id, "kind": "start" | "deliver", "text": "…",
     "nonce": "<the window nonce>"}
        `$.prompt.submit` the text, framed under the plugin's name, with the line
        `NONCE_LINE + nonce` first (entry, below). The window nonce is random, 128 bits,
        minted per window, so no other window of this endpoint or any earlier one names
        it. The endpoint sends the next `submit` only once this one's window has closed.
    {"op": "append", "delivery": id, "text": "…"}
        `$.session.append` a user-role row into the running turn (a steer).
    {"op": "command", "request": id, "name": "clear" | "exit"}
        `$.command.run` the command; the gate re-checks it (`/gate/command`).
    {"op": "live", "ask": n}
        Read `$.agent.list()` and `$.prompt.read()` and answer on `/live`.

Requests (runner and gate to endpoint, `POST` on `mod.sock`)
------------------------------------------------------------

`/report` — one report from the runner's ordered queue. Each carries `seq`, the queue's
own count from 1 for this load, and `kind`:

    session        {session_id, mod_version}       the runner's `$.session.id()`
    turn.start     {turn_id}                        a main-loop `turn.start`
    turn.complete  {turn_id, reason, is_aborted}    a main-loop `turn.complete`
    submit.close   {delivery, end, reason}          a submit settled: `end` is `resolve`,
                                                    `drop` or `fail` (it rejected); a
                                                    drop's `reason` is the gate's code read
                                                    from its line, else `dropped`. Sent
                                                    whenever it settles, after a bound's
                                                    report too
    submit.bound   {delivery, reason}               the runner's bound: still unsettled
                                                    after 5 s with no main-loop turn open
    append.failed  {delivery, reason}               a steer the runner could not append
    command.done   {request, state, reason, new_session_id}
                                                    `$.command.run` settled: `state` is
                                                    `ran`, `refused` or `failed`

Reports are keyed by their window's delivery: an end for a window that is not the open
one is logged and changes nothing. The bound is a report, not a close: the endpoint fails
the open window by it only while its prompt is not admitted (entry, below).

A report is applied once, in `seq` order. One whose `seq` the endpoint has already
applied is answered `ok` and changes nothing, so the runner may send a report again when
its answer missed the runner's bound. A `seq` past the next one expected means a report
was lost: the endpoint fails the open submit window (its delivery reads `Lost`, reason
`untied: …`) rather than tie a delivery from a partial record, then applies it. Answer:
`{"ok": true}`.

`/live` — `{ask, agents, draft}` (or `{ask, error}`), the answer to a `live` line.
`agents` is null when `$.agent.list()` could not be read: unknown, which counts as busy
and never as 0. An unreadable box is sent as `draft: true`. Answer `{"ok": true}`.

`/ask/stop` — `{turn_id, session_id, stop_hook_active, task_types, crons}` at a main-loop
`classic.Stop`. Answer `{"ok": true, "reason": "<text>" | null}`: a reason holds the turn
open (`block`), null lets it end. The endpoint answers within `hold.wait_ms`.

`/ask/context` — `{event: "ready" | "prompt", source, session_id}` at
`classic.SessionStart` (`ready`) and `classic.UserPromptSubmit` (`prompt`). Answer
`{"ok": true, "context": "<text>" | null}`, the event's `additionalContext`. The endpoint
answers within `listen.wait_ms`.

`/gate/entry` — `{nonce, draft, agents}` from the gate, for every runner prompt as it
enters. `nonce` is the window nonce the gate read off the prompt's first
line; `draft` (bool) and `agents` (int, or true when the list could not be read) are the
gate's readings. The endpoint admits the prompt only if the nonce names the window now
open and that window has admitted nothing yet; for a start it then re-checks the ledger
and the refusals the start asked for, with the readings. It records its answer before it
sends it. Answer `{"ok": true, "verdict": "pass"}` (the gate takes the nonce line off and
calls `next`) or `{"ok": true, "verdict": "drop", "reason": r, "text": "<one line for the
person>"}`; the gate drops a `drop` with `text` and never calls `next`. A nonce that names
no open window reads `late`, and the endpoint logs the late entry.

`/gate/command` — `{request, agents, draft}` from the gate, for a command its runner
queued, as it comes off the queue. The endpoint re-checks it and records its decision
before it answers, as `/gate/entry` does.
"""

from __future__ import annotations

import http.server
import json
import logging
import socketserver
import threading
from collections.abc import Callable
from typing import Any, BinaryIO

log = logging.getLogger("moxi.turns.claude")

#: This leg's version.
LEG = 1

#: The endpoint's line ops.
LINE_HELLO, LINE_FAILED = "hello", "failed"
LINE_SUBMIT, LINE_APPEND, LINE_COMMAND, LINE_LIVE = "submit", "append", "command", "live"

#: The request paths on `mod.sock`.
PATH_REPORT = "/report"
PATH_LIVE = "/live"
PATH_STOP = "/ask/stop"
PATH_CONTEXT = "/ask/context"
PATH_GATE_ENTRY = "/gate/entry"
PATH_GATE_COMMAND = "/gate/command"
PATHS = (PATH_REPORT, PATH_LIVE, PATH_STOP, PATH_CONTEXT, PATH_GATE_ENTRY,
         PATH_GATE_COMMAND)

#: The report kinds.
R_SESSION, R_TURN_START, R_TURN_COMPLETE = "session", "turn.start", "turn.complete"
R_SUBMIT_CLOSE, R_SUBMIT_BOUND = "submit.close", "submit.bound"
R_APPEND_FAILED, R_COMMAND_DONE = "append.failed", "command.done"
REPORTS = (R_SESSION, R_TURN_START, R_TURN_COMPLETE, R_SUBMIT_CLOSE, R_SUBMIT_BOUND,
           R_APPEND_FAILED, R_COMMAND_DONE)

#: A submit window's three ends.
END_RESOLVE, END_DROP, END_FAIL = "resolve", "drop", "fail"
ENDS = (END_RESOLVE, END_DROP, END_FAIL)

#: The refusal codes the plugins decide on their own, with no word from the endpoint: the
#: gate's `gone` (no answer within its 1 s, no `mod.sock` path, a fault in its hook) and
#: the runner's `unsupported` (the gate is not loaded, so nothing was submitted). A drop
#: whose reason is one of these reads `Refused` with it; any other drop the endpoint did
#: not record reads `dropped`, another plugin's.
GATE_CODES = ("gone", "unsupported")

#: The first line of every runner prompt is this prefix and the window nonce; the gate
#: takes the line off as it admits the prompt, so the model never reads it.
NONCE_LINE = "moxi-turns-window: "
#: What the endpoint answers for a runner prompt whose nonce names no open window: a
#: closed window's, an earlier endpoint's, or one already admitted.
LATE = "late"

#: The longest request body the endpoint reads.
MAX_BODY = 4 * 1024 * 1024


class BadRequest(Exception):
    """A request the endpoint cannot read; answered 400."""


class LineWriter:
    """The endpoint's half of the leg: one JSON object per line on its private stdout.

    A write that fails (EPIPE: the runner is gone) calls `on_broken` once and every later
    write is dropped, since nobody reads them.
    """

    def __init__(self, out: BinaryIO, on_broken: Callable[[str], None] | None = None):
        self._out = out
        self._lock = threading.Lock()
        self._broken = False
        self._on_broken = on_broken

    @property
    def broken(self) -> bool:
        return self._broken

    def write(self, obj: dict[str, Any]) -> bool:
        data = (json.dumps(obj, separators=(",", ":")) + "\n").encode()
        with self._lock:
            if self._broken:
                return False
            try:
                self._out.write(data)
                self._out.flush()
                return True
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._broken = True
                reason = f"EPIPE on the runner's line: {exc}"
        log.info("%s", reason)
        if self._on_broken is not None:
            self._on_broken(reason)
        return False


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    allow_reuse_address = False


class LegServer:
    """The HTTP server on `mod.sock`. `handle(path, body)` answers each request with a
    dict (sent as `200`), or raises `BadRequest` (`400`)."""

    def __init__(self, path: str, handle: Callable[[str, dict[str, Any]], dict[str, Any]]):
        self.path = path
        handler = _handler_for(handle)
        self._server = _Server(path, handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.2}, daemon=True,
                                        name="turns-claude-leg")

    def start(self) -> "LegServer":
        self._thread.start()
        return self

    def close(self) -> None:
        try:
            self._server.shutdown()
        finally:
            self._server.server_close()


def _handler_for(handle):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = "moxi-turns-endpoint"

        def address_string(self) -> str:
            return "runner"

        def log_message(self, fmt, *args) -> None:  # noqa: D401 - quiet by design
            log.debug("leg: " + fmt, *args)

        def do_POST(self) -> None:  # noqa: N802 - http.server's name
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > MAX_BODY:
                    raise BadRequest(f"a body of {length} bytes")
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except (ValueError, UnicodeDecodeError) as exc:
                    raise BadRequest(f"a body that is not JSON: {exc}") from None
                if not isinstance(body, dict):
                    raise BadRequest("a body that is not a JSON object")
                if self.path not in PATHS:
                    self._send(404, {"ok": False, "error": f"no such path {self.path!r}"})
                    return
                answer = handle(self.path, body)
                self._send(200, {"ok": True, **answer})
            except BadRequest as exc:
                self._send(400, {"ok": False, "error": str(exc)})
            except Exception as exc:  # noqa: BLE001 - one bad request never ends the leg
                log.exception("leg request %s failed", self.path)
                self._send(500, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})

        def do_GET(self) -> None:  # noqa: N802
            self._send(405, {"ok": False, "error": "POST only"})

        def _send(self, status: int, obj: dict[str, Any]) -> None:
            data = json.dumps(obj, separators=(",", ":")).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                pass              # the runner gave up on this request (its own bound)

    return Handler


__all__ = ["LEG", "LINE_HELLO", "LINE_FAILED", "LINE_SUBMIT", "LINE_APPEND", "LINE_COMMAND",
           "LINE_LIVE", "PATH_REPORT", "PATH_LIVE", "PATH_STOP", "PATH_CONTEXT",
           "PATH_GATE_ENTRY", "PATH_GATE_COMMAND", "PATHS", "R_SESSION", "R_TURN_START",
           "R_TURN_COMPLETE", "R_SUBMIT_CLOSE", "R_SUBMIT_BOUND", "R_APPEND_FAILED",
           "R_COMMAND_DONE", "REPORTS", "END_RESOLVE", "END_DROP", "END_FAIL", "ENDS",
           "GATE_CODES", "NONCE_LINE", "LATE", "MAX_BODY",
           "BadRequest", "LineWriter", "LegServer"]
