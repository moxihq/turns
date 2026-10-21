"""The harness wire: what comes in, what the fields say, what goes out.

`read_event` reads stdin defensively and reports what it absorbs; `field` reads one
fact under any of its spellings. The cases below are every shape of bad stdin a hook
has met: unreadable, undecodable, not JSON, not an object, and empty.
"""

from __future__ import annotations

import io
import json

import pytest

from hooks_support import Collector
from moxi_turns.hooks.wire import envelope, field, plain, read_event


class Unreadable:
    def __init__(self, exc):
        self.exc = exc

    def read(self):
        raise self.exc


# --- read_event ---------------------------------------------------------------------

def test_the_harness_json_comes_back_as_a_dict():
    data = read_event(io.StringIO(json.dumps({"cwd": "/x", "source": "startup"})))
    assert data == {"cwd": "/x", "source": "startup"}


@pytest.mark.parametrize("raw", ["", "   ", "\n\t "])
def test_empty_stdin_is_an_empty_dict_and_is_not_a_failure(raw):
    """Explicitly not reported: `{}` is the correct answer to an empty stdin."""
    seen = Collector()
    assert read_event(io.StringIO(raw), on_error=seen) == {}
    assert seen.calls == []


@pytest.mark.parametrize("exc", [OSError("closed"), ValueError("undecodable")])
def test_an_unreadable_stream_is_an_empty_dict_and_is_reported(exc):
    """An OSError or an undecodable stdin must not end a SessionStart hook in a
    traceback."""
    seen = Collector()
    assert read_event(Unreadable(exc), on_error=seen) == {}
    assert seen.wheres == ["read"]
    assert seen.calls[0][0] is exc


def test_an_undecodable_stdin_is_absorbed_as_the_value_error_it_is():
    exc = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
    seen = Collector()
    assert read_event(Unreadable(exc), on_error=seen) == {}
    assert seen.wheres == ["read"]


def test_input_that_is_not_json_is_an_empty_dict_and_is_reported():
    seen = Collector()
    assert read_event(io.StringIO("not json at all"), on_error=seen) == {}
    assert seen.wheres == ["read"]
    assert isinstance(seen.calls[0][0], ValueError)


@pytest.mark.parametrize("raw", ["[1, 2]", '"a string"', "17", "null", "true"])
def test_json_that_is_not_an_object_synthesises_a_value_error(raw):
    """So that every absorbed failure reaches `on_error` as an exception, and
    none of the four is swallowed."""
    seen = Collector()
    assert read_event(io.StringIO(raw), on_error=seen) == {}
    assert seen.wheres == ["read"]
    assert isinstance(seen.calls[0][0], ValueError)


def test_read_event_never_raises_and_needs_no_on_error():
    assert read_event(io.StringIO("garbage")) == {}
    assert read_event(Unreadable(OSError())) == {}


def test_it_reads_once_and_does_not_retry():
    class Counting:
        def __init__(self):
            self.reads = 0

        def read(self):
            self.reads += 1
            return "nonsense"

    stream = Counting()
    read_event(stream)
    assert stream.reads == 1


# --- field --------------------------------------------------------------------------

def test_the_first_non_blank_string_among_the_spellings_wins():
    """The harness has sent the same fact under more than one spelling more than once."""
    data = {"sessionId": "b"}
    assert field(data, "session_id", "sessionId") == "b"
    assert field({"session_id": "a", "sessionId": "b"}, "session_id", "sessionId") == "a"


@pytest.mark.parametrize("value", ["", "   ", None, 17, [], {}, True])
def test_a_blank_or_non_string_value_is_not_a_field(value):
    assert field({"k": value}, "k") is None


def test_a_missing_key_is_none_and_no_keys_is_none():
    assert field({}, "a", "b") is None
    assert field({"a": "x"}) is None


def test_field_knows_no_field_names():
    """Which spellings to try is the caller's fact, passed as arguments."""
    assert field({"whatever": "v"}, "whatever") == "v"


# --- envelope -----------------------------------------------------------------------

def test_the_envelope_is_the_three_keys_in_compact_separators():
    """One byte spelling, compact separators, so every hook turns renders writes the
    same bytes for the same payload."""
    assert envelope("SessionStart", "hello") == (
        '{"hookSpecificOutput":{"hookEventName":"SessionStart",'
        '"additionalContext":"hello"}}'
    )


def test_the_envelope_has_no_trailing_newline():
    assert not envelope("SessionStart", "x").endswith("\n")


def test_the_event_name_is_written_verbatim_with_no_table_to_miss_from():
    """A caller firing on an event this kit has never heard of gets a correct
    envelope for it, because the name must match the hook that fired."""
    for name in ("SessionStart", "UserPromptSubmit", "NotAnEventAnybodyHasShipped", ""):
        got = json.loads(envelope(name, "x"))
        assert got["hookSpecificOutput"]["hookEventName"] == name


@pytest.mark.parametrize("text", [None, 17, ["a"], {"a": 1}, b"bytes"])
def test_a_payload_that_is_not_a_string_is_the_consumers_error(text):
    """It raises rather than serialising a JSON number or list onto the wire; `run`
    turns that into one `on_error` with `where="render"` and no output at all."""
    with pytest.raises(TypeError):
        envelope("SessionStart", text)


def test_the_payload_survives_every_byte_a_reader_can_type():
    payload = 'quotes " backslash \\ newline \n unicode ü emoji 🜂 tab \t'
    got = json.loads(envelope("SessionStart", payload))
    assert got["hookSpecificOutput"]["additionalContext"] == payload


# --- plain --------------------------------------------------------------------------

def test_plain_returns_the_text_unchanged_and_ignores_the_event():
    """The second output shape: a person-facing line, such as one printed at
    SessionEnd, which is prose and not an envelope."""
    assert plain("SessionEnd", "one operator-facing line") == "one operator-facing line"
    assert plain("anything", "same") == plain("", "same")


@pytest.mark.parametrize("text", [None, 17, ["a"], {"a": 1}, b"bytes"])
def test_plain_refuses_a_non_string_the_way_envelope_does(text):
    """Left to pass through, a non-string payload would reach `emit`'s concatenation
    and report `where="emit"`: a third answer to one mistake, and the wrong one, since
    the mistake is the payload, not the write. Both renderers give one answer,
    `render`, the same as `envelope` gives."""
    with pytest.raises(TypeError):
        plain("SessionEnd", text)
