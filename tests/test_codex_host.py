"""The Codex host against synthetic app-server answers and notifications."""

from __future__ import annotations

import itertools
import threading
import time

import pytest

from moxi_turns import codex_hooks
from moxi_turns import codex_protocol as proto
from moxi_turns import codex as codex_module
from moxi_turns import doctor
from moxi_turns.client import Session
from moxi_turns.codex import CodexHarness
from moxi_turns.endpoint import Endpoint
from moxi_turns.results import Ended, Lost, Refused, Started, StopFacts


class FakeAppServer:
    def __init__(self, socket_path, **kwargs):
        self.socket_path = socket_path
        self.alive = False
        self.returncode = None
        self.starts = 0

    def start(self):
        self.alive = True
        self.starts += 1
        return self

    def wait_for_socket(self):
        return True

    def stop(self):
        self.alive = False


class FakeClient:
    def __init__(self, harness, *, on_notification, on_close):
        self.harness = harness
        self.on_notification = on_notification
        self.on_close = on_close
        self.connected = False
        self.calls = []
        if not hasattr(harness, "_fake_turns"):
            harness._fake_turns = itertools.count(1)
        self.turns = harness._fake_turns
        if not hasattr(harness, "_fake_primes"):
            harness._fake_primes = itertools.count(1)
        self.primes = harness._fake_primes
        self.live_turn = None
        self.miss_steer = False
        self.hold_priming = False   # a priming turn completes at once unless held
        self.absorbed = []          # turn/start texts taken into a running turn

    def connect(self):
        self.connected = True
        return self

    def initialize(self):
        return {}

    def close(self):
        self.connected = False

    def notify(self, method, params=None):
        pass

    def call(self, method, params=None, timeout=None):
        self.calls.append((method, params))
        if method == "hooks/list":
            return {"data": [{"hooks": [
                {"key": key, "enabled": True, "trustStatus": codex_hooks.TRUSTED}
                for key in self.harness.hook_keys
            ]}]}
        if method == "thread/start":
            return {"thread": {"id": "thread-1"}}
        if method == "thread/resume":
            return {"thread": {"id": "thread-1", "status": {"type": "idle"}}}
        if method == "turn/start":
            priming = params["input"][0]["text"] == codex_module.PRIMING_TEXT
            if self.live_turn is not None and not priming:
                # Codex takes a turn/start's input into the running turn: it answers
                # with a phantom id that never starts, and sends no turn/started.
                self.absorbed.append(params["input"][0]["text"])
                return {"turn": {"id": f"phantom-{len(self.absorbed)}"}}
            turn_id = f"prime-{next(self.primes)}" if priming else f"turn-{next(self.turns)}"
            self.live_turn = turn_id
            self.on_notification("turn/started", {"turn": {"id": turn_id}}, None)
            if priming and not self.hold_priming:
                self.complete()
            return {"turn": {"id": turn_id}}
        if method == "turn/steer":
            if self.miss_steer:
                self.miss_steer = False
                self.complete()
                raise proto.NoActiveTurn(-32600, "no active turn to steer")
            return {"turnId": self.live_turn}
        if method == "thread/backgroundTerminals/list":
            return {"data": []}
        if method == "thread/turns/list":
            data = [] if self.live_turn is None else [
                {"id": self.live_turn, "status": "inProgress"}
            ]
            return {"data": data}
        raise AssertionError(f"unexpected app-server call {method}")

    def complete(self, status="completed"):
        turn_id, self.live_turn = self.live_turn, None
        self.on_notification(
            "turn/completed", {"turn": {"id": turn_id, "status": status}}, None
        )

    def status(self, kind):
        self.on_notification("thread/status/changed",
                             {"threadId": "thread-1", "status": {"type": kind}}, None)


def _turn_texts(app):
    """The texts of the turns a client started, the priming turn left out."""
    return [params["input"][0]["text"] for method, params in app.calls
            if method == "turn/start"
            and params["input"][0]["text"] != codex_module.PRIMING_TEXT]


def _host(turns_env):
    harness = None

    def clients(**kwargs):
        client = FakeClient(harness, **kwargs)
        made.append(client)
        return client

    made = []
    harness = CodexHarness(
        turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
        client_factory=clients, app_server_factory=FakeAppServer,
    )
    endpoint = Endpoint(harness, session_id=None).open()
    harness.start()
    return endpoint, harness, made[0]


def test_a_submitted_delivery_is_tied_to_the_app_servers_turn(turns_env):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        assert session.session_id == "thread-1"
        delivery = session.start("hello")
        assert delivery.wait_started(2).turn_id == "turn-1"
        app.complete()
        ended = delivery.wait_ended(2)
        assert (ended.turn_id, ended.outcome) == ("turn-1", "answer")
    finally:
        endpoint.stop("the test ended")


def test_a_steering_delivery_uses_turn_steer_and_ends_with_that_turn(turns_env):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        first = session.start("first")
        assert first.wait_started(2).turn_id == "turn-1"
        steered = session.deliver("now", steer=True)
        assert steered.wait_started(2).turn_id == "turn-1"
        assert any(method == "turn/steer" for method, _ in app.calls)
        app.complete("interrupted")
        assert steered.wait_ended(2).outcome == "aborted"
    finally:
        endpoint.stop("the test ended")


def test_stopping_the_endpoint_reaps_the_owned_children_and_runtime_dir(turns_env):
    endpoint, harness, _ = _host(turns_env)
    directory = endpoint.dir
    endpoint.stop("the test ended")
    assert not harness.app_server.alive
    assert not directory.exists()


def test_a_steer_that_races_the_turn_end_becomes_one_new_turn(turns_env):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        first = session.start("first")
        assert first.wait_started(2).turn_id == "turn-1"
        app.miss_steer = True
        delivery = session.deliver("after the boundary", steer=True)
        assert delivery.wait_started(2).turn_id == "turn-2"
        app.complete()
        assert delivery.wait_ended(2).turn_id == "turn-2"
        assert _turn_texts(app) == ["first", "after the boundary"]
    finally:
        endpoint.stop("the test ended")


def test_a_plain_steer_that_races_the_turn_end_refuses_idle(turns_env):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        first = session.start("first")
        assert first.wait_started(2).turn_id == "turn-1"
        app.miss_steer = True
        delivery = session.steer("too late")
        assert delivery.wait_started(2).reason == "idle"
        assert delivery.wait_ended(2).reason == "idle"
        assert _turn_texts(app) == ["first"]
    finally:
        endpoint.stop("the test ended")


def test_supervision_reconnects_and_resumes_the_same_thread(turns_env):
    endpoint, harness, first = _host(turns_env)
    try:
        harness.app_server.alive = False
        first.connected = False
        deadline = time.monotonic() + 3
        while harness.ws is first and time.monotonic() < deadline:
            time.sleep(0.02)
        assert harness.ws is not first
        assert ("thread/resume", {"threadId": "thread-1", "excludeTurns": True,
                                  "approvalPolicy": "never"}) \
            in harness.ws.calls
        assert harness.app_server.starts == 2
        assert endpoint.hello()["session_id"] == "thread-1"
    finally:
        endpoint.stop("the test ended")


@pytest.mark.parametrize("key", sorted(codex_module.OWNED_OPTIONS))
def test_owned_options_are_refused_before_the_endpoint_opens(monkeypatch, turns_env, key):
    opened = []
    monkeypatch.setattr(Endpoint, "open", lambda self: opened.append(self))
    with pytest.raises(ValueError, match=key):
        codex_module.launch(turns_env, window=False, options={key: "caller value"})
    assert opened == []


def test_options_and_verifier_reach_start_and_every_resume(turns_env):
    answers = []
    harness = None
    made = []

    def clients(**kwargs):
        client = FakeClient(harness, **kwargs)
        made.append(client)
        return client

    options = {"model": "sample", "sandbox": "workspace-write"}
    harness = CodexHarness(
        turns_env, instructions="rules", options=options,
        verify=lambda answer: answers.append(answer) or None,
        window=False, ping=["/env/bin/moxi-turns-ping"], client_factory=clients,
        app_server_factory=FakeAppServer,
    )
    endpoint = Endpoint(harness).open()
    try:
        harness.hook_server.start()
        harness._connect()
        made[-1].close()
        harness._connect()
        harness.app_server.alive = False
        made[-1].close()
        harness._connect()
        starts = [params for method, params in made[0].calls if method == "thread/start"]
        resumes = [params for client in made[1:] for method, params in client.calls
                   if method == "thread/resume"]
        assert starts == [{**options, "cwd": str(turns_env), "approvalPolicy": "never",
                           "developerInstructions": "rules"}]
        assert resumes == [{**options, "threadId": "thread-1", "excludeTurns": True,
                            "approvalPolicy": "never",
                            "developerInstructions": "rules"}] * 2
        assert len(answers) == 3
    finally:
        endpoint.stop("the test ended")


def test_unaccepted_resume_gates_input_and_publishes_its_reason(turns_env):
    resume_entered = threading.Event()
    accept_resume = threading.Event()
    calls = 0

    def verify(answer):
        nonlocal calls
        calls += 1
        if calls == 2:
            resume_entered.set()
            assert accept_resume.wait(2)
        return None

    endpoint, harness, first = _host(turns_env)
    harness.verify = verify
    # The helper's first load happened before the verifier was installed.
    calls = 1
    first.close()
    thread = threading.Thread(target=harness._connect)
    thread.start()
    assert resume_entered.wait(2)
    session = Session(endpoint.dir, endpoint.hello())
    assert session.start("too early").wait_started(1).reason == "loading"
    assert session.steer("too early").wait_started(1).reason == "loading"
    queued = session.deliver("after acceptance")
    try:
        with pytest.raises(TimeoutError):
            queued.wait_started(0.05)
        accept_resume.set()
        thread.join(2)
        assert queued.wait_started(2).turn_id == "turn-1"
        assert "trouble" not in endpoint.hello()
    finally:
        accept_resume.set()
        endpoint.stop("the test ended")


def test_steer_first_delivery_waits_at_gate_while_old_turn_is_open(turns_env):
    resume_entered = threading.Event()
    accept_resume = threading.Event()
    endpoint, harness, first = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    running = session.start("running")
    assert running.wait_started(2).turn_id == "turn-1"

    def verify(answer):
        resume_entered.set()
        assert accept_resume.wait(2)

    harness.verify = verify
    first.close()
    thread = threading.Thread(target=harness._connect)
    thread.start()
    assert resume_entered.wait(2)
    queued = session.deliver("after reload", steer=True)
    try:
        with pytest.raises(TimeoutError):
            queued.wait_started(0.05)
        accept_resume.set()
        thread.join(2)
        assert running.wait_ended(2).outcome == "error"
        assert queued.wait_started(2).turn_id == "turn-2"
    finally:
        accept_resume.set()
        endpoint.stop("the test ended")


def test_fresh_window_primes_before_releasing_a_gated_delivery(turns_env):
    harness = None
    made = []

    def clients(**kwargs):
        client = FakeClient(harness, **kwargs)
        client.hold_priming = True
        made.append(client)
        return client

    harness = CodexHarness(
        turns_env, window=True, ping=["/env/bin/moxi-turns-ping"],
        client_factory=clients, app_server_factory=FakeAppServer,
    )
    endpoint = Endpoint(harness).open()
    session = Session(endpoint.dir, endpoint.hello())
    queued = session.deliver("real work")
    try:
        harness.hook_server.start()
        harness._connect()
        assert made[0].live_turn == "prime-1"
        with pytest.raises(TimeoutError):
            queued.wait_started(0.05)
        made[0].complete()
        assert queued.wait_started(2).turn_id == "turn-1"
    finally:
        endpoint.stop("the test ended")


def test_a_headless_start_primes_its_fresh_thread_and_a_resume_does_not(turns_env):
    # A fresh thread has no rollout until its first turn, and `thread/resume`
    # refuses it, so a thread launched without a window is primed too.
    endpoint, harness, first = _host(turns_env)
    try:
        primes = [params for method, params in first.calls if method == "turn/start"]
        assert [row["input"][0]["text"] for row in primes] == [codex_module.PRIMING_TEXT]
        assert harness.active_turn_id is None and harness.generation.ready.is_set()
        first.close()
        harness._connect()
        second = harness.ws
        assert [method for method, _ in second.calls if method == "turn/start"] == []
    finally:
        endpoint.stop("the test ended")


def test_start_warning_runs_but_resume_warning_fails_and_stops_its_process(turns_env):
    verdicts = iter(["start differs", "resume differs"])
    harness = None
    made = []

    def clients(**kwargs):
        client = FakeClient(harness, **kwargs)
        made.append(client)
        return client

    harness = CodexHarness(
        turns_env, window=False, verify=lambda answer: next(verdicts),
        ping=["/env/bin/moxi-turns-ping"], client_factory=clients,
        app_server_factory=FakeAppServer,
    )
    endpoint = Endpoint(harness).open()
    try:
        harness.hook_server.start()
        harness._connect()
        assert endpoint.hello()["trouble"] == "start differs"
        row = next(row for row in doctor._endpoint_rows()
                   if row.name.startswith("endpoint codex"))
        assert not row.ok and "start differs" in row.detail
        made[-1].close()
        with pytest.raises(RuntimeError, match="resume differs"):
            harness._connect()
        assert not harness.app_server.alive
        harness._load_failed(RuntimeError("resume differs"))
        assert endpoint.hello()["trouble"] == "resume differs"
    finally:
        endpoint.stop("the test ended")


def test_real_host_builds_stop_facts_through_its_hook_server(monkeypatch, turns_env):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    hold = session.hold(lambda facts: "wait" if facts.task_types else None)
    delivery = session.start("work")
    assert delivery.wait_started(2).turn_id == "turn-1"
    try:
        import io
        import json
        import sys

        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
            "session_id": "thread-1", "turn_id": "turn-1",
            "background_tasks": [{"type": "background-terminal"}],
            "stop_hook_active": False,
        })))
        output = io.StringIO()
        monkeypatch.setattr(sys, "stdout", output)
        assert codex_hooks.main([
            "--socket", str(harness.hook_server.socket_path), "Stop",
        ]) == 0
        assert json.loads(output.getvalue()) == {"decision": "block", "reason": "wait"}
        assert harness.stop_facts({"turn_id": "turn-1", "task_types": ["x"]}) == \
            StopFacts("turn-1", "thread-1", None, ("x",), 0)
    finally:
        hold.close()
        endpoint.stop("the test ended")


def test_phantom_turn_start_id_is_never_reported(monkeypatch, turns_env):
    endpoint, harness, app = _host(turns_env)
    original = app.call

    def call(method, params=None, timeout=None):
        if method == "turn/start":
            app.calls.append((method, params))
            return {"turn": {"id": "phantom"}}
        return original(method, params, timeout)

    app.call = call
    monkeypatch.setattr(codex_module, "TURN_STARTED_WAIT", 0.01)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        # The request went out and Codex answered it, so the text may be in: not a
        # refusal. Nothing names a turn to tie it to, so it reads lost at once.
        delivery = session.start("hello")
        started = delivery.wait_started(1)
        assert isinstance(started, Lost) and started.untied
        assert delivery.wait_ended(1) == started
        assert not endpoint.facts().turn_open
    finally:
        endpoint.stop("the test ended")


def test_exhausted_steer_mismatches_refuse_instead_of_claiming_delivery(turns_env):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    first = session.start("first")
    assert first.wait_started(2).turn_id == "turn-1"
    original = app.call
    count = itertools.count(2)

    def call(method, params=None, timeout=None):
        if method == "turn/steer":
            found = f"turn-{next(count)}"
            raise proto.ActiveTurnMismatch(-32600, "changed", params["expectedTurnId"],
                                           found)
        return original(method, params, timeout)

    app.call = call
    try:
        delivery = session.steer("follow-up")
        assert delivery.wait_started(2).reason == "gone"
        assert delivery.wait_ended(2).reason == "gone"
        assert _turn_texts(app) == ["first"]
    finally:
        endpoint.stop("the test ended")


@pytest.mark.parametrize("mode", ["raise", "timeout"])
def test_verifier_fault_fails_load_without_publishing(mode, turns_env):
    def verify(answer):
        if mode == "raise":
            raise LookupError("bad verifier")
        time.sleep(0.2)

    harness = None
    made = []

    def clients(**kwargs):
        client = FakeClient(harness, **kwargs)
        made.append(client)
        return client

    harness = CodexHarness(
        turns_env, window=False, verify=verify, verify_wait=0.02,
        ping=["/env/bin/moxi-turns-ping"], client_factory=clients,
        app_server_factory=FakeAppServer,
    )
    endpoint = Endpoint(harness).open()
    try:
        harness.hook_server.start()
        with pytest.raises(RuntimeError):
            harness._connect()
        assert harness.ws is None
        assert harness.generation is None
        assert harness.session_id is None
        assert not harness.app_server.alive
        assert not made[0].connected
    finally:
        endpoint.stop("the test ended")


def test_dead_process_open_turn_settles_error_before_resume(turns_env):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    delivery = session.start("work")
    assert delivery.wait_started(2).turn_id == "turn-1"
    harness.app_server.alive = False
    app.close()
    harness._connect()
    try:
        assert delivery.wait_ended(2).outcome == "error"
        assert not endpoint.facts().turn_open
    finally:
        endpoint.stop("the test ended")


def test_backoff_grows_caps_and_jitters():
    class Lowest:
        @staticmethod
        def uniform(low, high):
            assert (low, high) == (-0.2, 0.2)
            return -0.2

    delays = codex_module._backoff_delays(Lowest())
    assert [next(delays) for _ in range(8)] == [0.4, 0.8, 1.6, 3.2, 6.4, 12.8,
                                                   24.0, 24.0]


def test_headless_first_failure_returns_and_retries_until_accepted(turns_env):
    harness = None
    attempts = 0
    backoff_starts = 0

    def clients(**kwargs):
        nonlocal attempts
        attempts += 1
        client = FakeClient(harness, **kwargs)
        if attempts == 1:
            client.connect = lambda: (_ for _ in ()).throw(OSError("offline"))
        return client

    def backoffs():
        nonlocal backoff_starts
        backoff_starts += 1
        return itertools.repeat(0.001)

    harness = CodexHarness(
        turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
        client_factory=clients, app_server_factory=FakeAppServer,
        backoff_factory=backoffs,
    )
    endpoint = Endpoint(harness).open()
    try:
        harness.start()
        assert harness._accepted.wait(2)
        assert harness.session_id == "thread-1"
        assert not endpoint.is_gone
        assert "trouble" not in endpoint.hello()
        assert attempts >= 2
        deadline = time.monotonic() + 1
        while backoff_starts < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert backoff_starts >= 2  # initial retry sequence, then reset on acceptance
    finally:
        endpoint.stop("the test ended")


def test_headless_first_accepted_load_after_a_failure_names_the_thread_in_ready(turns_env):
    # A no-window launch returns once its first load was tried. When that load
    # failed, the caller has no thread id; the first accepted load gives it in `Ready`.
    harness = None
    attempts = 0
    retry = threading.Event()

    def clients(**kwargs):
        nonlocal attempts
        attempts += 1
        client = FakeClient(harness, **kwargs)
        if attempts == 1:
            client.connect = lambda: (_ for _ in ()).throw(OSError("offline"))
        else:
            assert retry.wait(5)
        return client

    harness = CodexHarness(
        turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
        client_factory=clients, app_server_factory=FakeAppServer,
        backoff_factory=lambda: itertools.repeat(0.001),
    )
    endpoint = Endpoint(harness, session_id=None).open()
    events = []
    try:
        harness.start()
        session = Session(endpoint.dir, endpoint.hello())
        assert session.session_id is None
        watcher = threading.Thread(target=lambda: events.extend(session.watch()),
                                   daemon=True)
        watcher.start()
        time.sleep(0.2)
        retry.set()
        deadline = time.monotonic() + 2
        while (not any(type(e).__name__ == "Ready" for e in events)
               and time.monotonic() < deadline):
            time.sleep(0.02)
        readies = [(e.source, e.session_id) for e in events
                   if type(e).__name__ == "Ready"]
        assert readies == [("startup", "thread-1")]
    finally:
        retry.set()
        endpoint.stop("the test ended")


def test_untrusted_hooks_give_up_on_the_first_load(turns_env):
    harness = None

    class UntrustedClient(FakeClient):
        def call(self, method, params=None, timeout=None):
            if method == "hooks/list":
                return {"data": []}
            return super().call(method, params, timeout)

    def clients(**kwargs):
        return UntrustedClient(harness, **kwargs)

    harness = CodexHarness(
        turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
        client_factory=clients, app_server_factory=FakeAppServer,
    )
    endpoint = Endpoint(harness).open()
    with pytest.raises(codex_hooks.HooksUntrusted):
        harness.start()
    try:
        assert harness._stop.is_set()
        assert not harness.app_server.alive
        assert harness.ws is None
    finally:
        endpoint.stop("the test ended")


def test_close_cleans_a_candidate_started_during_shutdown(turns_env):
    start_entered = threading.Event()
    release_start = threading.Event()
    made = []
    harness = None

    class BlockingAppServer(FakeAppServer):
        def start(self):
            self.alive = True
            self.starts += 1
            start_entered.set()
            assert release_start.wait(2)
            return self

    def clients(**kwargs):
        client = FakeClient(harness, **kwargs)
        made.append(client)
        return client

    harness = CodexHarness(
        turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
        client_factory=clients, app_server_factory=BlockingAppServer,
    )
    endpoint = Endpoint(harness).open()
    harness.hook_server.start()
    connect_errors = []
    connecting = threading.Thread(
        target=lambda: _capture_error(harness._connect, connect_errors)
    )
    connecting.start()
    assert start_entered.wait(2)
    stopping = threading.Thread(target=harness.close)
    stopping.start()
    assert harness._stop.wait(2)
    release_start.set()
    connecting.join(2)
    stopping.join(2)
    endpoint.stop("shutdown race")
    assert connect_errors
    assert not harness.app_server.alive
    assert made and not made[0].connected
    assert harness.generation is None
    assert not endpoint.dir.exists()


def _capture_error(call, errors):
    try:
        call()
    except BaseException as exc:
        errors.append(exc)


def test_steer_first_delivery_held_at_gate_steers_the_turn_the_load_shows(turns_env):
    harness = None
    made = []
    hold, release = threading.Event(), threading.Event()

    class ActiveResume(FakeClient):
        def call(self, method, params=None, timeout=None):
            if method == "thread/resume":
                self.calls.append((method, params))
                hold.set()
                assert release.wait(5)
                return {"thread": {"id": "thread-1", "status": {"type": "active"}}}
            return super().call(method, params, timeout)

    def clients(**kwargs):
        client = (ActiveResume if made else FakeClient)(harness, **kwargs)
        if made:
            client.live_turn = made[0].live_turn
        made.append(client)
        return client

    harness = CodexHarness(turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
                           client_factory=clients, app_server_factory=FakeAppServer)
    endpoint = Endpoint(harness, session_id=None).open()
    harness.start()
    session = Session(endpoint.dir, endpoint.hello())
    try:
        running = session.start("running")
        assert running.wait_started(2).turn_id == "turn-1"
        made[0].close()
        thread = threading.Thread(target=harness._connect)
        thread.start()
        assert hold.wait(2)
        queued = session.deliver("urgent", steer=True)
        release.set()
        thread.join(5)
        assert queued.wait_started(2).turn_id == "turn-1"
        assert [p["input"][0]["text"] for m, p in made[1].calls
                if m == "turn/steer"] == ["urgent"]
        made[1].complete()
        assert queued.wait_ended(2).turn_id == "turn-1"
        assert not [m for m, _ in made[1].calls if m == "turn/start"]
    finally:
        release.set()
        endpoint.stop("the test ended")


def test_a_refused_priming_turn_closes_its_candidate_and_retries(turns_env):
    harness = None
    made = []
    refusals = [proto.CodexError(-32603, "turn/start refused")]

    class RefusePrime(FakeClient):
        def call(self, method, params=None, timeout=None):
            if method == "turn/start" and refusals:
                raise refusals.pop()
            return super().call(method, params, timeout)

    class NoWindow:
        def __init__(self, host, **kwargs):
            pass

        def start(self):
            return self

        def stop(self):
            pass

    def clients(**kwargs):
        client = RefusePrime(harness, **kwargs)
        made.append(client)
        return client

    harness = CodexHarness(turns_env, window=True, ping=["/env/bin/moxi-turns-ping"],
                           client_factory=clients, app_server_factory=FakeAppServer,
                           window_factory=NoWindow,
                           backoff_factory=lambda: iter(lambda: 0.01, None))
    endpoint = Endpoint(harness, session_id=None).open()
    try:
        harness.start()
        assert not made[0].connected
        assert len(made) == 2 and harness.ws is made[1]
        assert harness.generation.ready.is_set()
        assert "trouble" not in endpoint.hello()
    finally:
        endpoint.stop("the test ended")


def test_a_delivery_made_during_the_priming_turn_waits_at_the_gate(turns_env):
    # The priming turn is part of the load, so the gate stays shut while it runs.
    # A delivery made then is not handed to the candidate; it starts on the next load.
    harness = None
    made = []
    in_priming, release = threading.Event(), threading.Event()

    class RefusePrime(FakeClient):
        def call(self, method, params=None, timeout=None):
            if (method == "turn/start"
                    and params["input"][0]["text"] == codex_module.PRIMING_TEXT):
                self.calls.append((method, params))
                in_priming.set()
                assert release.wait(5)
                raise proto.CodexError(-32603, "turn/start refused")
            return super().call(method, params, timeout)

    def clients(**kwargs):
        client = (FakeClient if made else RefusePrime)(harness, **kwargs)
        made.append(client)
        return client

    harness = CodexHarness(turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
                           client_factory=clients, app_server_factory=FakeAppServer,
                           backoff_factory=lambda: iter(lambda: 0.01, None))
    endpoint = Endpoint(harness, session_id=None).open()
    session = Session(endpoint.dir, endpoint.hello())
    starter = threading.Thread(target=harness.start, daemon=True)
    try:
        starter.start()
        assert in_priming.wait(2)
        assert harness.loading()
        queued = session.deliver("mid-load")
        time.sleep(0.1)
        release.set()
        assert queued.wait_started(2).turn_id == "turn-1"
        assert [params["input"][0]["text"] for method, params in made[0].calls
                if method == "turn/start"] == [codex_module.PRIMING_TEXT]
        assert _turn_texts(made[1]) == ["mid-load"]
    finally:
        release.set()
        endpoint.stop("the test ended")
        starter.join(2)


def test_accepted_start_and_resume_each_raise_one_ready(turns_env):
    harness = None
    made = []

    def clients(**kwargs):
        client = FakeClient(harness, **kwargs)
        made.append(client)
        return client

    harness = CodexHarness(turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
                           client_factory=clients, app_server_factory=FakeAppServer)
    endpoint = Endpoint(harness, session_id=None).open()
    events = []
    session = Session(endpoint.dir, endpoint.hello())
    watcher = threading.Thread(target=lambda: events.extend(session.watch()), daemon=True)
    watcher.start()
    time.sleep(0.2)
    try:
        harness.start()
        made[-1].close()
        harness._connect()
        # The resume's own SessionStart hook asks the listeners but raises no second Ready.
        assert endpoint.ready("resume", "thread-1", announce=False) is None
        def readies():
            return [(e.source, e.session_id) for e in events
                    if type(e).__name__ == "Ready"]

        deadline = time.monotonic() + 2
        while len(readies()) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
        time.sleep(0.1)
        assert readies() == [
            ("startup", "thread-1"), ("resume", "thread-1"),
        ]
        # The start's Ready follows its priming turn, which belongs to the load.
        names = [type(e).__name__ for e in events]
        assert names.index("Ready") > names.index("TurnStarted")
    finally:
        endpoint.stop("the test ended")
        watcher.join(2)


def test_a_refused_priming_turn_raises_no_ready(turns_env):
    # Only an accepted load raises Ready, and a refused priming turn fails the
    # start. The refused thread has no rollout, so `thread/resume` would refuse it
    # for good; the retry starts a new thread instead. The first thread's id, already
    # named by `identify`, is abandoned, and the launch's one Ready names the new one.
    harness = None
    made = []

    class RefusePrime(FakeClient):
        def call(self, method, params=None, timeout=None):
            if method == "turn/start":
                raise proto.CodexError(-32603, "turn/start refused")
            return super().call(method, params, timeout)

    class NewThread(FakeClient):
        def call(self, method, params=None, timeout=None):
            if method == "thread/resume":
                self.calls.append((method, params))
                raise proto.CodexError(
                    -32600, f"no rollout found for thread id {params['threadId']}")
            if method == "thread/start":
                self.calls.append((method, params))
                return {"thread": {"id": "thread-2"}}
            return super().call(method, params, timeout)

    def clients(**kwargs):
        client = (NewThread if made else RefusePrime)(harness, **kwargs)
        made.append(client)
        return client

    harness = CodexHarness(turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
                           client_factory=clients, app_server_factory=FakeAppServer,
                           backoff_factory=lambda: iter(lambda: 0.01, None))
    endpoint = Endpoint(harness, session_id=None).open()
    events = []
    session = Session(endpoint.dir, endpoint.hello())
    watcher = threading.Thread(target=lambda: events.extend(session.watch()), daemon=True)
    watcher.start()
    time.sleep(0.2)
    try:
        harness.start()
        assert harness._accepted.wait(2)
        time.sleep(0.2)
        assert len(made) == 2
        assert [m for m, _ in made[1].calls if m.startswith("thread/")] == ["thread/start"]
        assert harness.session_id == "thread-2"
        assert [(e.source, e.session_id) for e in events
                if type(e).__name__ == "Ready"] == [("startup", "thread-2")]
        hello = endpoint.hello()
        assert hello["session_id"] == "thread-2" and hello["earlier"] == ["thread-1"]
        assert endpoint.facts().session_id == "thread-2"
    finally:
        endpoint.stop("the test ended")
        watcher.join(2)


@pytest.mark.parametrize("answer", [True, {"sandbox": "differs"}, ["differs"]])
def test_a_non_string_verify_answer_fails_the_load(turns_env, answer):
    endpoint, harness, first = _host(turns_env)
    try:
        harness.verify = lambda result: answer
        first.close()
        with pytest.raises(RuntimeError, match="not a reason"):
            harness._connect()
        assert not harness.app_server.alive and harness.loading()
    finally:
        endpoint.stop("the test ended")


def test_gate_held_steer_goes_into_a_different_turn_the_load_shows(turns_env):
    # The accepted resume shows a running turn other than the one the ledger holds.
    # The old turn ends `error`, and nothing held at the gate is started as a new turn
    # in between: the steer-first delivery goes into the turn the load shows.
    harness = None
    made = []
    hold, release = threading.Event(), threading.Event()

    class OtherActiveResume(FakeClient):
        def call(self, method, params=None, timeout=None):
            if method == "thread/resume":
                self.calls.append((method, params))
                hold.set()
                assert release.wait(5)
                self.live_turn = "turn-9"
                return {"thread": {"id": "thread-1", "status": {"type": "active"}}}
            return super().call(method, params, timeout)

    def clients(**kwargs):
        client = (OtherActiveResume if made else FakeClient)(harness, **kwargs)
        made.append(client)
        return client

    harness = CodexHarness(turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
                           client_factory=clients, app_server_factory=FakeAppServer)
    endpoint = Endpoint(harness, session_id=None).open()
    harness.start()
    session = Session(endpoint.dir, endpoint.hello())
    try:
        running = session.start("running")
        assert running.wait_started(2).turn_id == "turn-1"
        made[0].close()
        thread = threading.Thread(target=harness._connect)
        thread.start()
        assert hold.wait(2)
        queued = session.deliver("urgent", steer=True)
        release.set()
        thread.join(5)
        assert running.wait_ended(2).outcome == "error"
        assert queued.wait_started(2).turn_id == "turn-9"
        assert not [m for m, _ in made[1].calls if m == "turn/start"]
        assert [p["input"][0]["text"] for m, p in made[1].calls
                if m == "turn/steer"] == ["urgent"]
        made[1].complete()
        assert queued.wait_ended(2).turn_id == "turn-9"
        assert endpoint.facts().turn_open is False
    finally:
        release.set()
        endpoint.stop("the test ended")


# -- A Codex turn closes only on Codex's own word ------------------------------------


def _hook(monkeypatch, harness, event, turn_id):
    """Send one Codex hook ping, as the hook command would, and return what it prints."""
    import io
    import json
    import sys

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "session_id": harness.session_id, "turn_id": turn_id, "stop_hook_active": False,
    })))
    output = io.StringIO()
    monkeypatch.setattr(sys, "stdout", output)
    assert codex_hooks.main(["--socket", str(harness.hook_server.socket_path), event]) == 0
    return output.getvalue()


def test_a_steer_after_an_unblocked_stop_starts_a_new_turn_after_the_completion(
        monkeypatch, turns_env):
    # Stop is Codex asking whether the turn may end, not its end, so the ledger keeps
    # the turn open until turn/completed. A live probe showed Codex taking a steer in
    # the last ms before turn/completed and never answering it, so after an unblocked
    # Stop the turn is ending: a steer-first delivery waits and starts a new turn once
    # Codex completes this one.
    monkeypatch.setattr(codex_module, "TURN_STARTED_WAIT", 0.01)
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        first = session.start("first")
        assert first.wait_started(2).turn_id == "turn-1"
        assert _hook(monkeypatch, harness, "Stop", "turn-1") == ""
        assert endpoint.facts().turn_open
        delivery = session.deliver("x", steer=True)
        with pytest.raises(TimeoutError):
            delivery.wait_started(0.1)
        assert endpoint.facts().turn_open
        assert not [m for m, _ in app.calls if m == "turn/steer"]
        assert _turn_texts(app) == ["first"] and app.absorbed == []
        app.complete()
        assert first.wait_ended(2) == Ended("turn-1", "answer")
        assert delivery.wait_started(2) == Started("turn-2")
        assert _turn_texts(app) == ["first", "x"] and app.absorbed == []
        assert not [m for m, _ in app.calls if m == "turn/steer"]
        app.complete()
        assert delivery.wait_ended(2) == Ended("turn-2", "answer")
        assert not endpoint.facts().turn_open
    finally:
        endpoint.stop("the test ended")


def test_a_plain_steer_after_an_unblocked_stop_reads_idle(monkeypatch, turns_env):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        first = session.start("first")
        assert first.wait_started(2).turn_id == "turn-1"
        assert _hook(monkeypatch, harness, "Stop", "turn-1") == ""
        delivery = session.steer("too late")
        assert delivery.wait_started(2) == delivery.wait_ended(2) == Refused("idle")
        assert endpoint.facts().turn_open
        assert not [m for m, _ in app.calls if m == "turn/steer"]
        app.complete()
        assert first.wait_ended(2) == Ended("turn-1", "answer")
    finally:
        endpoint.stop("the test ended")


def test_a_blocked_stop_leaves_the_turn_open_to_steers(monkeypatch, turns_env):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    hold = session.hold(lambda facts: "reply first")
    try:
        first = session.start("first")
        assert first.wait_started(2).turn_id == "turn-1"
        assert "reply first" in _hook(monkeypatch, harness, "Stop", "turn-1")
        delivery = session.deliver("x", steer=True)
        assert delivery.wait_started(2) == Started("turn-1")
        assert [p["input"][0]["text"] for m, p in app.calls if m == "turn/steer"] == ["x"]
        app.complete()
        assert delivery.wait_ended(2) == Ended("turn-1", "answer")
    finally:
        hold.close()
        endpoint.stop("the test ended")


def test_a_gate_held_steer_first_delivery_waits_out_an_ending_turn(
        monkeypatch, turns_env):
    # Endpoint._pump hands a steer-first delivery held at the load gate to the open
    # turn. While that turn is ending it must neither steer into it nor go to submit
    # (a turn/start Codex would absorb into the ending turn): it waits for the end.
    hold, release = threading.Event(), threading.Event()

    class SlowActiveResume(_ActiveResume):
        def call(self, method, params=None, timeout=None):
            if method == "thread/resume":
                hold.set()
                assert release.wait(5)
            return super().call(method, params, timeout)

    harness = None
    made = []

    def clients(**kwargs):
        client = (SlowActiveResume if made else FakeClient)(harness, **kwargs)
        if made:
            client.live_turn = made[0].live_turn
        made.append(client)
        return client

    harness = CodexHarness(turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
                           client_factory=clients, app_server_factory=FakeAppServer)
    endpoint = Endpoint(harness, session_id=None).open()
    harness.start()
    session = Session(endpoint.dir, endpoint.hello())
    try:
        running = session.start("running")
        assert running.wait_started(2).turn_id == "turn-1"
        assert _hook(monkeypatch, harness, "Stop", "turn-1") == ""
        made[0].close()
        thread = threading.Thread(target=harness._connect)
        thread.start()
        assert hold.wait(2)
        queued = session.deliver("urgent", steer=True)   # held at the load gate
        release.set()
        thread.join(5)
        with pytest.raises(TimeoutError):
            queued.wait_started(0.2)
        assert not [m for m, _ in made[1].calls if m in ("turn/steer", "turn/start")]
        assert made[1].absorbed == [] and endpoint.facts().turn_open
        made[1].complete()
        assert running.wait_ended(2) == Ended("turn-1", "answer")
        assert queued.wait_started(2) == Started("turn-2")
        assert _turn_texts(made[1]) == ["urgent"]
    finally:
        release.set()
        endpoint.stop("the test ended")


def test_the_stop_answer_waits_for_a_steer_codex_has_not_yet_answered(
        monkeypatch, turns_env):
    # A turn/steer already on its way when the hold answers Stop must reach Codex
    # before the Stop answer does: Codex then takes it inside the hook, before its end
    # check, and answers it. None may land in the gap after.
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    first = session.start("first")
    assert first.wait_started(2).turn_id == "turn-1"
    steering, gate = threading.Event(), threading.Event()
    original = app.call
    order = []

    def call(method, params=None, timeout=None):
        if method == "turn/steer":
            steering.set()
            assert gate.wait(5)
            order.append("steer answered")
        return original(method, params, timeout)

    app.call = call
    try:
        holder = []
        thread = threading.Thread(
            target=lambda: holder.append(session.deliver("x", steer=True)))
        thread.start()
        assert steering.wait(2)
        timer = threading.Timer(0.3, gate.set)    # Codex answers the steer later
        timer.start()
        assert _hook(monkeypatch, harness, "Stop", "turn-1") == ""   # the ping runs here
        order.append("stop answered")
        thread.join(3)
        assert order == ["steer answered", "stop answered"]
        delivery = holder[0]
        assert delivery.wait_started(2) == Started("turn-1")
        app.complete()
        assert delivery.wait_ended(2) == Ended("turn-1", "answer")
    finally:
        gate.set()
        endpoint.stop("the test ended")


def test_a_steer_the_endpoint_tied_before_the_stop_and_sent_after_it_waits(
        monkeypatch, turns_env):
    # The other side of that race: the endpoint put the steer on turn-1 before the Stop,
    # but its turn/steer had not gone out when the Stop was answered. It must not go
    # out after: it moves to the deliver path and starts a new turn after the end.
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    first = session.start("first")
    assert first.wait_started(2).turn_id == "turn-1"
    entered, gate = threading.Event(), threading.Event()
    original = harness._inject

    def inject(text):
        entered.set()
        assert gate.wait(5)
        return original(text)

    harness._inject = inject
    try:
        holder = []
        steer = threading.Thread(
            target=lambda: holder.append(session.deliver("x", steer=True)))
        steer.start()
        assert entered.wait(2)
        assert _hook(monkeypatch, harness, "Stop", "turn-1") == ""
        gate.set()
        steer.join(3)
        delivery = holder[0]
        with pytest.raises(TimeoutError):
            delivery.wait_started(0.1)
        assert not [m for m, _ in app.calls if m == "turn/steer"]
        app.complete()
        assert first.wait_ended(2) == Ended("turn-1", "answer")
        assert delivery.wait_started(2) == Started("turn-2")
        assert _turn_texts(app) == ["first", "x"]
    finally:
        gate.set()
        endpoint.stop("the test ended")


def test_a_start_absorbed_into_a_turn_already_reported_reads_lost_at_once(
        monkeypatch, turns_env):
    # The reported-turn guard: the ledger is closed while Codex still runs turn-1, which
    # the host has already reported. A turn/start Codex absorbs into it cannot be tied,
    # and reporting turn-1 again would close it as error: the delivery reads lost.
    monkeypatch.setattr(codex_module, "TURN_STARTED_WAIT", 0.01)
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        first = session.start("first")
        assert first.wait_started(2).turn_id == "turn-1"
        harness.settle_turn("turn-1", "error")      # the ledger closes; Codex runs on
        assert first.wait_ended(2) == Ended("turn-1", "error")
        epoch = endpoint.facts().epoch
        delivery = session.deliver("y")
        started = delivery.wait_started(2)
        assert isinstance(started, Lost) and started.untied
        assert delivery.wait_ended(1) == started
        assert app.absorbed == ["y"]
        facts = endpoint.facts()
        assert not facts.turn_open and facts.epoch == epoch
    finally:
        endpoint.stop("the test ended")


def test_an_interrupt_ends_the_turn_at_its_completion_not_at_the_ping(
        monkeypatch, turns_env):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        first = session.start("first")
        assert first.wait_started(2).turn_id == "turn-1"
        assert _hook(monkeypatch, harness, "Interrupt", "turn-1") == ""
        assert endpoint.facts().turn_open
        with pytest.raises(TimeoutError):
            first.wait_ended(0.05)
        app.complete("interrupted")
        assert first.wait_ended(2) == Ended("turn-1", "aborted")
        assert not endpoint.facts().turn_open
    finally:
        endpoint.stop("the test ended")


def test_codex_going_idle_without_completing_ends_the_turn_after_the_grace(
        monkeypatch, turns_env):
    monkeypatch.setattr(codex_module, "TURN_COMPLETED_GRACE", 0.05)
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        first = session.start("first")
        assert first.wait_started(2).turn_id == "turn-1"
        assert _hook(monkeypatch, harness, "Interrupt", "turn-1") == ""
        app.status("idle")                  # and no turn/completed ever comes
        assert endpoint.facts().turn_open
        assert first.wait_ended(2) == Ended("turn-1", "error")
        assert not endpoint.facts().turn_open
    finally:
        endpoint.stop("the test ended")


def test_a_completion_or_a_return_to_active_inside_the_grace_stops_its_timer(
        monkeypatch, turns_env):
    monkeypatch.setattr(codex_module, "TURN_COMPLETED_GRACE", 0.2)
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        first = session.start("first")
        assert first.wait_started(2).turn_id == "turn-1"
        assert _hook(monkeypatch, harness, "Interrupt", "turn-1") == ""
        app.status("idle")
        app.complete("interrupted")
        assert first.wait_ended(2) == Ended("turn-1", "aborted")
        second = session.start("second")
        assert second.wait_started(2).turn_id == "turn-2"
        app.status("idle")
        app.status("active")
        time.sleep(0.4)                     # both timers would have run out by now
        assert endpoint.facts().turn_open
        with pytest.raises(TimeoutError):
            second.wait_ended(0.01)
        app.complete()
        assert second.wait_ended(2) == Ended("turn-2", "answer")
    finally:
        endpoint.stop("the test ended")


_RAISES = [
    # Not connected, or the send itself raised: the request never went out.
    pytest.param(lambda: proto.AppServerOffline("Codex app-server not connected"),
                 "refused", id="not-sent"),
    # Codex answered the request with an error.
    pytest.param(lambda: proto.CodexError(-32000, "synthetic failure"), "refused",
                 id="error-answer"),
    # The connection closed after the send, or the call timed out: Codex may have it.
    pytest.param(lambda: proto.NoReply("Codex app-server closed the connection"), "lost",
                 id="closed-after-send"),
    pytest.param(lambda: proto.NoReply("Codex app-server timed out on turn/start"),
                 "lost", id="timed-out"),
]


def _check(delivery, reading):
    started, ended = delivery.wait_started(2), delivery.wait_ended(2)
    if reading == "refused":
        assert started == ended == Refused("gone")
    else:
        assert isinstance(started, Lost) and started.untied and ended == started


@pytest.mark.parametrize("make,reading", _RAISES)
def test_a_raising_turn_start_reads_refused_only_when_codex_did_not_take_it(
        turns_env, make, reading):
    endpoint, harness, app = _host(turns_env)
    original = app.call

    def call(method, params=None, timeout=None):
        if method == "turn/start":
            raise make()
        return original(method, params, timeout)

    app.call = call
    session = Session(endpoint.dir, endpoint.hello())
    try:
        _check(session.start("hello"), reading)
        assert not endpoint.facts().turn_open
    finally:
        endpoint.stop("the test ended")


@pytest.mark.parametrize("make,reading", _RAISES)
def test_a_raising_turn_steer_reads_refused_only_when_codex_did_not_take_it(
        turns_env, make, reading):
    endpoint, harness, app = _host(turns_env)
    session = Session(endpoint.dir, endpoint.hello())
    first = session.start("first")
    assert first.wait_started(2).turn_id == "turn-1"
    original = app.call

    def call(method, params=None, timeout=None):
        if method == "turn/steer":
            raise make()
        return original(method, params, timeout)

    app.call = call
    try:
        _check(session.deliver("now", steer=True), reading)
        assert endpoint.facts().turn_open
        app.complete()
        assert first.wait_ended(2) == Ended("turn-1", "answer")
    finally:
        endpoint.stop("the test ended")


def test_a_raising_turns_list_after_an_absorbed_start_reads_lost(monkeypatch, turns_env):
    # Once turn/start has answered, Codex has the text: a raise from the later
    # thread/turns/list call is not a refusal either.
    endpoint, harness, app = _host(turns_env)
    original = app.call

    def call(method, params=None, timeout=None):
        if method == "turn/start":
            app.calls.append((method, params))
            return {"turn": {"id": "phantom"}}
        if method == "thread/turns/list":
            raise proto.AppServerOffline("Codex app-server closed the connection")
        return original(method, params, timeout)

    app.call = call
    monkeypatch.setattr(codex_module, "TURN_STARTED_WAIT", 0.01)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        delivery = session.start("hello")
        started = delivery.wait_started(2)
        assert isinstance(started, Lost) and started.untied
        assert not endpoint.facts().turn_open
    finally:
        endpoint.stop("the test ended")


def test_a_load_that_shows_the_turn_running_stops_the_grace(monkeypatch, turns_env):
    # The status backstop is Codex's word read late; a reconnect whose load shows the
    # turn still in progress is a later word, and the timer set before it stops.
    monkeypatch.setattr(codex_module, "TURN_COMPLETED_GRACE", 0.2)
    harness = None
    made = []

    class ActiveResume(FakeClient):
        def call(self, method, params=None, timeout=None):
            if method == "thread/resume":
                self.calls.append((method, params))
                return {"thread": {"id": "thread-1", "status": {"type": "active"}}}
            return super().call(method, params, timeout)

    def clients(**kwargs):
        client = (ActiveResume if made else FakeClient)(harness, **kwargs)
        if made:
            client.live_turn = made[0].live_turn
        made.append(client)
        return client

    harness = CodexHarness(turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
                           client_factory=clients, app_server_factory=FakeAppServer)
    endpoint = Endpoint(harness, session_id=None).open()
    harness.start()
    session = Session(endpoint.dir, endpoint.hello())
    try:
        running = session.start("running")
        assert running.wait_started(2).turn_id == "turn-1"
        made[0].status("idle")
        made[0].close()
        harness._connect()
        time.sleep(0.4)
        assert endpoint.facts().turn_open
        made[1].complete()
        assert running.wait_ended(2) == Ended("turn-1", "answer")
    finally:
        endpoint.stop("the test ended")


def test_noreply_after_turn_started_claimed_the_delivery_leaves_it_on_that_turn(
        turns_env):
    # turn/started reaches the host and claims the delivery, then the connection
    # closes before turn/start's answer. Codex named the turn that took the text, so the
    # delivery stays Started on it and ends with it; NoReply does not make it Lost.
    endpoint, harness, app = _host(turns_env)
    original = app.call

    def call(method, params=None, timeout=None):
        if method == "turn/start" and params["input"][0]["text"] == "hello":
            app.calls.append((method, params))
            app.live_turn = "turn-1"
            app.on_notification("turn/started", {"turn": {"id": "turn-1"}}, None)
            raise proto.NoReply("Codex app-server closed the connection")
        return original(method, params, timeout)

    app.call = call
    session = Session(endpoint.dir, endpoint.hello())
    try:
        delivery = session.start("hello")
        assert delivery.wait_started(2) == Started("turn-1")
        with pytest.raises(TimeoutError):
            delivery.wait_ended(0.2)
        assert endpoint.facts().turn_open
        app.complete()
        assert delivery.wait_ended(2) == Ended("turn-1", "answer")
    finally:
        endpoint.stop("the test ended")


class _ActiveResume(FakeClient):
    """A reconnect's client whose load shows the thread running."""

    def call(self, method, params=None, timeout=None):
        if method == "thread/resume":
            self.calls.append((method, params))
            return {"thread": {"id": "thread-1", "status": {"type": "active"}}}
        return super().call(method, params, timeout)


def _reconnecting_host(turns_env, first_client):
    """A host whose first client is `first_client` and whose reconnects resume active,
    carrying the running turn over."""
    harness = None
    made = []

    def clients(**kwargs):
        client = (_ActiveResume if made else first_client)(harness, **kwargs)
        if made:
            client.live_turn = made[0].live_turn
        made.append(client)
        return client

    harness = CodexHarness(turns_env, window=False, ping=["/env/bin/moxi-turns-ping"],
                           client_factory=clients, app_server_factory=FakeAppServer)
    endpoint = Endpoint(harness, session_id=None).open()
    harness.start()
    return endpoint, harness, made


def test_a_reconnect_inside_the_started_wait_ties_the_delivery_to_the_turn_it_shows(
        monkeypatch, turns_env):
    # turn/start is answered, its turn/started is
    # lost with the connection, and the reconnect's load shows the turn that took the
    # text running. That load is a list naming an in-progress turn the host has not
    # reported, so it ties the delivery; nothing waits for PENDING_TTL_S.
    monkeypatch.setattr(codex_module, "TURN_STARTED_WAIT", 3.0)
    sent = threading.Event()

    class DropNotify(FakeClient):
        def call(self, method, params=None, timeout=None):
            if method == "turn/start" and params["input"][0]["text"] == "hello":
                self.calls.append((method, params))
                self.live_turn = "turn-1"     # Codex started it; the notification is lost
                sent.set()
                return {"turn": {"id": "turn-1"}}
            return super().call(method, params, timeout)

    endpoint, harness, made = _reconnecting_host(turns_env, DropNotify)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        delivery = session.start("hello")
        assert sent.wait(2)
        time.sleep(0.05)                      # turn/start has answered; the host waits
        made[0].close()
        harness._connect()
        assert delivery.wait_started(1) == Started("turn-1")
        assert endpoint._inflight is None and endpoint.facts().turn_open
        made[1].complete()
        assert delivery.wait_ended(2) == Ended("turn-1", "answer")
    finally:
        endpoint.stop("the test ended")


def test_a_turn_a_load_reports_before_turn_start_answers_does_not_count_as_its_start(
        monkeypatch, turns_env):
    # The other order: the reconnect's load reports the running turn before
    # turn/start has answered, so it cannot claim the delivery, and its report wakes the
    # wait. _start_turn checks the claim and takes the list path: the turn is already
    # reported, so the delivery reads lost at once instead of waiting in flight.
    monkeypatch.setattr(codex_module, "TURN_STARTED_WAIT", 3.0)
    harness_box = []

    class ReconnectMidStart(FakeClient):
        def call(self, method, params=None, timeout=None):
            if method == "turn/start" and params["input"][0]["text"] == "hello":
                self.calls.append((method, params))
                self.live_turn = "turn-1"     # the person's turn absorbs the text
                harness_box[0]._connect()     # and the link drops and reloads first
                return {"turn": {"id": "phantom-1"}}
            return super().call(method, params, timeout)

    endpoint, harness, made = _reconnecting_host(turns_env, ReconnectMidStart)
    harness_box.append(harness)
    session = Session(endpoint.dir, endpoint.hello())
    try:
        delivery = session.start("hello")
        started = delivery.wait_started(1)
        assert isinstance(started, Lost) and started.untied
        assert endpoint._inflight is None
        facts = endpoint.facts()
        assert facts.turn_open                # turn-1 is the person's, and runs on
        made[1].complete()
        deadline = time.monotonic() + 2
        while endpoint.facts().turn_open and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not endpoint.facts().turn_open
    finally:
        endpoint.stop("the test ended")
