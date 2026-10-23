"""`moxi-turns-endpoint` as a process: its first line, its caller socket once it has the
session id, and its life: it ends on its own SIGTERM, an EPIPE on its stdout
or a change of parent, never on anything the runner says, and a hangup or an interrupt
from a terminal does not end it."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time

import pytest

import moxi_turns as turns
from claude_runner import post
from conftest import write_config
from moxi_turns import claude_leg as leg

ENDPOINT = [sys.executable, "-m", "moxi_turns.claude_endpoint"]


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


def _until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@pytest.fixture
def spawn(turns_env):
    procs = []

    def make(argv=ENDPOINT):
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE)
        procs.append(p)
        return p

    yield make
    for p in procs:
        if p.poll() is None:
            p.kill()
        p.wait(timeout=5)


def _first(p) -> dict:
    line = p.stdout.readline()
    assert line, p.stderr.read().decode()
    return json.loads(line)


def test_first_line_then_hello_once_the_runner_sends_the_id(spawn):
    p = spawn()
    first = _first(p)
    assert first["op"] == "hello" and first["pid"] == p.pid
    assert turns.find(pid=os.getpid()) is None
    _, answer = post(first["mod_sock"], leg.PATH_REPORT,
                     {"seq": 1, "kind": "session", "session_id": "s-p", "mod_version": "9"})
    assert answer["ok"]
    assert _until(lambda: turns.find(session_id="s-p") is not None)
    s = turns.find(pid=os.getpid())               # its parent is the harness
    assert s.session_id == "s-p" and s.pid == os.getpid()


def test_sigterm_ends_it_with_lost_and_removes_its_directory(spawn):
    p = spawn()
    first = _first(p)
    sock = first["mod_sock"]
    post(sock, leg.PATH_REPORT, {"seq": 1, "kind": "session", "session_id": "s-t"})
    assert _until(lambda: turns.find(session_id="s-t") is not None)
    s = turns.find(session_id="s-t")
    d = s.deliver("x")
    assert json.loads(p.stdout.readline())["op"] == "submit"
    post(sock, leg.PATH_REPORT, {"seq": 2, "kind": "turn.start", "turn_id": "T"})
    result = []
    t = threading.Thread(target=lambda: result.append(d.wait_ended(5)))
    t.start()
    time.sleep(0.2)
    p.send_signal(signal.SIGTERM)
    assert p.wait(timeout=5) == 0
    t.join(5)
    assert isinstance(result[0], turns.Lost) and not result[0].untied
    assert not os.path.exists(os.path.dirname(sock))


def test_a_hangup_or_an_interrupt_does_not_end_it(spawn):
    p = spawn()
    _first(p)
    p.send_signal(signal.SIGHUP)
    p.send_signal(signal.SIGINT)
    time.sleep(0.3)
    assert p.poll() is None


def test_epipe_on_its_stdout_ends_it(spawn):
    p = spawn()
    first = _first(p)
    post(first["mod_sock"], leg.PATH_REPORT, {"seq": 1, "kind": "session",
                                              "session_id": "s-e"})
    assert _until(lambda: turns.find(session_id="s-e") is not None)
    p.stdout.close()                              # the runner is gone
    turns.find(session_id="s-e").facts()          # a `live` line meets the closed pipe
    assert p.wait(timeout=5) == 0
    assert not os.path.exists(os.path.dirname(first["mod_sock"]))


def test_a_change_of_parent_ends_it(spawn):
    # A parent that starts the endpoint and is then killed, as `kill -9` of claude does.
    script = ("import subprocess, sys\n"
              f"p = subprocess.Popen({ENDPOINT!r}, stdin=subprocess.DEVNULL)\n"
              "print(p.pid, flush=True)\n"
              "p.wait()\n")
    parent = spawn([sys.executable, "-c", script])
    # The endpoint's first line and the parent's pid line share stdout, in either order.
    lines = [json.loads(parent.stdout.readline()) for _ in range(2)]
    first = next(x for x in lines if isinstance(x, dict))
    endpoint_pid = next(x for x in lines if isinstance(x, int))
    assert first["pid"] == endpoint_pid
    parent.kill()
    parent.wait()
    assert _until(lambda: not _alive(endpoint_pid), timeout=5)
    assert not os.path.exists(os.path.dirname(first["mod_sock"]))


def test_a_broken_config_sends_one_failed_line(spawn, turns_env):
    write_config(turns_env, "[hold]\nwait_ms = 'soon'\n")
    p = spawn()
    first = _first(p)
    assert first["op"] == "failed" and first["leg"] == leg.LEG and first["error"]
    assert p.wait(timeout=5) == 2
