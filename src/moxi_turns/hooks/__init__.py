"""turns' hook bodies: the mechanics every harness hook command shares.

A hook command reads the harness's JSON on stdin, tolerates garbage in it, renders the
`additionalContext` envelope, writes it once and flushes, never raises, and bounds how
long the whole thing may take. This package is that, once, so no hook turns writes
carries its own copy. It is turns' internal code: it is not in the announcement's `API`,
so no other module imports it, and every hook body turns renders goes through it.

Two shapes run through the whole surface, and everything else here is a consequence of
one of them:

    The kit never swallows a failure silently. Every failure it absorbs is reported to
    the caller's `on_error` before it returns a degraded value. What that report SAYS
    to a human is the caller's, because the wording belongs to whoever runs the hook.

    The kit never selects a process exit code. `guard` makes a callable unable to
    raise; `run` returns 0 because it owns the whole hook; a command with its own
    exit-code contract composes `guard` and keeps its own `main`.

`on_error` has one signature throughout, `on_error(exc, *, where)`, where `where` is one
of six short mechanical tokens: read, body, render, emit, deadline, emitted. Every call
the kit makes to it is wrapped in a guard of its own, so a broken logger cannot break
exit 0.

Nine public names in three modules:

    moxi_turns.hooks.wire   read_event, field, envelope, plain
    moxi_turns.hooks.text   clamp
    moxi_turns.hooks.run    guard, emit, remaining, run

- `read_event(stream=None, *, on_error=None)`: the harness JSON on stdin as a dict.
  Every failure is `{}` and never an exception, and every failure is reported. Empty
  stdin is not a failure.
- `field(data, *keys)`: the first key whose value is a non-blank string, else `None`.
  A harness has sent the same fact under more than one spelling.
- `envelope(event, text)`: one line of JSON, no trailing newline, compact separators:
  `{"hookSpecificOutput":{"hookEventName":…,"additionalContext":…}}`. The event name is
  written verbatim.
- `plain(event, text)`: `text` unchanged, the person-facing line, which is not an
  envelope.
- `clamp(text, cap, *, marker)`: `text` when it fits, else `text` cut with `marker`
  appended. The marker's length is reserved before anything is spent, so the admission
  can never be the thing that does not fit. `marker` is required and there is no
  default cap.
- `guard(body, *args, on_error=None, seconds=None, default=None, where="body")`: calls
  `body(*args)` under an optional deadline and returns its value, or `default` if it
  raised or the wall fired. Never raises, and says nothing about how the process should
  exit.
- `emit(line, out=None)`: one `write` of `line + "\\n"`, then one `flush`.
- `remaining()`: seconds left on the deadline `guard` or `run` installed, or `None`. A
  caller derives a per-operation timeout from the hook's own wall instead of copying a
  number into each call site.
- `run(event, body, *, stream, out, cap, marker, seconds, render, on_error,
  on_emitted)`: the whole hook, once. Read, compose, cap, render, emit (once), then
  `on_emitted`. Returns 0 on every path.

A `body` returns a `str` for "emit this" or `None` for "this event is a no-op here",
which is the third output shape, silence, expressed without a flag. A body that decides
its payload must not be printed at all raises, and `run` prints nothing and returns 0.
Capping and refusing are different answers to an oversized payload; the kit offers both
and chooses neither.

The deadline, and what it does not cover. `guard(seconds=…)` and `run(seconds=…)` arm
one wall for the whole hook with `signal.setitimer`, which binds the main thread on
POSIX only. Off the main thread, or on a platform with no `SIGALRM`, the kit installs no
deadline and says so through `on_error` with `where="deadline"` rather than pretending
to hold a bound it does not hold. The wall is off entirely unless `seconds` is passed,
and the kit ships no default number. Zero or a negative `seconds` is a caller error, not
a wall: `setitimer` would disarm on it, so the kit installs nothing, reports it with
`where="deadline"`, and `remaining()` answers `None`. Only the guard that armed a wall
absorbs it: a deadline arriving in a frame that armed nothing is re-raised untouched
until it reaches its owner, so an inner `guard` around one blocking call cannot eat the
hook's bound.

The one thing the kit cannot keep off stderr. When the harness closes the read end of
the pipe early, `emit` raises `BrokenPipeError`, `run` absorbs it and exits 0, and
CPython then prints `Exception ignored while flushing sys.stdout: BrokenPipeError` on
stderr as the interpreter shuts down. That line comes from no frame in this package, so
no gauge here can catch it. A caller that needs a silent stderr closes or redirects its
own `sys.stdout` before the interpreter does.

What this package is not:

- Not the wiring. It never reads or writes a harness's settings or hook files, and it
  never decides which command runs on which event; that is turns' adapters'.
- Not a dispatcher. No event-to-handler table anywhere on the hook path: `run` takes one
  event and one callable.
- Not a place for any module's payload words. What a listener answers, turns passes on
  unchanged; the cut marker is the caller's argument.
- Not a transport. It opens no network connection, starts no child process and reads
  no file.
- Not a state store. It keeps no log and no self-log.
- Not the chooser of a process's exit code. It guarantees the hook path cannot raise;
  what `main` returns is the caller's.
- Not a policy about which events exist, which harness fires them, or which module
  answers them.

The ten laws, each with a live gauge in turns' `tests/hooks/test_hooks_law.py`:

    L1  Stateless: every cap, marker, timeout and field spelling arrives as an argument.
    L2  Env-free: no module reads the environment or computes a default path.
    L3  Standard library only: the package imports `json`, `sys` and `signal` and its
        own three modules, and nothing of Moxi's, the core and the rest of turns
        included.
    L4  Nothing imported eagerly: importing the package binds no submodule.
    L5  A hard per-hook time bound, when `seconds` is passed: the body is abandoned and
        nothing is written to stdout.
    L6  A size cap that admits its own cuts.
    L7  The hook path cannot raise, and the kit never selects an exit code.
    L8  Compose, then emit, once.
    L9  stdout carries the payload or nothing; the kit writes no diagnostic itself.
    L10 No module's words: the kit authors no string a reader sees.

Nothing is bound here at import. The submodules are reached through a module-level
`__getattr__` (L4): with three stdlib imports the saving today is nil, and the law's
purpose is the second clause. It is the tripwire that makes any future eager import a
visible change to this file rather than a line added to a module somebody already
imports.
"""

from __future__ import annotations

import sys

_SUBMODULES = ("run", "text", "wire")

__all__ = ["run", "text", "wire"]


def __getattr__(name):
    """Bind a submodule on first touch, and nothing before that (L4).

    `__import__` rather than a `from . import …` statement: inside a package's own
    `__getattr__`, the from-form asks the package for the attribute it is in the middle
    of resolving and recurses until the stack ends. The import system sets the attribute
    on the package itself, so this runs once per submodule and never again.
    """
    if name in _SUBMODULES:
        __import__(f"{__name__}.{name}")
        return sys.modules[f"{__name__}.{name}"]
    raise AttributeError(name)


def __dir__():
    return sorted(set(globals()) | set(_SUBMODULES))
