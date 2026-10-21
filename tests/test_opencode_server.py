"""The OpenCode server's argv, its listening line, and the HTTP client's two kinds of
failure."""

from __future__ import annotations

import ctypes
import os
import signal
import socket
import sys
import threading

import pytest

from moxi_turns import procs
from moxi_turns.opencode_server import LISTENING, Http, NoAnswer, Unreachable, serve_argv


def test_the_server_listens_on_loopback_on_a_port_it_chooses_and_logs():
    assert serve_argv("/x/opencode") == ["/x/opencode", "serve", "--hostname", "127.0.0.1",
                                         "--port", "0", "--print-logs", "--log-level",
                                         "INFO"]


def test_the_listening_line_names_the_address():
    line = 'timestamp=x level=INFO message="opencode server listening on http://127.0.0.1:4096"'
    assert LISTENING.search(line).group(1) == "http://127.0.0.1:4096"


def test_a_refused_connection_is_unreachable_and_a_dropped_answer_is_no_answer():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(Unreachable):
        Http(f"http://127.0.0.1:{port}", "pw", timeout=1).request("GET", "/x")

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def swallow():
        conn, _ = listener.accept()
        conn.recv(65536)
        conn.close()                    # read the request, answer nothing

    threading.Thread(target=swallow, daemon=True).start()
    with pytest.raises(NoAnswer):
        Http(f"http://127.0.0.1:{listener.getsockname()[1]}", "pw", timeout=2).request(
            "POST", "/session/ses_x/prompt_async", {"messageID": "msg_x"})
    listener.close()


def test_requests_carry_the_password_as_basic_auth():
    assert Http("http://127.0.0.1:1", "pw").headers()["authorization"] == \
        "Basic b3BlbmNvZGU6cHc="


def test_the_parent_death_ask_resolves_prctl_in_the_parent(monkeypatch):
    # The host is multi-threaded: a dlopen between fork and exec can hang the child.
    if not sys.platform.startswith("linux"):
        assert procs.die_with_parent(os.getpid()) is None
        return
    opened, asked = [], []

    class Libc:
        def prctl(self, *args):
            asked.append(args)
            return 0

    monkeypatch.setattr(ctypes, "CDLL", lambda *a, **k: opened.append(a) or Libc())
    pre = procs.die_with_parent(os.getppid())
    assert len(opened) == 1
    pre()
    assert len(opened) == 1
    assert asked == [(procs.PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)]
