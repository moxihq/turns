"""The endpoint core's held turns, driven by the fake harness: a
turn whose `TurnStarted` and `TurnEnded` wait for a decision made after it opened, while
the ledger opens and closes it at once."""

from __future__ import annotations

import queue
import threading
import time

import moxi_turns as turns
from fake_harness import FakeHarness


def _watch(s):
    events: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: [events.put(e) for e in s.watch()], daemon=True).start()
    time.sleep(0.1)
    return events


def _drain(events, timeout=0.3):
    out = []
    while True:
        try:
            out.append(events.get(timeout=timeout))
        except queue.Empty:
            return out


def test_the_ledger_opens_a_held_turn_at_once_and_the_tie_raises_its_events(make_endpoint):
    ep, fake = make_endpoint(FakeHarness(auto=False))
    s = turns.find(session_id="s-1")
    events = _watch(s)
    d = s.deliver("x")
    ep.turn_started("T", held=True)
    f = s.facts()
    assert f.turn_open and f.epoch == 1
    assert s.start("y").wait_started(1) == turns.Refused("busy-turn")
    ep.turn_ended("T", "answer")
    assert not s.facts().turn_open
    assert _drain(events) == []
    assert ep.turn_tied("T", d.id) is True
    assert d.wait_ended(1) == turns.Ended("T", "answer")
    assert [(type(e).__name__, e.delivery_id) for e in _drain(events)] == [
        ("TurnStarted", d.id), ("TurnEnded", d.id)]


def test_a_released_turn_is_the_person_and_the_delivery_waits_on(make_endpoint):
    ep, fake = make_endpoint(FakeHarness(auto=False))
    s = turns.find(session_id="s-1")
    events = _watch(s)
    d = s.deliver("x")
    ep.turn_started("P", held=True)
    ep.turn_released("P")
    assert [(type(e).__name__, e.delivery_id) for e in _drain(events)] == [
        ("TurnStarted", None)]
    ep.lost(d.id, "untied: test")
    result = d.wait_started(1)
    assert isinstance(result, turns.Lost) and result.untied


def test_a_tie_to_a_delivery_no_longer_waiting_releases_the_turn(make_endpoint):
    ep, fake = make_endpoint(FakeHarness(auto=False))
    s = turns.find(session_id="s-1")
    d = s.start("x")
    ep.refused(d.id, "stale-turn")
    ep.turn_started("T", held=True)
    assert ep.turn_tied("T", d.id) is False
    assert d.wait_ended(1) == turns.Refused("stale-turn")


def test_the_held_slot_frees_only_at_the_decision(make_endpoint):
    ep, fake = make_endpoint(FakeHarness(auto=False))
    s = turns.find(session_id="s-1")
    d1 = s.deliver("one")
    d2 = s.deliver("two")
    assert [x.id for x in fake.submitted] == [d1.id]
    ep.turn_started("T1", held=True)
    ep.turn_ended("T1", "answer")
    assert [x.id for x in fake.submitted] == [d1.id]       # not decided yet
    ep.turn_tied("T1", d1.id)
    assert [x.id for x in fake.submitted] == [d1.id, d2.id]


def test_stop_releases_a_held_turn_before_gone(make_endpoint):
    ep, fake = make_endpoint(FakeHarness(auto=False))
    s = turns.find(session_id="s-1")
    events = _watch(s)
    d = s.deliver("x")
    ep.turn_started("T", held=True)
    ep.stop("the endpoint stopped")
    got = [type(e).__name__ for e in _drain(events)]
    assert got == ["TurnStarted", "Gone"]
    lost = ep.wait(d.id, "ended", 0)
    assert isinstance(lost, turns.Lost) and not lost.untied
