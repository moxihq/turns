"""The caller's side: finding a session's endpoint and speaking the socket protocol to it.

A caller names a session by what it already knows (the harness session id, or a
process id), and turns resolves that to a live endpoint by connecting to it, not by
trusting a file. An endpoint directory whose socket refuses is swept.
"""

from __future__ import annotations

import contextlib
import itertools
import json
import logging
import os
import select
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from moxi.paths import Layout

from . import protocol
from .procs import DEFAULT as DEFAULT_PROC, ProcView
from .results import (Ended, Facts, Failed, Gone, Lost, Passed, Ran, Refused, Started,
                      StopFacts, from_wire)

log = logging.getLogger("moxi.turns.client")

_ids = itertools.count(1)

#: How long a gate-passed `/exit` may take to end the harness process before it reads
#: `Failed("not-exited")`.
EXIT_BOUND_S = 5.0
#: The reason of a gate-passed `/exit` whose harness process still runs after the bound
#: (a reload's SIGTERM met it, or the exit was held).
NOT_EXITED = "not-exited"


class TurnsError(Exception):
    """An endpoint that cannot be reached or answered something turns cannot read."""


def runtime_dir() -> Path:
    """Where every endpoint's directory lives: `RUNTIME/turns/`."""
    return Layout("turns").runtime_dir()


class Session:
    """One live endpoint, as `find` or `sessions` found it."""

    def __init__(self, directory: Path, hello: dict[str, Any]):
        self.dir = directory
        self.sock = directory / protocol.SOCK
        self._hello = hello

    def __repr__(self) -> str:
        return (f"Session(harness={self.harness!r}, pid={self.pid}, "
                f"session_id={self.session_id!r})")

    @property
    def harness(self) -> str:
        return self._hello.get("harness", "")

    @property
    def can(self) -> frozenset[str]:
        return frozenset(self._hello.get("can") or ())

    @property
    def pid(self) -> int | None:
        return self._hello.get("pid")

    @property
    def session_id(self) -> str | None:
        return self._hello.get("session_id")

    @property
    def hello(self) -> dict[str, Any]:
        return dict(self._hello)

    # -- requests ----------------------------------------------------------------------

    def _request(self, op: str, timeout: float | None = protocol.CONNECT_TIMEOUT,
                 **fields) -> dict[str, Any]:
        """One request on a connection of its own. Raises `TurnsError` when the endpoint
        cannot be reached or does not answer."""
        rid = next(_ids)
        try:
            with protocol.connect(self.sock) as conn:
                conn.send({"v": protocol.VERSION, "id": rid, "op": op, **fields})
                reply = conn.recv(timeout)
        except (OSError, TimeoutError, protocol.ProtocolError) as exc:
            raise TurnsError(f"the endpoint at {self.dir} did not answer {op}: {exc}") \
                from None
        if reply is None:
            raise TurnsError(f"the endpoint at {self.dir} went away during {op}")
        if not reply.get("ok"):
            raise TurnsError(f"the endpoint refused {op}: {reply.get('code')}: "
                             f"{reply.get('error')}")
        return reply

    def refresh(self) -> dict[str, Any]:
        """Ask the endpoint for its hello now (the session id follows a `/clear`)."""
        self._hello = self._request("hello")["hello"]
        return self.hello

    def facts(self) -> Facts:
        return from_wire(self._request("facts")["facts"])

    def start(self, text: str, *, if_epoch: int | None = None, refuse_draft: bool = False,
              refuse_agents: bool = False) -> "Delivery":
        return self._deliver("start", text=text, if_epoch=if_epoch,
                             refuse_draft=refuse_draft, refuse_agents=refuse_agents)

    def deliver(self, text: str, *, steer: bool = False) -> "Delivery":
        return self._deliver("deliver", text=text, steer=steer)

    def steer(self, text: str) -> "Delivery":
        return self._deliver("steer", text=text)

    def _deliver(self, op: str, **fields) -> "Delivery":
        try:
            reply = self._request(op, **fields)
        except TurnsError as exc:
            log.info("%s", exc)
            return Delivery(self, None, Refused("gone"))
        known = from_wire(reply["result"]) if "result" in reply else None
        return Delivery(self, reply.get("delivery"), known)

    def command(self, name: str, *, if_epoch: int | None = None,
                timeout: float = 30.0) -> Ran | Refused | Failed:
        """Run `/clear` or `/exit`; `timeout` bounds the wait for the harness to settle it.

        `/exit` reads `Ran` only once the harness process is gone: when the
        harness's gate passes it the endpoint answers `passed`, and this watches the
        process `hello` names (a pidfd on Linux, opened before the request so a reused
        pid cannot stand in for it) and answers `Ran` once it has ended, or
        `Failed("not-exited")` when it still runs after `EXIT_BOUND_S`."""
        watch = _ProcessWatch(self.pid) if name == "exit" else None
        try:
            try:
                reply = self._request("command",
                                      timeout=timeout + protocol.CONNECT_TIMEOUT,
                                      name=name, if_epoch=if_epoch,
                                      timeout_ms=int(timeout * 1000))
            except TurnsError as exc:
                log.info("%s", exc)
                return Refused("gone")
            result = from_wire(reply["result"])
            if isinstance(result, Passed):
                if watch is not None and watch.wait(EXIT_BOUND_S):
                    return Ran(None)
                return Failed(NOT_EXITED)
            return result
        finally:
            if watch is not None:
                watch.close()

    def watch(self) -> Iterator:
        """Every `Ready`, `TurnStarted` and `TurnEnded` of the session as it happens, then
        one `Gone` when the endpoint ends or goes away."""
        try:
            conn = protocol.connect(self.sock)
        except OSError as exc:
            yield Gone(f"the endpoint could not be reached: {exc}")
            return
        try:
            conn.send({"v": protocol.VERSION, "id": next(_ids), "op": "watch"})
            first = conn.recv(protocol.CONNECT_TIMEOUT)
            if not first or not first.get("ok"):
                yield Gone("the endpoint refused watch")
                return
            while True:
                frame = conn.recv()
                if frame is None:
                    yield Gone("the endpoint went away")
                    return
                event = from_wire(frame.get("event"))
                yield event
                if isinstance(event, Gone):
                    return
        except (OSError, TimeoutError, protocol.ProtocolError, ValueError) as exc:
            yield Gone(f"the endpoint went away: {exc}")
        finally:
            conn.close()

    def hold(self, on_stop: Callable[[StopFacts], str | None]) -> "Hold":
        """Become the session's one holder: `on_stop` is asked at each Stop. Raises
        `TurnsError` when the endpoint cannot be reached or already has a holder."""
        try:
            conn = protocol.connect(self.sock)
            conn.send({"v": protocol.VERSION, "id": next(_ids), "op": "hold"})
            reply = conn.recv(protocol.CONNECT_TIMEOUT)
        except (OSError, TimeoutError, protocol.ProtocolError) as exc:
            raise TurnsError(f"the endpoint at {self.dir} did not answer hold: {exc}") \
                from None
        if not reply or not reply.get("ok"):
            conn.close()
            code = reply.get("code") if reply else "gone"
            raise TurnsError(f"the endpoint refused hold: {code}: "
                             f"{reply.get('error') if reply else 'it went away'}")
        return Hold(conn, on_stop)


class Delivery:
    """One text handed to turns. `id` is the id turns minted, or None when the endpoint
    could not be reached (then both waits answer `Refused("gone")`)."""

    def __init__(self, session: Session, delivery_id: str | None, known=None):
        self.session = session
        self.id = delivery_id
        self._known = known

    def __repr__(self) -> str:
        return f"Delivery({self.id!r})"

    def wait_started(self, timeout: float | None) -> Started | Refused | Lost:
        return self._wait("started", timeout)

    def wait_ended(self, timeout: float | None) -> Ended | Refused | Lost:
        return self._wait("ended", timeout)

    def _wait(self, until: str, timeout: float | None):
        """Raises `TimeoutError` when `timeout` passes first."""
        if self._known is not None:
            return self._known
        try:
            reply = self.session._request(
                "wait", timeout=None if timeout is None
                else timeout + protocol.CONNECT_TIMEOUT,
                delivery=self.id, until=until,
                timeout_ms=None if timeout is None else int(timeout * 1000))
        except TurnsError as exc:
            return Lost(str(exc))
        if reply.get("pending"):
            raise TimeoutError(f"delivery {self.id} has not {until} after {timeout} s")
        return from_wire(reply["result"])


class Hold:
    """The session's holder. `close()` gives the hold up."""

    def __init__(self, conn: protocol.LineConn, on_stop: Callable[[StopFacts], str | None]):
        self._conn = conn
        self._on_stop = on_stop
        self._closed = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="turns-hold")
        self._thread.start()

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    def _run(self) -> None:
        try:
            while True:
                try:
                    frame = self._conn.recv()
                except protocol.ProtocolError:
                    continue
                if frame is None:
                    return
                if frame.get("op") != "stop":
                    continue
                try:
                    facts = from_wire(frame.get("facts"))
                    reason = self._on_stop(facts)
                except Exception as exc:  # noqa: BLE001 - a broken holder lets the turn end
                    log.warning("on_stop raised %s: %s; the turn ends", type(exc).__name__,
                                exc)
                    reason = None
                if not isinstance(reason, str) or not reason:
                    reason = None
                with contextlib.suppress(OSError, protocol.ProtocolError):
                    self._conn.send({"v": protocol.VERSION, "op": protocol.STOP_ANSWER,
                                     "ask": frame.get("ask"), "reason": reason})
        finally:
            self._closed.set()
            self._conn.close()

    def close(self) -> None:
        self._conn.close()
        self._thread.join(timeout=2.0)


class _ProcessWatch:
    """Waits for one process to end: a pidfd where the platform has one, else a poll of
    `kill(pid, 0)`."""

    def __init__(self, pid: int | None):
        self.pid = pid if isinstance(pid, int) and pid > 0 else None
        self.fd = None
        if self.pid is not None:
            try:
                self.fd = os.pidfd_open(self.pid)
            except (AttributeError, OSError):
                self.fd = None

    def wait(self, timeout: float) -> bool:
        """True once the process has ended (or there was none to watch)."""
        if self.pid is None:
            return False
        if self.fd is not None:
            poller = select.poll()
            poller.register(self.fd, select.POLLIN)
            return bool(poller.poll(max(0, int(timeout * 1000))))
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.kill(self.pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                pass
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def close(self) -> None:
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None


# -- finding -------------------------------------------------------------------------


def _candidates() -> Iterator[tuple[Path, dict[str, Any]]]:
    """Every endpoint directory with a readable `hello.json`, newest first."""
    root = runtime_dir()
    try:
        entries = list(root.iterdir())
    except OSError:
        return
    found = []
    for directory in entries:
        try:
            hello = json.loads((directory / protocol.HELLO).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue          # starting, or not an endpoint: never swept from here
        if isinstance(hello, dict):
            found.append((directory, hello))
    found.sort(key=lambda item: item[1].get("started") or 0, reverse=True)
    yield from found


def _live(directory: Path) -> dict[str, Any] | None:
    """The endpoint's hello when its socket answers; None, after sweeping the directory
    when the socket is dead, when it does not."""
    session = Session(directory, {})
    try:
        return session._request("hello")["hello"]
    except TurnsError:
        pass
    verdict, _ = protocol.probe(directory / protocol.SOCK)
    if verdict == protocol.STALE:
        _sweep(directory)
    return None


def _sweep(directory: Path) -> None:
    for name in protocol.ENDPOINT_FILES:
        with contextlib.suppress(OSError):
            (directory / name).unlink()
    with contextlib.suppress(OSError):
        os.rmdir(directory)
    log.info("swept the dead endpoint directory %s", directory)


def sessions() -> list[Session]:
    """Every live endpoint, newest first. Dead ones are swept."""
    out = []
    for directory, _ in _candidates():
        hello = _live(directory)
        if hello is not None:
            out.append(Session(directory, hello))
    return out


def find(*, session_id: str | None = None, pid: int | None = None,
         proc: ProcView | None = None) -> Session | None:
    """The live endpoint of a session, or None.

    `session_id` matches an endpoint's current session id, or else an earlier id of the
    same launch. `pid` matches the harness process or any process under it: the walk goes
    up from `pid`, and the nearest harness process met wins.
    """
    if (session_id is None) == (pid is None):
        raise TypeError("find() takes exactly one of session_id= or pid=")
    candidates = list(_candidates())
    if session_id is not None:
        current = [c for c in candidates if c[1].get("session_id") == session_id]
        earlier = [c for c in candidates
                   if session_id in (c[1].get("earlier") or ()) and c not in current]
        order = current + earlier
    else:
        by_pid: dict[int, list] = {}
        for c in candidates:
            if isinstance(c[1].get("pid"), int):
                by_pid.setdefault(c[1]["pid"], []).append(c)
        order = []
        for ancestor in (proc or DEFAULT_PROC).ancestry(pid):
            order.extend(by_pid.get(ancestor, ()))
    for directory, _ in order:
        hello = _live(directory)
        if hello is None:
            continue
        # The file named the match; the live answer must still name it.
        if session_id is not None and not (hello.get("session_id") == session_id
                                           or session_id in (hello.get("earlier") or ())):
            continue
        return Session(directory, hello)
    return None


__all__ = ["EXIT_BOUND_S", "NOT_EXITED", "TurnsError", "Session", "Delivery", "Hold",
           "runtime_dir", "sessions", "find"]
