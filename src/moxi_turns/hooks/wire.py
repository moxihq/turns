"""The harness wire: the JSON in, the field reads, and the two output shapes.

`read_event` reads the hook's stdin defensively and reports every failure it absorbs;
`field` reads one fact that a harness may send under more than one spelling; `envelope`
is the three-key `additionalContext` line, in one byte spelling; `plain` is the second
output shape, a person-facing line that is not an envelope.

The third output shape, silence, needs no function: a `body` that returns `None`
expresses it.
"""

from __future__ import annotations

import json
import sys

# `from __future__ import annotations` above is load-bearing, not habit: it makes the
# `TextIO` annotation below a string, so `typing` is never imported at runtime and the
# kit's import list stays the three names L3 allows: json, sys, signal.

__all__ = ["envelope", "field", "plain", "read_event"]


# --- the `where` vocabulary ---------------------------------------------------------
#
# `on_error(exc, *, where)` has one signature throughout the kit and `where` is one of
# six short mechanical tokens. They live here, in the lower module, so both modules that
# report share one vocabulary rather than two copies of six strings.

_READ = "read"
_BODY = "body"
_RENDER = "render"
_EMIT = "emit"
_DEADLINE = "deadline"
_EMITTED = "emitted"

_WHERE = (_READ, _BODY, _RENDER, _EMIT, _DEADLINE, _EMITTED)

#: The synthesised failure for JSON that parsed but is not an object, so that every
#: absorbed failure reaches `on_error` as an exception and none is swallowed.
_NOT_OBJECT = "not a JSON object"

#: The refusal both renderers share. A non-string payload would serialise as a JSON
#: number or list on the wire, or concatenate into a `TypeError` one step later and be
#: reported from the wrong place; either way it is the caller's error, it reports as
#: `render`, and it produces no output.
_TEXT_STR = "text must be str"


class _Deadline(BaseException):
    """The armed wall, firing.

    It lives here, beside the `where` vocabulary, because `_report` below has to know
    the class and `run` imports `wire`; the reverse would be a cycle.

    A `BaseException` so that no caller's `except Exception` absorbs it. Two rules
    carry it, and everything about the wall's behaviour follows from them:

    - only the guard that ARMED a wall may absorb it; every other frame re-raises it
      untouched, so an inner guard cannot eat a bound it did not set;
    - `_report` re-raises it and absorbs every other `BaseException`, so a wall firing
      inside a broken logger still reaches the guard that armed it while a broken
      logger still cannot break exit 0.
    """


def _report(on_error, exc: BaseException, where: str) -> None:
    """Hand one absorbed failure to the caller, and let nothing but the wall escape.

    Absorbs `BaseException`, not merely `Exception`: a logger that raises `SystemExit`
    or takes a `KeyboardInterrupt` mid-write must not put a raise in a caller's `main`
    (a broken logger cannot break exit 0; L7, `run` returns 0 on every path). A logger
    that writes to a database under lock contention, with many sessions starting at
    once, spends real time inside that write, so a Ctrl-C landing there is the ordinary
    case and not an exotic one.

    `_Deadline` is the one exception: it is not the logger's failure, and the guard that
    armed it is still owed it.
    """
    if on_error is None:
        return
    try:
        on_error(exc, where=where)
    except _Deadline:
        raise
    except BaseException:  # noqa: BLE001 -- the logger is the caller's; exit 0 is not
        pass


def read_event(stream: TextIO | None = None, *, on_error=None) -> dict:
    """The harness JSON on stdin as a dict. Every failure is `{}` and never an exception.

    Four failures are absorbed and all four are reported: an unreadable stream, an
    undecodable one, input that is not JSON, and JSON that is not an object, for which
    a `ValueError` is synthesised so the report is always an exception. Empty or
    whitespace-only input is NOT a failure and is not reported: `{}` is the correct
    answer to it.

    It reads once and returns. It does not retry, and it does not look at what is in the
    dict: which spellings a field goes by is the caller's fact.
    """
    try:
        raw = (sys.stdin if stream is None else stream).read()
    except (OSError, ValueError) as exc:
        # UnicodeDecodeError is a ValueError: an undecodable stdin lands here, which is
        # why the read is inside the try and not before it.
        _report(on_error, exc, _READ)
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        _report(on_error, exc, _READ)
        return {}
    if not isinstance(data, dict):
        _report(on_error, ValueError(_NOT_OBJECT), _READ)
        return {}
    return data


def field(data: dict, *keys: str) -> str | None:
    """The first key in `keys` whose value is a non-blank string, else `None`.

    A harness has sent the same fact under more than one spelling, so every caller that
    reads a field defensively writes this. It knows no field names: which spellings to
    try is the caller's, passed as arguments.
    """
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def envelope(event: str, text: str) -> str:
    """One line of JSON, no trailing newline, in one byte spelling.

    `event` is written verbatim. The kit holds no list of event names and has no
    unknown-event branch, because the one rule the harness holds it to is that the name
    must match the hook that fired. A caller firing on an event this kit has never heard
    of gets a correct envelope for it.

    A `text` that is not a string is the caller's error: it raises, which `run` turns
    into one `on_error` with `where="render"` and no output at all.
    """
    if not isinstance(text, str):
        raise TypeError(_TEXT_STR)
    return json.dumps(
        {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}},
        separators=(",", ":"),
    )


def plain(event: str, text: str) -> str:
    """`text` unchanged, ignoring `event`: the person-facing line, not an envelope.

    It exists so that "which shape does this event use" is an argument at the call site
    rather than a branch inside the kit.

    It refuses a non-string for the same reason `envelope` does, and so that both
    renderers give the caller ONE answer. Left to pass through, a non-string payload
    reaches `emit`'s concatenation instead and reports as `where="emit"`: a third
    answer to one mistake, and the wrong one, since the mistake is the payload and not
    the write.
    """
    if not isinstance(text, str):
        raise TypeError(_TEXT_STR)
    return text
