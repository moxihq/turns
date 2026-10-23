"""The listener group and its asking."""

from __future__ import annotations

import os
import time
import types

import pytest

import moxi_turns as turns
from fake_harness import FakeHarness
import listener_targets
from moxi_turns import listeners


class Entry:
    def __init__(self, name, rows):
        self.name = name
        self._rows = rows

    def load(self):
        if isinstance(self._rows, Exception):
            raise self._rows
        return types.SimpleNamespace(LISTENERS=self._rows)


def registry(*entries):
    return listeners.load(entries)


def event(name="ready", **kw):
    return turns.SessionEvent(event=name, harness="fake", session_id="s-1",
                              env={"HOME": "/h"}, **kw)


@pytest.fixture(autouse=True)
def clear_calls():
    listener_targets.CALLS.clear()


def test_texts_join_in_module_name_order_and_each_listener_gets_its_own_env():
    reg = registry(Entry("zeta", [("ready", "listener_targets:comms")]),
                   Entry("alpha", [(("ready", "prompt"), "listener_targets:cache")]))
    assert [ls.module for ls in reg.listeners] == ["alpha", "zeta"]
    text = listeners.ask(reg, event("ready", source="startup"), 2.0)
    assert text == "cache context\n\ncomms at ready"
    assert ("cache", "ready", None) in listener_targets.CALLS
    assert listeners.ask(reg, event("prompt"), 2.0) == "cache context"


def test_a_late_broken_or_odd_answer_counts_as_none():
    reg = registry(Entry("a", [("prompt", "listener_targets:slow")]),
                   Entry("b", [("prompt", "listener_targets:broken")]),
                   Entry("c", [("prompt", "listener_targets:not_text")]),
                   Entry("d", [("prompt", "listener_targets:silent")]),
                   Entry("e", [("prompt", "listener_targets:cache")]),
                   Entry("f", [("prompt", "listener_targets:missing")]))
    t0 = time.monotonic()
    assert listeners.ask(reg, event("prompt"), 0.2) == "cache context"
    assert time.monotonic() - t0 < 0.6          # one bound for all, not one each
    assert listeners.ask(registry(Entry("d", [("prompt", "listener_targets:silent")])),
                         event("prompt"), 0.2) is None


def test_turn_and_gone_answers_are_ignored_and_do_not_wait():
    reg = registry(Entry("a", [(("turn", "gone"), "listener_targets:slow")]),
                   Entry("b", [("turn", "listener_targets:comms")]))
    t0 = time.monotonic()
    assert listeners.ask(reg, event("turn", turn_id="t1", outcome="answer"), 2.0) is None
    assert time.monotonic() - t0 < 0.5
    t0 = time.monotonic()
    listeners.ask(reg, event("gone", reason="bye"), 0.3, wait=True)
    assert 0.25 < time.monotonic() - t0 < 0.9


@pytest.mark.parametrize("rows, why", [
    (None, "LISTENERS"),
    ([("ready",)], "not (events, target)"),
    ([("finished", "x.y:z")], "names events"),
    ([("ready", "no-colon")], "has target"),
    (ImportError("gone"), "does not load"),
])
def test_a_bad_registration_is_broken_and_costs_only_its_module(rows, why):
    reg = registry(Entry("bad", rows), Entry("good", [("ready", "listener_targets:cache")]))
    assert [b.module for b in reg.broken] == ["bad"] and why in reg.broken[0].reason
    assert [ls.module for ls in reg.listeners] == ["good"]


def test_two_registrations_under_one_name_are_both_off():
    reg = registry(Entry("comms", [("ready", "listener_targets:comms")]),
                   Entry("comms", [("ready", "listener_targets:cache")]))
    assert reg.listeners == () and "uninstall all but one" in reg.broken[0].reason


def test_the_endpoint_asks_at_ready_and_prompt_and_tells_turn_and_gone(make_endpoint):
    reg = registry(Entry("comms", [(("ready", "prompt", "turn", "gone"),
                                    "listener_targets:comms")]))
    env = dict(os.environ, SESSION_STAMP="x")
    ep, fake = make_endpoint(registry=reg, env=env)
    assert ep.ready("startup", "s-1") == "comms at ready"
    assert ep.prompt() == "comms at prompt"
    fake.start_turn()
    fake.end_turn("aborted")
    ep.gone("the session ended")
    time.sleep(0.1)
    assert [c[:4] for c in listener_targets.CALLS] == [
        ("comms", "ready", "s-1", "startup"), ("comms", "prompt", "s-1", None),
        ("comms", "turn", "s-1", None), ("comms", "gone", "s-1", None)]


def test_the_environment_metadata_group_loads(monkeypatch):
    # With nothing registered in this environment, the group is empty and nothing breaks.
    reg = listeners.load()
    assert all(b.module for b in reg.broken)
    assert listeners.GROUP == "moxi.turns.listeners"
