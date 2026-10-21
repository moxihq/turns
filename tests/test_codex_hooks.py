"""Codex hook trust, listener output, and the Stop hold."""

from __future__ import annotations

import io
import json
import shlex
import sys
import time

from fake_harness import FakeHarness
from moxi_turns import codex_hooks, listeners
from moxi_turns.client import Session
from moxi_turns.endpoint import Endpoint
from moxi_turns.results import StopFacts
from conftest import write_config


class HookHarness(FakeHarness):
    name = "codex"
    can = frozenset({"start", "steer", "hold"})

    def __init__(self):
        super().__init__(can=self.can)
        self.session_id = "thread-1"
        self.foreign = []
        self.settled = []
        self.ending = []

    def stop_facts(self, frame):
        return StopFacts(turn_id=frame.get("turn_id"), session_id=self.session_id,
                         stop_hook_active=frame.get("stop_hook_active"), crons=0)

    def settle_turn(self, turn_id, outcome):
        # No hook closes a turn (RULE K1); a call here is a failure the tests look for.
        self.settled.append((turn_id, outcome))
        self.endpoint.turn_ended(turn_id, outcome)

    def stop_answered(self, turn_id):
        self.ending.append(turn_id)
        self.endpoint.turn_ending(turn_id)

    def foreign_hook(self, event, session_id):
        self.foreign.append((event, session_id))


def _registry():
    def answer(event):
        return f"context at {event.event}"

    listener = listeners.Listener(
        "sample", frozenset({"ready", "prompt"}), "sample.module:answer", _fn=answer,
    )
    return listeners.Registry((listener,))


def _ping(monkeypatch, path, event, data, *extra):
    stdin = io.StringIO(json.dumps(data))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    assert codex_hooks.main(["--socket", str(path), event, *extra]) == 0
    return stdout.getvalue()


def test_session_hooks_render_listener_context_through_turns_hook_code(
        monkeypatch, turns_env):
    harness = HookHarness()
    endpoint = Endpoint(harness, registry=_registry(),
                        session_id=harness.session_id).open()
    server = codex_hooks.HookServer(harness, endpoint.dir / "hooks.sock").start()
    try:
        output = _ping(monkeypatch, server.socket_path, "SessionStart",
                       {"session_id": "thread-1", "source": "startup"})
        assert json.loads(output)["hookSpecificOutput"] == {
            "hookEventName": "SessionStart", "additionalContext": "context at ready",
        }
        output = _ping(monkeypatch, server.socket_path, "UserPromptSubmit",
                       {"session_id": "thread-1"})
        assert json.loads(output)["hookSpecificOutput"]["additionalContext"] == \
            "context at prompt"
    finally:
        server.stop()
        endpoint.stop("the test ended")


def test_stop_blocks_only_after_the_ping_prints_the_reason(monkeypatch, turns_env):
    harness = HookHarness()
    endpoint = Endpoint(harness, session_id=harness.session_id).open()
    server = codex_hooks.HookServer(harness, endpoint.dir / "hooks.sock").start()
    session = Session(endpoint.dir, endpoint.hello())
    hold = session.hold(lambda facts: "reply before stopping")
    harness.start_turn()
    try:
        output = _ping(monkeypatch, server.socket_path, "Stop", {
            "session_id": "thread-1", "turn_id": harness.open_turn,
            "stop_hook_active": False,
        })
        assert json.loads(output) == {
            "decision": "block", "reason": "reply before stopping",
        }
        # A blocked Stop: the turn runs on, open to steers.
        assert endpoint.facts().turn_open and harness.ending == []

        hold.close()
        output = _ping(monkeypatch, server.socket_path, "Stop", {
            "session_id": "thread-1", "turn_id": harness.open_turn,
        })
        assert output == ""
        # Stop asks whether the turn may end; it ends on Codex's turn/completed. An
        # unblocked answer marks it ending: still open, but it takes no more steers.
        assert endpoint.facts().turn_open and harness.settled == []
        assert harness.ending == [harness.open_turn]
        assert endpoint.ledger.open.ending
    finally:
        hold.close()
        server.stop()
        endpoint.stop("the test ended")


def test_an_interrupt_ping_closes_no_turn(monkeypatch, turns_env):
    harness = HookHarness()
    endpoint = Endpoint(harness, session_id=harness.session_id).open()
    server = codex_hooks.HookServer(harness, endpoint.dir / "hooks.sock").start()
    harness.start_turn()
    try:
        assert _ping(monkeypatch, server.socket_path, "Interrupt", {
            "session_id": "thread-1", "turn_id": harness.open_turn,
        }) == ""
        assert endpoint.facts().turn_open and harness.settled == []
        assert harness.ending == []
    finally:
        server.stop()
        endpoint.stop("the test ended")


def test_each_hook_is_hashed_and_must_be_trusted():
    argv, keys = codex_hooks.session_flags(
        ["/env/bin/moxi-turns-ping"], "/run/hooks.sock"
    )
    assert len(keys) == len(codex_hooks.EVENTS)
    assert argv.count("-c") == len(codex_hooks.EVENTS) + 1
    result = {"data": [{"hooks": [
        {"key": key, "enabled": True, "trustStatus": "trusted"} for key in keys
    ]}]}
    assert codex_hooks.trust_problem(result, keys) is None
    result["data"][0]["hooks"][0]["trustStatus"] = "untrusted"
    assert "not trusted" in codex_hooks.trust_problem(result, keys)


def test_hook_commands_carry_the_endpoints_event_budget():
    argv, _ = codex_hooks.session_flags(
        ["/env/bin/moxi-turns-ping"], "/run/hooks.sock",
        hold_wait_s=4, listen_wait_s=6,
    )
    commands = [argv[index + 1] for index, value in enumerate(argv[:-1]) if value == "-c"]
    joined = "\n".join(commands)
    assert "--wait-seconds 5 Stop" in joined
    assert "--wait-seconds 7 SessionStart" in joined
    assert "--wait-seconds 7 UserPromptSubmit" in joined
    assert "--wait-seconds 7 PostCompact" in joined
    assert "--wait-seconds 3 Interrupt" in joined
    assert shlex.split(codex_hooks.command(
        ["ping"], "/run/hooks.sock", codex_hooks.EVENTS[0], 5,
    )) == ["ping", "--socket", "/run/hooks.sock", "--wait-seconds", "5",
           "SessionStart"]


def test_holder_and_listener_may_answer_after_three_seconds(monkeypatch, turns_env):
    write_config(turns_env, "hold.wait_ms = 4000\nlisten.wait_ms = 4000\n")

    def answer(event):
        time.sleep(3.05)
        return "late but in budget"

    registry = listeners.Registry((listeners.Listener(
        "slow", frozenset({"ready"}), "slow:answer", _fn=answer,
    ),))
    harness = HookHarness()
    endpoint = Endpoint(harness, registry=registry,
                        session_id=harness.session_id).open()
    server = codex_hooks.HookServer(harness, endpoint.dir / "hooks.sock").start()
    session = Session(endpoint.dir, endpoint.hello())
    hold = session.hold(lambda facts: (time.sleep(3.05), "hold in budget")[1])
    harness.start_turn()
    try:
        output = _ping(monkeypatch, server.socket_path, "SessionStart",
                       {"session_id": "thread-1"}, "--wait-seconds", "5")
        assert json.loads(output)["hookSpecificOutput"]["additionalContext"] == \
            "late but in budget"
        output = _ping(monkeypatch, server.socket_path, "Stop", {
            "session_id": "thread-1", "turn_id": harness.open_turn,
        }, "--wait-seconds", "5")
        assert json.loads(output)["reason"] == "hold in budget"
    finally:
        hold.close()
        server.stop()
        endpoint.stop("the test ended")


def test_late_hook_answers_produce_no_context_or_block(monkeypatch, turns_env):
    write_config(turns_env, "hold.wait_ms = 50\nlisten.wait_ms = 50\n")

    def answer(event):
        time.sleep(0.2)
        return "too late"

    registry = listeners.Registry((listeners.Listener(
        "slow", frozenset({"ready"}), "slow:answer", _fn=answer,
    ),))
    harness = HookHarness()
    endpoint = Endpoint(harness, registry=registry,
                        session_id=harness.session_id).open()
    server = codex_hooks.HookServer(harness, endpoint.dir / "hooks.sock").start()
    session = Session(endpoint.dir, endpoint.hello())
    hold = session.hold(lambda facts: (time.sleep(0.2), "too late")[1])
    harness.start_turn()
    try:
        assert _ping(monkeypatch, server.socket_path, "SessionStart",
                     {"session_id": "thread-1"}, "--wait-seconds", "0.1") == ""
        assert _ping(monkeypatch, server.socket_path, "Stop", {
            "session_id": "thread-1", "turn_id": harness.open_turn,
        }, "--wait-seconds", "0.1") == ""
        assert endpoint.facts().turn_open and harness.settled == []
        assert harness.ending == [harness.open_turn]   # no block came: unblocked
    finally:
        hold.close()
        server.stop()
        endpoint.stop("the test ended")


def test_load_hooks_ask_listeners_but_leave_ready_to_the_accepted_load(
        monkeypatch, turns_env):
    import threading

    harness = HookHarness()
    endpoint = Endpoint(harness, registry=_registry(),
                        session_id=harness.session_id).open()
    server = codex_hooks.HookServer(harness, endpoint.dir / "hooks.sock").start()
    events = []
    session = Session(endpoint.dir, endpoint.hello())
    watcher = threading.Thread(target=lambda: events.extend(session.watch()), daemon=True)
    watcher.start()
    time.sleep(0.2)
    try:
        for event, source in (("SessionStart", "startup"), ("SessionStart", "resume"),
                              ("SessionStart", "clear"), ("PostCompact", "auto")):
            output = _ping(monkeypatch, server.socket_path, event,
                           {"session_id": "thread-1", "source": source})
            assert json.loads(output)["hookSpecificOutput"]["additionalContext"] == \
                "context at ready"
        deadline = time.monotonic() + 2
        while len(events) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)
        assert [e.source for e in events] == ["clear", "auto"]
    finally:
        server.stop()
        endpoint.stop("the test ended")
        watcher.join(2)
