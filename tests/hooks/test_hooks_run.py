"""`guard`, `emit`, `remaining`, `run` — the order, the shapes and the refusals.

The laws these serve have their named gauges in `test_hooks_law.py`. What is here is the
behaviour around them: the order inside `run`, the three output shapes, the refusal
path, and the six `where` tokens reaching `on_error` from the six
places they name.
"""

from __future__ import annotations

import io
import json

import pytest

from hooks_support import Collector, Recorder
from moxi_turns.hooks.run import emit, guard, run
from moxi_turns.hooks.wire import envelope, plain

EVENT = json.dumps({"cwd": "/w", "session_id": "s-1"})


# --- guard --------------------------------------------------------------------------

def test_guard_returns_the_bodys_value_when_nothing_goes_wrong():
    assert guard(lambda a, b: a + b, 2, 3) == 5


def test_guard_passes_its_positional_arguments_through():
    assert guard("-".join, ["a", "b"]) == "a-b"


def test_guard_returns_the_default_and_reports_where_it_was_told():
    seen = Collector()

    def boom():
        raise KeyError("k")

    assert guard(boom, on_error=seen, default="fallback", where="render") == "fallback"
    assert seen.wheres == ["render"]
    assert isinstance(seen.calls[0][0], KeyError)


def test_guard_defaults_its_where_to_body():
    seen = Collector()
    guard(lambda: 1 / 0, on_error=seen)
    assert seen.wheres == ["body"]


def test_guard_selects_no_exit_code():
    """`guard` is the separable never-raise primitive. A command composes it and keeps
    its own `main`, its own flags and every exit code it ships."""
    seen = Collector()

    def exits():
        raise SystemExit(1)

    assert guard(exits, on_error=seen, default="still here") == "still here"
    assert seen.wheres == ["body"]


def test_guard_absorbs_a_failure_with_no_on_error_at_all():
    assert guard(lambda: 1 / 0) is None


# --- emit ---------------------------------------------------------------------------

def test_emit_is_one_write_and_one_flush():
    out = Recorder()
    emit("the line", out)
    assert out.writes == ["the line\n"]
    assert out.flushes == 1


def test_emit_flushes_after_it_writes_and_before_anything_else():
    order = []

    class Ordered(Recorder):
        def write(self, chunk):
            order.append("write")
            return super().write(chunk)

        def flush(self):
            order.append("flush")
            return super().flush()

    emit("x", Ordered())
    assert order == ["write", "flush"]


def test_emit_writes_to_stdout_when_no_stream_is_given(capsys):
    emit("to the real stdout")
    assert capsys.readouterr().out == "to the real stdout\n"


# --- run: the three output shapes ---------------------------------------------------

def test_a_body_that_returns_text_gets_the_envelope():
    out = Recorder()
    assert run("SessionStart", lambda d: d["cwd"], stream=io.StringIO(EVENT),
               out=out) == 0
    assert json.loads(out.text)["hookSpecificOutput"] == {
        "hookEventName": "SessionStart",
        "additionalContext": "/w",
    }


def test_a_body_that_returns_none_is_silence_with_no_flag():
    """The third output shape, expressed by the body's return value rather
    than by an argument."""
    out = Recorder()
    assert run("PreCompact", lambda d: None, stream=io.StringIO(EVENT), out=out) == 0
    assert out.writes == []


def test_render_plain_is_the_operator_facing_line():
    out = Recorder()
    assert run("SessionEnd", lambda d: "a line for a human", render=plain,
               stream=io.StringIO(EVENT), out=out) == 0
    assert out.text == "a line for a human\n"


def test_which_shape_an_event_uses_is_an_argument_and_not_a_branch():
    for render, expected in ((envelope, "{"), (plain, "t")):
        out = Recorder()
        run("Whatever", lambda d: "text", render=render, stream=io.StringIO(EVENT),
            out=out)
        assert out.text.startswith(expected)


# --- run: the order ------------------------------------------------------------------

def test_the_order_is_compose_cap_render_emit_once_then_on_emitted():
    """The order, gauged as a sequence rather than trusted as prose."""
    order = []

    class Watched(Recorder):
        def write(self, chunk):
            order.append("emit")
            return super().write(chunk)

        def flush(self):
            order.append("flush")
            return super().flush()

    def body(data):
        order.append("body")
        return "x" * 50

    def render(event, text):
        order.append("render")
        assert len(text) == 10, "the cap is applied to the TEXT, before the envelope"
        return envelope(event, text)

    def on_emitted(text):
        order.append("on_emitted")

    out = Watched()
    assert run("SessionStart", body, stream=io.StringIO(EVENT), out=out, cap=10,
               marker="…", render=render, on_emitted=on_emitted) == 0
    assert order == ["body", "render", "emit", "flush", "on_emitted"]


def test_the_cap_is_applied_to_the_text_and_not_to_the_json_line():
    """The cliff is measured on the additionalContext field, not on the wire."""
    out = Recorder()
    run("SessionStart", lambda d: "y" * 500, stream=io.StringIO(EVENT), out=out,
        cap=20, marker="[cut]")
    text = json.loads(out.text)["hookSpecificOutput"]["additionalContext"]
    assert len(text) == 20
    assert text.endswith("[cut]")
    assert len(out.text) > 20


def test_on_emitted_runs_after_the_flush():
    """An ordering with a reason, not a style: a logger stuck behind a locked database
    must not hold the payload in a buffer while the session waits."""
    flushed = []
    seen = []

    class Watched(Recorder):
        def flush(self):
            flushed.append(True)
            return super().flush()

    run("SessionStart", lambda d: "x", stream=io.StringIO(EVENT), out=Watched(),
        on_emitted=lambda text: seen.append(bool(flushed)))
    assert seen == [True]


def test_on_emitted_is_called_on_the_silent_path_too():
    """A caller that logs its injections logs its no-ops as well."""
    seen = []
    out = Recorder()
    run("SessionStart", lambda d: None, stream=io.StringIO(EVENT), out=out,
        on_emitted=lambda text: seen.append(text))
    assert seen == [None]
    assert out.writes == []


def test_on_emitted_is_handed_the_text_that_was_emitted():
    seen = []
    run("SessionStart", lambda d: "z" * 40, stream=io.StringIO(EVENT), out=Recorder(),
        cap=10, marker="…", on_emitted=seen.append)
    assert seen == ["z" * 9 + "…"]


def test_a_broken_on_emitted_is_reported_and_costs_nothing():
    out = Recorder()
    seen = Collector()

    def on_emitted(text):
        raise RuntimeError("the log is down")

    assert run("SessionStart", lambda d: "x", stream=io.StringIO(EVENT), out=out,
               on_emitted=on_emitted, on_error=seen) == 0
    assert out.text.endswith("\n")
    assert seen.wheres == ["emitted"]


def test_a_body_that_raises_is_not_reported_as_an_emission():
    """One failure is logged once: `on_error` gets the exception and `on_emitted` is not
    called, so a consumer that logs both channels does not log a failure twice."""
    seen = Collector()
    emitted = []

    def body(data):
        raise ValueError("no")

    assert run("SessionStart", body, stream=io.StringIO(EVENT), out=Recorder(),
               on_error=seen, on_emitted=emitted.append) == 0
    assert seen.wheres == ["body"]
    assert emitted == []


# --- run: the refusal path and the caller errors -------------------------------------

def test_a_body_that_refuses_prints_nothing_and_still_returns_zero():
    """Capping and refusing are different answers to an oversized payload, and the
    kit offers both. A caller with a hard backstop raises rather than passing `cap`."""
    out = Recorder()
    seen = Collector()

    def body(data):
        raise AssertionError("this payload must not be printed")

    assert run("SessionStart", body, stream=io.StringIO(EVENT), out=out,
               on_error=seen) == 0
    assert out.writes == []
    assert seen.wheres == ["body"]


def test_a_cap_without_a_marker_is_a_caller_error_and_produces_no_output():
    """The kit will not word the admission, so it will not cut without one."""
    out = Recorder()
    seen = Collector()
    assert run("SessionStart", lambda d: "x" * 100, stream=io.StringIO(EVENT), out=out,
               cap=10, on_error=seen) == 0
    assert out.writes == []
    assert seen.wheres == ["render"]
    assert isinstance(seen.calls[0][0], ValueError)


@pytest.mark.parametrize("render", [envelope, plain])
def test_a_non_string_payload_reports_render_under_either_renderer(render):
    """One mistake, one `where`. Were `plain` to let a non-string through, it would
    blow up in `emit`'s concatenation instead, and the same caller error would report
    from two different places depending on which output shape the event used."""
    out = Recorder()
    seen = Collector()
    assert run("SessionEnd", lambda d: 17, stream=io.StringIO(EVENT), out=out,
               render=render, on_error=seen) == 0
    assert out.writes == []
    assert seen.wheres == ["render"]
    assert isinstance(seen.calls[0][0], TypeError)


def test_a_marker_without_a_cap_caps_nothing():
    out = Recorder()
    run("SessionStart", lambda d: "x" * 100, stream=io.StringIO(EVENT), out=out,
        marker="…")
    assert len(json.loads(out.text)["hookSpecificOutput"]["additionalContext"]) == 100


def test_a_payload_that_fits_the_cap_is_not_marked():
    out = Recorder()
    run("SessionStart", lambda d: "short", stream=io.StringIO(EVENT), out=out,
        cap=100, marker="…")
    assert json.loads(out.text)["hookSpecificOutput"]["additionalContext"] == "short"


# --- run: the body sees the event dict -----------------------------------------------

def test_the_body_is_handed_the_dict_the_stream_carried():
    seen = []
    run("SessionStart", lambda d: seen.append(d) or None, stream=io.StringIO(EVENT),
        out=Recorder())
    assert seen == [{"cwd": "/w", "session_id": "s-1"}]


def test_the_body_is_handed_an_empty_dict_when_the_stream_is_garbage():
    """Degrade to `{}` and carry on; what the consumer does with an
    empty dict is the consumer's policy."""
    seen = []
    errors = Collector()
    run("SessionStart", lambda d: seen.append(d) or None,
        stream=io.StringIO("}{ not json"), out=Recorder(), on_error=errors)
    assert seen == [{}]
    assert errors.wheres == ["read"]


def test_the_body_can_derive_its_own_timeouts_from_what_is_left():
    """In the shape a caller uses it: inside `run`'s deadline, `remaining()` is a
    share of the hook's own wall, so a blocking call on the boot path stops needing its
    own copied number."""
    from moxi_turns.hooks.run import remaining

    seen = []
    run("SessionStart", lambda d: seen.append(remaining()) or None,
        stream=io.StringIO(EVENT), out=Recorder(), seconds=3.0)
    assert len(seen) == 1
    assert 0.0 < seen[0] <= 3.0
    assert remaining() is None


def test_run_is_not_a_dispatcher():
    """One event name, one callable, no table. A caller's handler table and its
    dispatch stay in the caller, because only the caller knows what its verbs mean."""
    calls = []
    run("AnEventNobodyRegistered", lambda d: calls.append(1) or "x",
        stream=io.StringIO(EVENT), out=Recorder())
    assert calls == [1]


@pytest.mark.parametrize("where,kwargs", [
    ("read", dict(stream=io.StringIO("nonsense"), body=lambda d: None)),
    ("body", dict(stream=io.StringIO("{}"), body=lambda d: 1 / 0)),
    ("render", dict(stream=io.StringIO("{}"), body=lambda d: object())),
    ("emitted", dict(stream=io.StringIO("{}"), body=lambda d: "x")),
])
def test_each_where_token_arrives_from_the_place_it_names(where, kwargs):
    seen = Collector()
    body = kwargs.pop("body")
    extra = {}
    if where == "emitted":
        def boom(text):
            raise RuntimeError("x")
        extra["on_emitted"] = boom
    assert run("SessionStart", body, out=Recorder(), on_error=seen, **kwargs,
               **extra) == 0
    assert seen.wheres == [where]
