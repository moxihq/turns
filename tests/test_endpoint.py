"""The endpoint core and its turn ledger, driven by the fake harness, through the public
API over the socket."""

from __future__ import annotations

import json
import os
import stat
import threading
import time

import pytest

import moxi_turns as turns
from conftest import write_config
from fake_harness import FakeHarness


def test_find_by_session_id_and_by_pid(make_endpoint):
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    assert s is not None and s.harness == "fake" and s.pid == os.getpid()
    assert s.can == frozenset({"start", "steer", "hold", "clear", "exit"})
    assert turns.find(pid=os.getpid()).dir == ep.dir
    assert turns.find(session_id="nobody") is None
    assert [x.dir for x in turns.sessions()] == [ep.dir]


def test_find_by_pid_walks_up_from_a_process_under_the_harness(make_endpoint):
    class Tree:
        def ancestry(self, pid):
            yield from (pid, 4242, os.getpid(), 1)

    ep, _ = make_endpoint()
    assert turns.find(pid=999999, proc=Tree()).dir == ep.dir


def test_directory_and_hello(make_endpoint):
    ep, _ = make_endpoint()
    assert stat.S_IMODE(ep.dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(ep.sock_path.stat().st_mode) == 0o600
    assert ep.dir.name == f"fake-{ep.nonce}"
    hello = json.loads(ep.hello_path.read_text())
    assert hello["session_id"] == "s-1" and hello["protocol"] == [1]
    assert {"harness", "pid", "earlier", "cwd", "can", "endpoint_version",
            "mod_version"} <= set(hello)


def test_a_clear_keeps_the_endpoint_and_hello_follows_the_id(make_endpoint):
    ep, _ = make_endpoint()
    ep.ready("clear", "s-2")
    assert json.loads(ep.hello_path.read_text())["session_id"] == "s-2"
    assert turns.find(session_id="s-1").session_id == "s-2"     # an earlier id still finds it
    assert turns.find(session_id="s-2").dir == ep.dir


def test_a_dead_directory_is_swept_and_a_starting_one_is_not(make_endpoint, turns_env):
    ep, _ = make_endpoint()
    dead = ep.dir.parent / "fake-dead"
    dead.mkdir()
    (dead / "hello.json").write_text(json.dumps({"harness": "fake", "pid": os.getpid(),
                                                 "session_id": "s-1", "started": 9e12}))
    starting = ep.dir.parent / "fake-starting"
    starting.mkdir()
    assert turns.find(session_id="s-1").dir == ep.dir
    assert not dead.exists() and starting.exists()


def test_start_runs_and_ends_with_the_outcome(make_endpoint):
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    d = s.start("hello")
    assert d.wait_started(2) == turns.Started("t1")
    assert fake.submitted[0].text == "hello"
    with pytest.raises(TimeoutError):
        d.wait_ended(0.05)
    fake.end_turn("answer")
    assert d.wait_ended(2) == turns.Ended("t1", "answer")
    f = s.facts()
    assert (f.session_id, f.epoch, f.turn_open, f.stale) == ("s-1", 1, False, False)


@pytest.mark.parametrize("outcome", ["aborted", "error", "refusal"])
def test_esc_and_errors_are_outcomes(make_endpoint, outcome):
    ep, fake = make_endpoint()
    d = turns.find(session_id="s-1").start("x")
    fake.end_turn(outcome)
    assert d.wait_ended(2) == turns.Ended("t1", outcome)


def test_start_refusals(make_endpoint):
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    fake.start_turn()
    assert s.start("x").wait_started(1) == turns.Refused("busy-turn")
    fake.end_turn()
    assert s.start("x", if_epoch=0).wait_started(1) == turns.Refused("stale-turn")
    fake.draft = True
    assert s.start("x", refuse_draft=True).wait_started(1) == turns.Refused("draft")
    fake.draft, fake.agents = False, 1
    assert s.start("x", refuse_agents=True).wait_started(1) == turns.Refused("busy-agents")
    fake.live_fails = True
    assert s.start("x", refuse_draft=True).wait_started(1) == turns.Refused("draft")
    fake.live_fails, fake.agents = False, 0
    d = s.start("x", if_epoch=1, refuse_draft=True, refuse_agents=True)
    assert d.wait_started(1) == turns.Started("t2")


def test_an_unreadable_subagent_list_is_unknown_and_counts_as_busy(make_endpoint):
    # §5.7 idle facts; b6-1 [L3]: never 0.
    env = dict(os.environ, MOXI_TURNS_COMMANDS="clear")
    ep, fake = make_endpoint(env=env)
    s = turns.find(session_id="s-1")
    fake.agents = None
    assert s.facts().agents is None
    assert s.start("x", refuse_agents=True).wait_started(1) == turns.Refused("busy-agents")
    assert s.command("clear") == turns.Refused("busy-agents")
    fake.live_fails = True
    f = s.facts()
    assert f.agents is None and f.draft is True


def test_a_turn_typed_between_the_submit_and_the_entry_refuses_the_start(make_endpoint):
    ep, fake = make_endpoint(FakeHarness(auto=False))
    s = turns.find(session_id="s-1")
    d = s.start("x", if_epoch=0)
    fake.start_turn()                       # the person types first
    fake.end_turn()
    assert fake.enter(fake.submitted[0]) == "stale-turn"
    assert d.wait_started(1) == turns.Refused("stale-turn")
    assert d.wait_ended(1) == turns.Refused("stale-turn")


def test_a_start_whose_checks_fail_where_it_takes_effect_is_refused(make_endpoint):
    ep, fake = make_endpoint(FakeHarness(auto=False))
    d = turns.find(session_id="s-1").start("x", refuse_draft=True)
    fake.draft = True
    assert fake.enter(fake.submitted[0]) == "draft"
    assert d.wait_started(1) == turns.Refused("draft")


def test_deliver_waits_for_the_running_turn_and_two_are_two_turns(make_endpoint):
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    fake.start_turn()                        # the person's turn
    a, b = s.deliver("a"), s.deliver("b")
    with pytest.raises(TimeoutError):
        a.wait_started(0.05)
    assert fake.submitted == []
    fake.end_turn()
    assert a.wait_started(1) == turns.Started("t2")
    assert [x.text for x in fake.submitted] == ["a"]
    fake.end_turn()
    assert a.wait_ended(1) == turns.Ended("t2", "answer")
    assert b.wait_started(1) == turns.Started("t3")
    fake.end_turn("aborted")
    assert b.wait_ended(1) == turns.Ended("t3", "aborted")
    # A start is refused while a delivery is held.
    fake.start_turn()
    s.deliver("c")
    assert s.start("x").wait_started(1) == turns.Refused("busy-turn")


def test_steer(make_endpoint):
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    assert s.steer("x").wait_started(1) == turns.Refused("idle")
    turn = fake.start_turn()
    d = s.steer("now")
    assert d.wait_started(1) == turns.Started(turn)
    assert [x.text for x in fake.appended] == ["now"]
    e = s.deliver("also", steer=True)
    assert e.wait_started(1) == turns.Started(turn)
    fake.end_turn()
    assert d.wait_ended(1) == e.wait_ended(1) == turns.Ended(turn, "answer")
    idle = s.deliver("new", steer=True)                # idle: a new turn
    assert idle.wait_started(1) == turns.Started("t2")


def test_a_steering_deliver_racing_a_turn_end_gets_a_new_turn(make_endpoint):
    """b5-1 [L1]: the turn ends between the caller's deliver(steer=True) and the steer
    taking hold. The text is mail for a new turn, never `idle`."""
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    fake.start_turn()
    made = ep._new

    def racing_new(kind, text, **checks):
        if kind == "steer" and fake.open_turn:
            fake.end_turn()                      # the turn ends in the window
        return made(kind, text, **checks)

    ep._new = racing_new
    d = s.deliver("mail", steer=True)
    assert d.wait_started(1) == turns.Started("t2")
    assert [x.text for x in fake.submitted] == ["mail"]
    assert [x.kind for x in fake.submitted] == ["deliver"]
    assert fake.appended == []
    # Racing a turn end with another delivery in flight, it queues behind that one.
    slow = FakeHarness(auto=False)
    ep2, fake2 = make_endpoint(slow, session_id="s-2")
    s2 = turns.find(session_id="s-2")
    fake2.start_turn()
    first = s2.deliver("first")                  # held for the running turn
    made2 = ep2._new

    def racing_new2(kind, text, **checks):
        if kind == "steer" and fake2.open_turn:
            fake2.end_turn()                     # "first" goes in flight, untied
        return made2(kind, text, **checks)

    ep2._new = racing_new2
    second = s2.deliver("second", steer=True)
    assert [x.text for x in fake2.submitted] == ["first"]
    fake2.enter(fake2.submitted[0])
    assert first.wait_started(1) == turns.Started("t2")
    fake2.end_turn()
    fake2.enter(fake2.submitted[1])
    assert second.wait_started(1) == turns.Started("t3")


def test_unsupported_and_gone(make_endpoint):
    ep, fake = make_endpoint(FakeHarness(can=("start",)))
    s = turns.find(session_id="s-1")
    fake.start_turn()
    assert s.steer("x").wait_started(1) == turns.Refused("unsupported")
    assert s.command("clear") == turns.Refused("unsupported")
    d = s.deliver("held", steer=True)            # no steer here: held to the turn's end
    ep.stop("the session ended")
    assert isinstance(d.wait_ended(1), turns.Lost)      # the endpoint is gone with it
    assert s.start("x").wait_started(1) == turns.Refused("gone")
    assert turns.find(session_id="s-1") is None


def test_an_open_delivery_answers_lost_when_the_endpoint_goes(make_endpoint):
    ep, fake = make_endpoint()
    d = turns.find(session_id="s-1").start("x")
    assert d.wait_started(1) == turns.Started("t1")
    waiter = []
    t = threading.Thread(target=lambda: waiter.append(d.wait_ended(None)))
    t.start()
    time.sleep(0.1)
    ep.gone("a reload")
    t.join(3)
    assert waiter == [turns.Lost("a reload")]


def test_watch_yields_every_turn_event_then_gone(make_endpoint):
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    events = []
    started = threading.Event()

    def read():
        for ev in s.watch():
            events.append(ev)
            started.set()

    t = threading.Thread(target=read)
    t.start()
    time.sleep(0.2)
    ep.ready("startup", "s-1")
    fake.start_turn()
    fake.end_turn()
    d = s.start("x")
    d.wait_started(1)
    fake.end_turn("error")
    ep.gone("bye")
    t.join(3)
    assert events == [
        turns.Ready("startup", "s-1"),
        turns.TurnStarted("t1", "s-1", None),
        turns.TurnEnded("t1", "answer", "s-1", None, ()),
        turns.TurnStarted("t2", "s-1", d.id),
        turns.TurnEnded("t2", "error", "s-1", d.id, ()),
        turns.Gone("bye"),
    ]


def test_epoch_counts_the_current_session_and_a_missed_end_closes_as_error(make_endpoint):
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    d = s.start("x")
    d.wait_started(1)
    fake.start_turn()                    # a turn start with the last one still open
    assert d.wait_ended(1) == turns.Ended("t1", "error")
    fake.end_turn()
    assert s.facts().epoch == 2
    ep.ready("clear", "s-2")
    assert (s.facts().session_id, s.facts().epoch) == ("s-2", 0)


# -- holding -------------------------------------------------------------------------


def test_the_holder_holds_a_stop_open_or_lets_it_end(make_endpoint):
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    assert ep.on_stop(turns.StopFacts(turn_id="t1")) is None     # no holder
    seen = []
    h = s.hold(lambda facts: seen.append(facts) or ("reply first" if len(seen) == 1 else None))
    facts = turns.StopFacts(turn_id="t1", session_id="s-1", stop_hook_active=False,
                            task_types=("shell",), crons=0)
    assert ep.on_stop(facts) == "reply first"
    assert ep.on_stop(facts) is None
    assert seen == [facts, facts]
    with pytest.raises(turns.TurnsError, match="held"):
        s.hold(lambda f: None)
    h.close()
    time.sleep(0.1)
    assert ep.on_stop(facts) is None
    s.hold(lambda f: "again").close()


def test_a_late_or_broken_holder_lets_the_turn_end(make_endpoint, turns_env):
    write_config(turns_env, "[hold]\nwait_ms = 100\n")
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    h = s.hold(lambda f: time.sleep(0.5) or "too late")
    t0 = time.monotonic()
    assert ep.on_stop(turns.StopFacts()) is None
    assert time.monotonic() - t0 < 0.4
    h.close()
    time.sleep(0.6)

    def broken(facts):
        raise RuntimeError("bug")

    h = s.hold(broken)
    assert ep.on_stop(turns.StopFacts()) is None
    h.close()


# -- session commands ----------------------------------------------------------------


def test_a_command_needs_consent(make_endpoint, turns_env):
    ep, fake = make_endpoint()
    s = turns.find(session_id="s-1")
    assert s.command("clear") == turns.Refused("not-allowed")
    assert s.command("reboot") == turns.Refused("unsupported")
    write_config(turns_env, '[commands]\nallow = ["exit"]\n')
    assert s.command("clear") == turns.Refused("not-allowed")
    assert s.command("exit") == turns.Ran(None)


def test_a_launch_stamp_grants_commands(make_endpoint):
    env = dict(os.environ, MOXI_TURNS_COMMANDS="clear,exit")
    ep, fake = make_endpoint(env=env)
    s = turns.find(session_id="s-1")
    r = s.command("clear", if_epoch=0)
    assert r == turns.Ran("s-2")
    assert s.refresh()["session_id"] == "s-2"
    assert s.command("exit") == turns.Refused("rate")        # one accepted per 60 s


def test_a_command_runs_only_from_an_idle_snapshot(make_endpoint):
    env = dict(os.environ, MOXI_TURNS_COMMANDS="clear")
    ep, fake = make_endpoint(env=env)
    s = turns.find(session_id="s-1")
    fake.start_turn()
    assert s.command("clear") == turns.Refused("busy-turn")
    fake.end_turn()
    assert s.command("clear", if_epoch=0) == turns.Refused("stale-turn")
    fake.agents = 1
    assert s.command("clear") == turns.Refused("busy-agents")
    fake.agents, fake.draft = 0, True
    assert s.command("clear") == turns.Refused("draft")
    fake.draft = False
    fake.auto_commands = False
    results = []
    t = threading.Thread(target=lambda: results.append(s.command("clear", timeout=3)))
    t.start()
    time.sleep(0.2)
    assert s.command("clear") == turns.Refused("in-flight")
    fake.start_turn()                          # a turn opens before the command takes effect
    fake.run_command(fake.commands[0])
    t.join(3)
    assert results == [turns.Refused("busy-turn")]


def test_a_delivery_the_harness_never_settles_expires_and_frees_the_slot(
        make_endpoint, monkeypatch):
    """b5-1 [L3]: a delivery handed to the adapter that is never tied nor refused answers
    `Lost` past the bound, and the endpoint takes the next one."""
    from moxi_turns import endpoint

    monkeypatch.setattr(endpoint, "PENDING_TTL_S", 0.2)
    ep, fake = make_endpoint(FakeHarness(auto=False))
    s = turns.find(session_id="s-1")
    a = s.deliver("a")                               # in flight, never entered
    b = s.deliver("b")                               # held behind it
    assert s.start("x").wait_started(1) == turns.Refused("busy-turn")
    assert [x.text for x in fake.submitted] == ["a"]
    lost = a.wait_started(2)
    assert isinstance(lost, turns.Lost) and lost.reason.startswith("expired")
    assert a.wait_ended(1) == lost
    assert [x.text for x in fake.submitted] == ["a", "b"]   # the held one went in
    # The adapter's late word about the expired one is refused and changes nothing.
    assert fake.enter(fake.submitted[0]) == "expired"
    assert a.wait_started(1) == lost
    assert fake.enter(fake.submitted[1]) is None
    assert b.wait_started(1) == turns.Started("t1")
    fake.end_turn()
    assert b.wait_ended(1) == turns.Ended("t1", "answer")
    # An untied start expires the same way, and a start after it goes in.
    c = s.start("c")
    assert isinstance(c.wait_started(2), turns.Lost)
    d = s.start("d")
    fake.enter(fake.submitted[-1])
    assert d.wait_started(1) == turns.Started("t2")


def test_a_command_the_harness_never_settles_expires_and_frees_the_slot(
        make_endpoint, monkeypatch):
    """b5-1 [L3]: a command the adapter never settles fails past the bound and frees the
    one command slot; the caller who stopped waiting first got `Failed` already."""
    from moxi_turns import endpoint

    monkeypatch.setattr(endpoint, "COMMAND_RATE_S", 0.0)
    monkeypatch.setattr(endpoint, "PENDING_TTL_S", 0.4)
    env = dict(os.environ, MOXI_TURNS_COMMANDS="clear,exit")
    fake = FakeHarness()
    fake.auto_commands = False
    ep, fake = make_endpoint(fake, env=env)
    s = turns.find(session_id="s-1")
    first = s.command("clear", timeout=0.1)          # the caller stops waiting
    assert isinstance(first, turns.Failed)
    assert not first.reason.startswith("expired")
    assert s.command("exit", timeout=0.1) == turns.Refused("in-flight")
    pending = ep._commands[fake.commands[0].id]
    deadline = time.monotonic() + 3
    while pending.result is None and time.monotonic() < deadline:
        time.sleep(0.02)                             # past the bound: the slot frees
    assert pending.result.reason.startswith("expired")
    # An adapter that comes back late is told the command expired, so it does not run.
    fake.run_command(fake.commands[0])
    assert ep.ledger.session_id == "s-1"
    fake.auto_commands = True
    assert s.command("clear", timeout=1) == turns.Ran("s-2")
    # A caller who waits without a bound gets the expiry, not a hang.
    fake.auto_commands = False
    t0 = time.monotonic()
    late = ep.command("exit", timeout=None)
    assert isinstance(late, turns.Failed) and late.reason.startswith("expired")
    assert time.monotonic() - t0 < 2
