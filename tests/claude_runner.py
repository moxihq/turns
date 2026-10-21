"""A stand-in for the Claude runner and gate: reads the endpoint's lines and posts to its
`mod.sock`, as the two plugins do over the leg (`moxi_turns.claude_leg`)."""

from __future__ import annotations

import http.client
import itertools
import json
import os
import queue
import socket
import threading

from moxi_turns import claude_leg as leg


class UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float = 5.0):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.unix_path)
        self.sock = sock


def post(sock_path: str, path: str, body: dict, timeout: float = 5.0) -> tuple[int, dict]:
    conn = UnixHTTP(sock_path, timeout)
    try:
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        conn.close()


class FakeRunner:
    """Reads the endpoint's lines from `stream` (a binary file); answers `live` lines
    itself from `agents` and `draft` unless `answer_live` is off."""

    def __init__(self, stream):
        self.stream = stream
        self.first: dict | None = None
        self.lines: queue.Queue = queue.Queue()
        self.agents = 0
        self.draft = False
        self.answer_live = True
        self._seq = itertools.count(1)
        self._first = threading.Event()
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self) -> None:
        for raw in iter(self.stream.readline, b""):
            line = json.loads(raw)
            if self.first is None:
                self.first = line
                self._first.set()
                continue
            if line.get("op") == leg.LINE_LIVE:
                if self.answer_live:
                    threading.Thread(target=self.post, args=(leg.PATH_LIVE, {
                        "ask": line["ask"], "agents": self.agents, "draft": self.draft}),
                        daemon=True).start()
                continue
            self.lines.put(line)

    def wait_first(self, timeout: float = 5.0) -> dict:
        assert self._first.wait(timeout), "no first line from the endpoint"
        return self.first

    @property
    def mod_sock(self) -> str:
        return self.wait_first()["mod_sock"]

    def next_line(self, timeout: float = 5.0) -> dict:
        return self.lines.get(timeout=timeout)

    def no_line(self, timeout: float = 0.3) -> bool:
        try:
            line = self.lines.get(timeout=timeout)
        except queue.Empty:
            return True
        self.lines.put(line)
        return False

    def post(self, path: str, body: dict, timeout: float = 5.0) -> dict:
        status, answer = post(self.mod_sock, path, body, timeout)
        assert status == 200, (status, answer)
        return answer

    def report(self, kind: str, *, seq: int | None = None, **fields) -> dict:
        return self.post(leg.PATH_REPORT, {"seq": next(self._seq) if seq is None else seq,
                                           "kind": kind, **fields})

    # The runner's reports, by name.

    def session(self, session_id: str, mod_version: str = "0.1.0") -> dict:
        return self.report(leg.R_SESSION, session_id=session_id, mod_version=mod_version)

    def turn_start(self, turn_id: str) -> dict:
        return self.report(leg.R_TURN_START, turn_id=turn_id)

    def turn_complete(self, turn_id: str, reason: str = "answer",
                      is_aborted: bool = False) -> dict:
        return self.report(leg.R_TURN_COMPLETE, turn_id=turn_id, reason=reason,
                           is_aborted=is_aborted)

    def close(self, delivery: str, end: str, reason: str | None = None) -> dict:
        return self.report(leg.R_SUBMIT_CLOSE, delivery=delivery, end=end, reason=reason)

    def bound(self, delivery: str, reason: str = "unsettled after 5000 ms with no turn open"
              ) -> dict:
        return self.report(leg.R_SUBMIT_BOUND, delivery=delivery, reason=reason)

    # The gate's ask, as a runner prompt enters.

    def enter(self, nonce: str, *, draft: bool = False, agents=0) -> dict:
        return self.post(leg.PATH_GATE_ENTRY, {"nonce": nonce, "draft": draft,
                                               "agents": agents})


def pipe_pair():
    """(read end as a binary file, write end as an unbuffered binary file)."""
    r, w = os.pipe()
    return os.fdopen(r, "rb"), os.fdopen(w, "wb", buffering=0)
