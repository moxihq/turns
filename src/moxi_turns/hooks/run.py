"""The never-raise guard, the one write, the deadline, and the whole hook.

Four names. `guard` is the separable primitive: it makes a callable unable to raise and
says nothing about how the process should exit, which is what lets a command with its
own exit-code contract adopt it and keep its own `main`. `run` is the whole hook once,
and it returns 0 because it owns the whole hook.

Order inside `run`, and why: compose, then cap, then render, then emit (once), then
`on_emitted`. The cap is applied to the additionalContext TEXT, before the envelope,
because the harness's limit is measured on that field and not on the JSON line. Nothing
reaches stdout until the whole line exists, so a timeout, a refusal or a crash produces
no output rather than a truncated one: "assert before it prints", generalised from an
assertion into a structure. And `on_emitted` runs AFTER the flush, inside its own guard,
because the injection is the product and the log is provenance: a logger stuck behind a
locked database must not be able to hold the payload in a buffer while the session
waits.

The refusal path, stated as a rule: a body that decides its payload must not be printed
at all raises, and `run` prints nothing and returns 0. A body that decides its payload
may be cut passes `cap` and a marker. The kit offers both and chooses neither.
"""

from __future__ import annotations

import signal
import sys

from .text import clamp
from .wire import (
    _BODY,
    _DEADLINE,
    _EMIT,
    _EMITTED,
    _READ,
    _RENDER,
    _Deadline,
    _report,
    envelope,
    read_event,
)

__all__ = ["emit", "guard", "remaining", "run"]


#: "this step produced no value", which a body's own `None` must not be confused with:
#: `None` from a body means "no-op, emit nothing" and is a success.
_MISS = object()

#: `setitimer(…, 0)` DISARMS rather than fires. An outer deadline whose time is already
#: spent is re-armed with the smallest positive interval instead, so it fires at once.
_TICK = 1e-6

#: How many kit deadlines are on the stack. `remaining()` reports the kit's own wall and
#: never a timer the caller installed for its own reasons.
_ARMED = 0

#: `cap` without `marker` is a caller error: the kit will not word the admission.
_NO_MARKER = "cap needs a marker"

#: `setitimer` accepts a non-positive interval and DISARMS on it, so a bound of zero or
#: less is a wall that silently is not one. L5 says the bound is off when `seconds` is
#: not passed; passing it and getting nothing is the one outcome the law does not admit,
#: so it is a caller error, reported like any other arming failure.
_BAD_SECONDS = "seconds must be > 0"


def _fire(signum, frame):
    raise _Deadline(_DEADLINE)


def _arm(seconds):
    """Install the wall, or install nothing at all. Returns `(prev_handler, prev_timer)`.

    Nothing half-installed survives a failure here: the handler goes in before the timer
    because there is no way to order it otherwise, so if `setitimer` refuses, the handler
    comes straight back out. Leaving `_fire` behind would make any later SIGALRM in the
    caller's own code raise a `_Deadline` in an unrelated frame, and the next kit `guard`
    would capture `_fire` as "previous" and faithfully restore it forever.
    """
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        raise ValueError(_BAD_SECONDS)
    prev_handler = signal.signal(signal.SIGALRM, _fire)
    try:
        prev_timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    except BaseException:
        if prev_handler is not None:
            signal.signal(signal.SIGALRM, prev_handler)
        raise
    return prev_handler, prev_timer


def _absorb(on_error, exc, where):
    """Report from the frame that owns the wall, where nothing at all may escape.

    `_report` re-raises `_Deadline` so a wall reaches its owner. This frame IS the owner,
    and there is nobody above it to tell: a wall arriving while it reports has been
    outlived by the failure it is already reporting.
    """
    try:
        _report(on_error, exc, where)
    except _Deadline:
        pass


def _shielded(fn, *args):
    """Run the one write with the kit's wall lifted, then put it back exactly as it was.

    `emit` writes and then flushes. A wall delivered between those two leaves the line in
    the buffer, where the interpreter flushes it at exit, so the hook would report a
    deadline AND the harness would still get the payload, which is the one outcome L5
    forbids ("nothing is written to stdout"). The window is one in-memory write and one
    flush; a bound lifted across it costs less than a payload the hook declared dead.

    A timer the caller installed for its own reasons is not touched: nothing happens
    here unless a kit wall is on the stack.
    """
    if not _ARMED:
        return fn(*args)
    held = signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        return fn(*args)
    finally:
        if held[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, held[0], held[1])


def _disarm(seconds, prev_timer, prev_handler):
    """Take the kit's deadline down and put back whatever was underneath it.

    The restore needs no clock, which matters because L3 allows three imports and `time`
    is not one of them: what the inner timer has left, read as it is cancelled, is what
    the outer timer did not spend.
    """
    left = signal.setitimer(signal.ITIMER_REAL, 0)[0]
    if prev_handler is not None:
        signal.signal(signal.SIGALRM, prev_handler)
    outer = prev_timer[0] if prev_timer else 0.0
    if outer > 0:
        spent = seconds - left
        signal.setitimer(signal.ITIMER_REAL, max(outer - spent, _TICK), prev_timer[1])


def guard(body, *args, on_error=None, seconds=None, default=None, where=_BODY):
    """Call `body(*args)` under an optional wall-clock deadline; never raise.

    Returns the body's value, or `default` if it raised or the deadline fired; the
    exception goes to `on_error`. It returns nothing about how the process should exit
    and it never calls `sys.exit`.

    The deadline is off unless `seconds` is passed. It is armed with `signal.setitimer`,
    which binds the main thread on POSIX only: off the main thread, on a platform
    without `SIGALRM`, or when `seconds` is zero or negative, the kit installs no
    deadline and reports that through `on_error` with `where="deadline"` rather than
    pretending to hold a bound it does not hold. The body then runs unbounded, which is
    the honest outcome, and `remaining()` answers `None` rather than describing a wall
    that is not there.

    **Only the frame that armed a wall absorbs it.** A `_Deadline` arriving in a frame
    that armed nothing is re-raised untouched, all the way to the guard whose wall it is.
    Without that rule an inner `guard`, the shape a caller writes when it bounds one
    blocking call inside its hook, eats the hook's wall, and everything after it runs
    with no bound at all while `on_error` has already been told the deadline fired.
    """
    global _ARMED
    armed = False
    prev_handler = None
    prev_timer = None
    if seconds is not None:
        try:
            prev_handler, prev_timer = _arm(seconds)
            armed = True
            _ARMED += 1
        except (ValueError, AttributeError, OSError) as exc:
            _report(on_error, exc, _DEADLINE)
    try:
        return body(*args)
    except _Deadline as exc:
        if not armed:
            raise
        _absorb(on_error, exc, _DEADLINE)
        return default
    except BaseException as exc:  # noqa: BLE001 -- L7 is the whole point of this function
        if armed:
            _absorb(on_error, exc, where)
        else:
            _report(on_error, exc, where)
        return default
    finally:
        if armed:
            _ARMED -= 1
            try:
                _disarm(seconds, prev_timer, prev_handler)
            except BaseException:  # noqa: BLE001 -- a wall that fires as it is taken
                pass               # down has already been outlived by its body


def emit(line: str, out: TextIO | None = None) -> None:
    """One `write` of `line + "\\n"`, then one `flush`.

    One write, because a payload assembled in pieces can be interrupted in pieces, and
    "never print half a payload" is a property of how it is written rather than of how
    careful the writer is. The flush comes before anything else the caller does: the
    injection gates the session's first turn and provenance does not.

    One thing outside the kit's reach, stated rather than left to be discovered. When
    the harness closes the read end of the pipe early, the write or the flush raises
    `BrokenPipeError`, `run` absorbs it and exits 0, and the interpreter then prints
    `Exception ignored while flushing sys.stdout: BrokenPipeError` on stderr as it
    shuts down. That line is CPython's, not the kit's: it comes from no frame here, no
    gauge can catch it, and a caller that needs a silent stderr closes or redirects its
    own `sys.stdout` before the interpreter does.
    """
    stream = sys.stdout if out is None else out
    stream.write(line + "\n")
    stream.flush()


def remaining() -> float | None:
    """Seconds left on the deadline `guard` or `run` installed, or `None` when none is.

    It is how a caller stops writing the same timeout into every blocking call on the
    boot path: the bound becomes a share of the hook's own wall instead of a number
    copied into each call site.

    Under nesting it answers the INNERMOST wall, which is the one a call made here must
    actually respect. It answers `None` whenever no kit wall is standing, including
    when `seconds` was passed and refused, rather than describing a timer the caller
    installed for its own reasons, or a bound that is not there.
    """
    if not _ARMED:
        return None
    left = signal.getitimer(signal.ITIMER_REAL)[0]
    return left if left > 0 else 0.0


def _cap(text, cap, marker):
    if marker is None:
        raise ValueError(_NO_MARKER)
    return clamp(text, cap, marker=marker)


def _once(event, body, stream, out, cap, marker, render, on_error, on_emitted):
    """The hook's one pass, with every step in its own guard and its own `where`.

    None of these guards arms anything, so none of them absorbs the wall: a `_Deadline`
    from any step rises through here to `run`'s outer guard, which armed it, reports it
    once, and ends the hook with nothing written. That is what makes L5's "the body is
    abandoned, nothing is written to stdout" true when the wall fires during the read or
    the render rather than only during the body.
    """
    data = guard(
        lambda: read_event(stream, on_error=on_error),
        on_error=on_error, default={}, where=_READ,
    )
    text = guard(body, data, on_error=on_error, default=_MISS, where=_BODY)
    if text is _MISS:
        # The refusal path. `on_error` has the exception; stdout gets nothing, and
        # `on_emitted` is not called, so one failure is logged once.
        return
    if text is not None:
        if cap is not None:
            text = guard(_cap, text, cap, marker, on_error=on_error, default=_MISS,
                         where=_RENDER)
            if text is _MISS:
                return
        line = guard(render, event, text, on_error=on_error, default=_MISS,
                     where=_RENDER)
        if line is _MISS:
            return
        if guard(_shielded, emit, line, out, on_error=on_error, default=_MISS,
                 where=_EMIT) is _MISS:
            return
    if on_emitted is not None:
        guard(on_emitted, text, on_error=on_error, where=_EMITTED)


def run(event: str, body, *, stream: TextIO | None = None, out: TextIO | None = None,
        cap: int | None = None, marker: str | None = None, seconds: float | None = None,
        render=envelope, on_error=None, on_emitted=None) -> int:
    """The whole hook, once. Returns 0 on every path, and never calls `sys.exit`.

    Read the event dict from `stream`, call `body(data)` through `guard`, clamp what it
    returns if `cap` is given, render it through `render(event, text)`, emit it in one
    write, call `on_emitted(text)`, return 0.

    `body` returns a `str` for "emit this" or `None` for "this event is a no-op here",
    which is how the third output shape, silence, is expressed without a flag.
    `on_emitted` is called on that silent path too, because a caller that logs its
    injections logs its no-ops as well. Passing `cap` without `marker` is a caller error
    and produces no output.

    `run` is not a dispatcher: it is handed one event name and one callable, it holds no
    table mapping events to handlers, it reads no file and it imports nothing of Moxi's.
    The mapping of verbs to events and the refusal of an unknown verb stay in the
    caller, the only place that knows what its own verbs mean.

    The deadline, when `seconds` is passed, covers the whole pass (the read, the body,
    the cap, the render and the write), because the thing worth bounding is how long a
    session waits on its hooks, not how long one step of one hook takes.
    """
    guard(
        _once, event, body, stream, out, cap, marker, render, on_error, on_emitted,
        on_error=on_error, seconds=seconds, where=_BODY,
    )
    return 0
