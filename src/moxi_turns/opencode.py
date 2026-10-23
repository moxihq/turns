"""Launch an OpenCode session behind a turns endpoint. Experimental, as in comms.

``launch`` owns one `opencode serve` (`opencode_server`) on loopback with a per-launch
password, one OpenCode session it creates, and that server's event stream. The host is
the endpoint: one process, the same socket protocol, `can` = start.

**Posting.** A delivery is posted with `POST /session/:id/prompt_async` under a message
id the host mints, and only into an idle session, read from `GET /session/status`
immediately before each post (a 200 whose JSON has no entry for the session, or an entry
that says idle). OpenCode has one loop per session: a prompt posted while a turn runs is
taken at that turn's next step, and the running turn's own reply is never written. So
`can` holds no `steer`, and the core holds a `deliver` made during a turn until it ends.
That status read is where a start takes effect: the ledger re-check
(`entering`) runs first, then the status read, and a busy session refuses the start
`busy-turn`. A deliver that meets a busy session waits for the next idle and is posted
then. OpenCode has no Stop to hold and no session command.

**The tie.** A turn is one run of the session's loop, from its busy status to its idle.
Its turn id is the id of the user message that opened it (the host's message id for a
delivery's turn), or a host-minted id when the loop started with no new user message.
The delivery that took a turn is the one whose message id comes back as the parent of
an assistant message. Where the turn starts before that reply is seen, the endpoint holds
its events until the tie is decided (`turn_started(…, held=True)`); an idle with no reply
to the posted message releases the turn as the person's and the delivery reads
`Lost("untied: …")`.

**Outcomes.** An idle ends the turn. Its outcome comes from the turn's last assistant
message: `aborted` for `MessageAbortedError` (Esc in a window), `error` for any other
error or a `session.error` during the turn, `error` when the turn wrote no assistant
message or its `finish` is `error`, `refusal` when its `finish` is `content-filter`, and
`answer` otherwise. A server that dies ends its open turn `error`.

**Supervision.** A server that exits is started again on the same config and the same
session, a cut or silent stream is opened again, and a post that had no answer, or
that a dead server took with no user message seen, is reconciled against the session's
messages, each on comms' backoff (0.5 s doubling to
30 s, 20 % jitter, reset by a success). While no stream is open and reconciled, a
handed delivery waits in the host and goes in once it is; the core's `PENDING_TTL_S`
bounds the wait. A post the server refuses in a way a retry cannot fix (a 4xx) refuses
that delivery `gone` and ends the endpoint with the server's answer.

**Options**. `options` is OpenCode's config object. The host lays it, unread,
into the server's `OPENCODE_CONFIG_CONTENT` at every server start, a restart's included.
turns owns `instructions`: the host writes the `instructions` text to a file in the
endpoint's directory and names it there, so a mapping that names `instructions` is
refused with `ValueError` before anything starts, and so is one turns cannot serialize.
The environment variables the host sets on the server (`OPENCODE_SERVER_PASSWORD`,
`OPENCODE_CONFIG_CONTENT`) are turns' too: `env` may not name them, and the inherited
environment's copies are dropped.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import logging
import os
import random
import secrets
import shutil
import string
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from . import client as turns_client
from . import protocol
from .doctor import Row
from .endpoint import EXPIRED, Endpoint
from .harness import CommandRequest, Delivery, Harness, Live
from .opencode_server import EventStream, Http, NoAnswer, Server, Unreachable
from .results import Failed

log = logging.getLogger("moxi.turns.opencode")

#: The host is experimental, as comms' OpenCode launcher is.
EXPERIMENTAL = True
BINARY = "opencode"
#: Where OpenCode's installer puts the binary when it is not on PATH.
INSTALL_DIR = Path("~/.opencode/bin")
#: The top-level config keys turns owns.
OWNED_OPTIONS = frozenset({"instructions"})
#: The server environment variables turns owns.
OWNED_ENV = frozenset({"OPENCODE_SERVER_PASSWORD", "OPENCODE_CONFIG_CONTENT"})
#: The file in the endpoint's directory that carries `instructions`.
INSTRUCTIONS_FILE = protocol.INSTRUCTIONS
#: The `error.name` of an assistant message whose turn was aborted (a window's Esc).
ABORTED = "MessageAbortedError"
#: Seconds before a post the server answered with a 5xx or 429 is tried again.
FLUSH_RETRY = 1.0
#: The supervisor's poll; it notices a lost server or stream, it does not pace retries.
SUPERVISE_POLL = 0.5
RECONNECT_WAIT = 0.5
BACKOFF_MAX = 30.0
BACKOFF_FACTOR = 2.0
BACKOFF_JITTER = 0.2
#: The longest a waiting delivery sleeps before it looks at the host again.
WAIT_STEP = 1.0
#: What a refusal a retry cannot fix means, by status (comms' wording).
MEANINGS = {400: "a request the server refuses", 401: "the server password did not match",
            403: "forbidden", 404: "this launch's session is gone"}


class OpenCodeOffline(OSError):
    """The server, or the session, could not be reached."""


def backoff_delays(rng=random):
    delay = RECONNECT_WAIT
    while True:
        yield min(delay, BACKOFF_MAX) * (1 + rng.uniform(-BACKOFF_JITTER, BACKOFF_JITTER))
        delay = min(delay * BACKOFF_FACTOR, BACKOFF_MAX)


def mint_message_id(n: int = 1) -> str:
    """A message id in OpenCode's own `msg_` format: 12 hex digits of
    `milliseconds * 4096 + n`, then 14 base62 characters. The server keeps it."""
    ms = int(time.time() * 1000)
    tail = "".join(random.choice(string.ascii_letters + string.digits) for _ in range(14))
    return f"msg_{(ms * 0x1000 + n) & 0xFFFFFFFFFFFF:012x}{tail}"


def idle_from_status(status: int, body, session_id: str) -> bool:
    """Whether a `GET /session/status` answer says the session is idle. Only a 200 whose
    body is a JSON object with no entry for the session, or an entry of type idle, does;
    an error, an empty or malformed body, or any other entry is not idle."""
    if status != 200 or not isinstance(body, dict):
        return False
    entry = body.get(session_id)
    if entry is None:
        return True
    return isinstance(entry, dict) and entry.get("type") == "idle"


def launch_config(options: Mapping, instructions_path: Path | None) -> dict:
    """What `OPENCODE_CONFIG_CONTENT` carries: the caller's `options` unchanged, with
    turns' `instructions` (the file's absolute path) when there is one."""
    config = dict(options)
    if instructions_path is not None:
        config["instructions"] = [str(instructions_path)]
    return config


def outcome_of(last: dict, errored: bool) -> str:
    """A turn's outcome from its last assistant message and whether the session reported
    an error during it."""
    error = last.get("error")
    if isinstance(error, dict) and error.get("name") == ABORTED:
        return "aborted"
    if errored or error or not last or last.get("finish") == "error":
        return "error"
    if last.get("finish") == "content-filter":
        return "refusal"
    return "answer"


def resolve_binary(path: str | None = None) -> str | None:
    """`opencode` on PATH, else the installer's directory, as an absolute path."""
    found = shutil.which(BINARY, path=path)
    if found:
        return str(Path(found).absolute())
    installed = INSTALL_DIR.expanduser() / BINARY
    if installed.is_file() and os.access(installed, os.X_OK):
        return str(installed)
    return None


@dataclass
class _Post:
    """The delivery in flight: posted, or about to be, under `mid`."""

    delivery: Delivery
    mid: str
    admitted: bool = False      # its user message was seen
    uncertain: bool = False     # the post had no answer; the reconcile decides


class OpenCodeHarness(Harness):
    name = "opencode"
    can = frozenset({"start"})
    mod_version = None

    def __init__(self, cwd, *, instructions: str | None = None, env=None, options=None,
                 binary: str | None = None, server_factory=None, backoff_factory=None,
                 silence: float | None = None):
        if options is not None and not isinstance(options, Mapping):
            raise TypeError("options must be a mapping")
        options = dict(options or {})
        owned = OWNED_OPTIONS.intersection(options)
        if owned:
            raise ValueError(f"OpenCode options name keys turns owns: "
                             f"{', '.join(sorted(owned))}")
        try:
            json.dumps(options)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"OpenCode options are not a JSON object: {exc}") from exc
        caller_env = {str(k): str(v) for k, v in (env or {}).items()}
        owned_env = OWNED_ENV.intersection(caller_env)
        if owned_env:
            raise ValueError(f"env names variables the OpenCode host sets: "
                             f"{', '.join(sorted(owned_env))}")
        self.cwd = str(Path(cwd).resolve())
        self.instructions = instructions
        self.options = options
        self.env = {k: v for k, v in os.environ.items() if k not in OWNED_ENV}
        self.env.update(caller_env)
        self.binary = binary
        self.password = secrets.token_urlsafe(24)
        self.instructions_path: Path | None = None
        self.pid = os.getpid()
        self._server_factory = server_factory or Server
        self._backoff_factory = backoff_factory or backoff_delays
        self._silence = silence

        self.server: Server | None = None
        self.http: Http | None = None
        self.stream: EventStream | None = None
        self.session_id: str | None = None
        self.trouble: str | None = None

        self._lock = threading.RLock()
        self._changed = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._supervisor: threading.Thread | None = None
        self._delays = None
        self._ready = False             # stream open and reconciled
        self._reconcile_wanted = False
        self._busy = False              # the session's own status says it runs
        self._turn_id: str | None = None   # the turn the ledger holds open
        self._held = False              # its tie is not decided yet
        self._errored = False           # `session.error` during the open turn
        self._last: dict = {}           # the open turn's latest assistant message
        self._last_user: str | None = None  # the latest user message not yet a turn's
        self._seen_users: set[str] = set()  # OpenCode sends a user message again in its turn
        self._turns = 0
        self._post: _Post | None = None

    # -- life ----------------------------------------------------------------------------

    def attach(self, endpoint) -> None:
        super().attach(endpoint)
        if self.instructions:
            self.instructions_path = endpoint.dir / INSTRUCTIONS_FILE

    def server_env(self) -> dict:
        env = dict(self.env)
        env["OPENCODE_SERVER_PASSWORD"] = self.password
        env["OPENCODE_CONFIG_CONTENT"] = json.dumps(
            launch_config(self.options, self.instructions_path))
        return env

    def start(self) -> None:
        """Server up, the session created, the stream open and reconciled, `Ready`. Any
        failure here raises with its reason, and the launch fails."""
        if self.instructions_path is not None:
            fd = os.open(self.instructions_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                         0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(str(self.instructions))
        self._bring_up()
        self._create_session()
        self._connect_stream()
        self._ready_event("startup", self.session_id)
        self._supervisor = threading.Thread(target=self._supervise, daemon=True,
                                            name="turns-opencode-supervisor")
        self._supervisor.start()

    def _bring_up(self) -> None:
        binary = self.binary or resolve_binary(self.env.get("PATH"))
        if binary is None:
            raise OpenCodeOffline("no opencode binary resolves (`opencode` on PATH, or "
                                  f"{INSTALL_DIR}/opencode)")
        if self.server is not None:
            self.server.stop()
        server = self._server_factory(binary, env=self.server_env(), cwd=self.cwd,
                                      on_line=_log_line)
        self.server = server.start()
        url = server.wait_listening()
        if not url:
            raise OpenCodeOffline(f"the opencode server never said where it listens "
                                  f"(exit {server.returncode})")
        self.http = Http(url, self.password)

    def _create_session(self) -> None:
        try:
            status, body = self.http.json("POST", "/session", {})
        except (Unreachable, NoAnswer) as e:
            raise OpenCodeOffline(f"creating the session: {e}") from e
        if status != 200 or not isinstance(body, dict) or not body.get("id"):
            raise OpenCodeOffline(f"creating the session answered {status}")
        with self._lock:
            self.session_id = body["id"]
        log.info("started OpenCode session %s", self.session_id)

    def _connect_stream(self) -> None:
        with self._lock:
            self._ready = False
        old, self.stream = self.stream, None
        if old is not None:
            old.close("replaced")
        kw = {} if self._silence is None else {"silence": self._silence}
        stream = EventStream(self.http, on_event=self.on_event,
                             on_close=self._on_stream_close, **kw)
        try:
            stream.start()
        except (Unreachable, NoAnswer) as e:
            raise OpenCodeOffline(f"the event stream did not open: {e}") from e
        self.stream = stream
        self.reconcile()

    def _on_stream_close(self, reason: str) -> None:
        with self._lock:
            self._ready = False
            self._changed.notify_all()
        if not self._stop.is_set():
            log.warning("the OpenCode event stream closed: %s", reason)

    def close(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        with self._lock:
            self._ready = False
            self._changed.notify_all()
        if self.stream is not None:
            self.stream.close()
        if self.server is not None:
            self.server.stop()
        if self.instructions_path is not None:
            with contextlib.suppress(OSError):
                self.instructions_path.unlink()
        with contextlib.suppress(OSError, AttributeError):
            self.endpoint.dir.rmdir()
        if (self._supervisor is not None
                and self._supervisor is not threading.current_thread()):
            self._supervisor.join(timeout=2.0)

    # -- supervision ---------------------------------------------------------------------

    def _supervise(self) -> None:
        while not self._stop.wait(SUPERVISE_POLL):
            try:
                if self.server is not None and not self.server.alive:
                    self._server_lost()
                    self._bring_up()
                    self._connect_stream()
                elif self.stream is None or self.stream.closed.is_set():
                    self._connect_stream()
                elif self._reconcile_wanted:
                    self.reconcile()
                self._delays = None
                self.trouble = None
            except (OpenCodeOffline, Unreachable, NoAnswer) as e:
                if self._stop.is_set():
                    return
                self.trouble = str(e)
                log.warning("the OpenCode server is not reachable: %s", e)
                if self._delays is None:
                    self._delays = self._backoff_factory()
                self._stop.wait(next(self._delays))
            except Exception:                                   # noqa: BLE001
                log.exception("the OpenCode supervisor failed a round")
                self._stop.wait(SUPERVISE_POLL)

    def _server_lost(self) -> None:
        """The server exited: its open turn ends `error`. A posted delivery the stream
        saw admitted reads `Lost`; the reconcile against the new server decides the rest."""
        code = self.server.returncode if self.server is not None else None
        log.warning("the opencode server exited (%s); starting it again", code)
        with self._lock:
            self._ready = False
            self._busy = False
            self._end_turn_locked("error")
            post = self._post
            if post is not None and not post.admitted:
                # Not seen on the stream: the reconcile reads the new server's messages
                # and posts it again if it never went in.
                post.uncertain = True
            elif post is not None:
                self._post = None
                self.endpoint.lost(post.delivery.id,
                                   f"untied: the opencode server exited ({code})")
            self._changed.notify_all()

    # -- reconcile -----------------------------------------------------------------------

    def reconcile(self) -> None:
        """After the stream (re)opens, or a post had no answer: read the session's status
        and its messages, settle what the stream missed, then let posts go in."""
        sid = self.session_id
        try:
            status, body = self.http.json("GET", "/session/status")
            mstatus, messages = self.http.json("GET", f"/session/{sid}/message")
        except (Unreachable, NoAnswer) as e:
            raise OpenCodeOffline(f"reconciling: {e}") from e
        if status != 200 or not isinstance(body, dict):
            raise OpenCodeOffline(f"the session status read answered {status}")
        if mstatus == 404:
            self._fatal(404, "this launch's session is no longer on the server")
            return
        if mstatus != 200 or not isinstance(messages, list):
            raise OpenCodeOffline(f"reading the session's messages answered {mstatus}")
        idle = idle_from_status(status, body, sid)
        infos = [((m or {}).get("info") or {}) for m in messages]
        by_id = {i.get("id"): i for i in infos}
        last = next((i for i in reversed(infos) if i.get("role") == "assistant"), {})
        retry = None
        with self._lock:
            self._reconcile_wanted = False
            post = self._post
            replied = None
            if post is not None:
                if post.mid in by_id:
                    post.admitted, post.uncertain = True, False
                    self._seen_users.add(post.mid)
                    replied = next((i for i in infos if i.get("role") == "assistant"
                                    and i.get("parentID") == post.mid), None)
                elif post.uncertain:
                    # Never admitted: it never ran, so it goes in again.
                    self._post = None
                    retry, post = post.delivery, None
            if last and self._turn_id is not None:
                self._last = _brief(last)
            if not idle and not self._busy:
                self._open_turn_locked()
            if post is not None and replied is not None:
                if self._turn_id is None:
                    # Its whole turn ran while the stream was cut.
                    self._post = None
                    self._turn_id, self._turns = post.mid, self._turns + 1
                    self._seen_users.add(post.mid)
                    self.endpoint.turn_started(post.mid, post.delivery.id)
                    self._last = _brief(last)
                elif self._held:
                    self._tie_locked(post)
            if idle:
                self._busy = False
                self._end_turn_locked(outcome_of(self._last, self._errored))
                post = self._post
                if post is not None and post.admitted:
                    self._post = None
                    self.endpoint.lost(post.delivery.id, "untied: the session is idle "
                                       "and no reply to the posted message was seen")
            self._ready = True
            self._changed.notify_all()
        if retry is not None:
            self.submit(retry)

    # -- events --------------------------------------------------------------------------

    def on_event(self, event: dict) -> None:
        kind = event.get("type")
        props = event.get("properties") or {}
        compacted = False
        with self._lock:
            sid = self.session_id
            if kind == "message.updated":
                info = props.get("info") or {}
                if info.get("sessionID") == sid:
                    self._on_message(info)
            elif kind == "session.status" and props.get("sessionID") == sid:
                if (props.get("status") or {}).get("type") == "idle":
                    self._on_idle()
                elif not self._busy:
                    self._open_turn_locked()
            elif kind == "session.idle" and props.get("sessionID") == sid:
                self._on_idle()
            elif kind == "session.error" and props.get("sessionID") == sid:
                if self._turn_id is not None:
                    self._errored = True
            elif kind == "session.compacted" and props.get("sessionID") == sid:
                compacted = True
            self._changed.notify_all()
        if compacted:
            # Manual or automatic: `Ready(compact)`. An automatic compaction runs inside
            # the busy loop, so the open turn stays open.
            self._ready_event("compact", sid)

    def _ready_event(self, source: str, sid: str | None) -> None:
        """`Ready`. OpenCode has no channel for the listeners' added context, so the
        host drops a non-empty answer and logs that it did."""
        context = self.endpoint.ready(source, sid)
        if context:
            log.info("the listeners' context at %s (%d characters) was dropped: the "
                     "OpenCode host has no channel for it", source, len(context))

    def _on_message(self, info: dict) -> None:
        mid = info.get("id")
        post = self._post
        if info.get("role") == "user":
            if mid is not None and mid not in self._seen_users:
                self._seen_users.add(mid)
                self._last_user = mid
            if post is not None and mid == post.mid:
                post.admitted = True
            return
        if info.get("role") != "assistant":
            return
        if self._turn_id is None:
            if (info.get("time") or {}).get("completed"):
                # A finished reply's late update (the server sends the last one twice):
                # its turn has ended, and it opens none.
                return
            # A reply being written is a running loop, whether or not its busy status
            # came first.
            self._open_turn_locked()
        if not self._last or str(mid) >= str(self._last.get("id") or ""):
            self._last = _brief(info)
        if post is not None and info.get("parentID") == post.mid:
            post.admitted = True
            if self._held:
                self._tie_locked(post)

    def _on_idle(self) -> None:
        """The session's loop ended. The server publishes both `session.status` idle and
        `session.idle`; the second finds no open turn and does nothing."""
        self._busy = False
        if self._turn_id is None:
            return
        self._end_turn_locked(outcome_of(self._last, self._errored))
        post = self._post
        if post is not None and post.admitted:
            self._post = None
            self.endpoint.lost(post.delivery.id, "untied: the session went idle with no "
                               "reply to the posted message")

    def _open_turn_locked(self) -> None:
        self._busy = True
        if self._turn_id is not None:
            return
        self._turns += 1
        turn_id = self._last_user or f"turn-{self.endpoint.nonce}-{self._turns}"
        self._last_user = None
        self._turn_id, self._errored, self._last = turn_id, False, {}
        post = self._post
        if post is None:
            self._held = False
            self.endpoint.turn_started(turn_id, None)
        else:
            # Which delivery took it is known only once a reply names its parent.
            self._held = True
            self.endpoint.turn_started(turn_id, held=True)

    def _tie_locked(self, post: _Post) -> None:
        self._held = False
        self._post = None
        self.endpoint.turn_tied(self._turn_id, post.delivery.id)

    def _end_turn_locked(self, outcome: str) -> None:
        turn_id = self._turn_id
        if turn_id is None:
            return
        if self._held:
            self._held = False
            self.endpoint.turn_released(turn_id)
        self._turn_id = None
        self.endpoint.turn_ended(turn_id, outcome)

    # -- the core's calls ----------------------------------------------------------------

    def live(self) -> Live:
        """Subagents are the session's child sessions that are running. The host has no
        window and no prompt box, so `draft` is False."""
        sid, http = self.session_id, self.http
        if not sid or http is None:
            return Live(None, False)
        try:
            cstatus, children = http.json("GET", f"/session/{sid}/children", timeout=2.0)
            sstatus, status = http.json("GET", "/session/status", timeout=2.0)
        except (Unreachable, NoAnswer):
            return Live(None, False)
        if cstatus != 200 or sstatus != 200 or not isinstance(children, list) \
                or not isinstance(status, dict):
            return Live(None, False)
        running = sum(1 for c in children if isinstance(c, dict)
                      and not idle_from_status(200, status, c.get("id")))
        return Live(running, False)

    def submit(self, delivery: Delivery) -> None:
        threading.Thread(target=self._submit, args=(delivery,), daemon=True,
                         name="turns-opencode-submit").start()

    def append(self, delivery: Delivery) -> None:
        # `can` holds no steer, so the core never calls this; refuse if it does.
        self.endpoint.refused(delivery.id, "unsupported")

    def command(self, request: CommandRequest) -> None:
        self.endpoint.command_settled(request.id, Failed("unsupported"))

    def expired(self, delivery_id: str) -> None:
        with self._lock:
            if self._post is not None and self._post.delivery.id == delivery_id:
                self._post = None
            self._changed.notify_all()

    def _wait(self) -> None:
        with self._lock:
            self._changed.wait(WAIT_STEP)

    def _submit(self, d: Delivery) -> None:
        """Post `d` once the host is ready and the session idle; settle it on every
        road (the adapter's duty, `harness.py`)."""
        while True:
            if self._stop.is_set():
                self.endpoint.refused(d.id, "gone")
                return
            if not self.endpoint.is_waiting(d.id):
                return                  # expired or settled elsewhere
            with self._lock:
                ready = self._ready and self.http is not None
            if not ready:
                self._wait()
                continue
            reason = self.endpoint.entering(d.id)
            if reason is None and (d.refuse_agents or d.refuse_draft):
                live = self.live()
                if d.refuse_agents and (live.agents is None or live.agents):
                    reason = "busy-agents"
                elif d.refuse_draft and live.draft:
                    reason = "draft"
            if reason is not None:
                if reason != EXPIRED:
                    self.endpoint.refused(d.id, reason)
                return
            outcome = self._post_once(d)
            if outcome == "posted":
                return
            if outcome == "busy" and d.kind == "start":
                self.endpoint.refused(d.id, "busy-turn")
                return
            if outcome == "retry":
                self._stop.wait(FLUSH_RETRY)
            else:
                self._wait()

    def _post_once(self, d: Delivery) -> str:
        """One status read and post: "posted" (the stream or the reconcile settles it),
        "busy", "offline" or "retry" (nothing was admitted), or "refused" (settled)."""
        sid, http = self.session_id, self.http
        try:
            status, body = http.json("GET", "/session/status")
        except (Unreachable, NoAnswer) as e:
            log.warning("the session's status could not be read before a post: %s", e)
            return "offline"
        if not idle_from_status(status, body, sid):
            return "busy"
        mid = mint_message_id()
        post = _Post(d, mid)
        with self._lock:
            if self._turn_id is not None or self._busy:
                return "busy"
            self._post = post
        try:
            code, answer = http.request("POST", f"/session/{sid}/prompt_async",
                                        {"messageID": mid,
                                         "parts": [{"type": "text", "text": d.text}]})
        except Unreachable as e:
            self._unpost(post)
            log.warning("a post found no server: %s", e)
            return "offline"
        except NoAnswer as e:
            with self._lock:
                post.uncertain = True
                self._reconcile_wanted = True
            log.warning("post %s had no answer (%s); the session's messages will say "
                        "whether it was admitted", mid, e)
            return "posted"
        if 200 <= code < 300:
            return "posted"
        self._unpost(post)
        if code >= 500 or code == 429:
            log.warning("post %s answered %s; trying again", mid, code)
            return "retry"
        self.endpoint.refused(d.id, "gone")
        self._fatal(code, answer)
        return "refused"

    def _unpost(self, post: _Post) -> None:
        with self._lock:
            if self._post is post:
                self._post = None

    def _fatal(self, code: int, detail) -> None:
        """A refusal a retry cannot fix: the endpoint ends with the server's answer."""
        meaning = MEANINGS.get(code, "a refusal a retry cannot fix")
        detail_text = str(detail or "").strip()[:300] or "(no body)"
        reason = (f"the OpenCode server refused with {code} ({meaning}): {detail_text}")
        self.trouble = reason
        log.error("%s", reason)
        threading.Thread(target=self.endpoint.gone, args=(reason,), daemon=True,
                         name="turns-opencode-gone").start()


def _brief(info: dict) -> dict:
    return {"id": info.get("id"), "finish": info.get("finish"), "error": info.get("error")}


def _log_line(line: str) -> None:
    log.debug("server: %s", line.rstrip())


_HOSTS: list[tuple[Endpoint, OpenCodeHarness]] = []
_HOSTS_LOCK = threading.Lock()


def launch(cwd, *, instructions: str | None = None, env=None, options=None):
    """Launch a hosted OpenCode session (experimental) and return its live turns
    ``Session``. Returns once the server listens, the session exists and its event
    stream is reconciled; raises with the reason when any of that fails."""
    path = Path(cwd).resolve()
    if not path.is_dir():
        raise FileNotFoundError(f"OpenCode working directory does not exist: {path}")
    harness = OpenCodeHarness(path, instructions=instructions, env=env, options=options)
    return _run(harness)


def _run(harness: OpenCodeHarness):
    endpoint = Endpoint(harness, env=harness.env).open()
    try:
        harness.start()
        session = turns_client.find(session_id=harness.session_id)
        if session is None:
            raise RuntimeError("the OpenCode endpoint opened but could not be found")
    except BaseException:
        endpoint.stop("the OpenCode launch failed")
        raise
    with _HOSTS_LOCK:
        _HOSTS.append((endpoint, harness))
    return session


def rows() -> list[Row]:
    binary = resolve_binary()
    return [Row("OpenCode binary (experimental host)", binary is not None,
                binary or "opencode is not on PATH or in the installer's directory")]


def _close_hosts() -> None:
    with _HOSTS_LOCK:
        hosts = list(_HOSTS)
        _HOSTS.clear()
    for endpoint, _ in hosts:
        endpoint.stop("the launching process ended")


atexit.register(_close_hosts)

__all__ = ["launch"]
