"""Pipe tests: the process, given this stdin, prints these bytes and exits 0.

The discipline is carried into the package rather than left to each caller:

    The hook contract is not "these functions return the right objects", it is "this
    process, given this stdin, prints this stdout and exits 0", and the two differ in
    exactly the places that break a session's start.

Every script below is a stand-in caller: it composes the kit and raises `SystemExit`
with what `run` returned, because selecting the exit code is the caller's job and never
the kit's.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from hooks_support import drive

HOOK = """
from moxi_turns.hooks.run import run
from moxi_turns.hooks.wire import field
raise SystemExit(run("SessionStart", lambda data: field(data, "cwd", "workingDir")))
"""

WIRE = b'{"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":'


def test_json_on_stdin_becomes_the_envelope_on_stdout():
    code, out, err = drive(HOOK, json.dumps({"cwd": "/home/x/project"}))
    assert code == 0
    assert out == WIRE + b'"/home/x/project"}}\n'
    assert err == ""


def test_the_wire_is_one_line_with_exactly_one_trailing_newline():
    """L8: one write, compact separators, no second newline and no trailing
    space. A test that decoded first could not see any of that."""
    code, out, err = drive(HOOK, json.dumps({"cwd": "/x"}))
    assert out.count(b"\n") == 1
    assert out.endswith(b"}}\n")
    assert b", " not in out and b": " not in out


def test_the_other_spelling_of_a_field_is_read_by_the_consumers_argument():
    code, out, err = drive(HOOK, json.dumps({"workingDir": "/x"}))
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "/x"


@pytest.mark.parametrize("stdin", [b"", b"   ", b"not json", b"[1,2]", b"null",
                                   b'{"cwd": ""}', b"\xff\xfe\x00bad"])
def test_every_shape_of_garbage_on_stdin_exits_zero_with_an_empty_stdout(stdin):
    """Every shape of bad stdin, in one table. The payload or nothing: a
    body handed `{}` returns `None` here, so there is nothing to say and nothing said."""
    code, out, err = drive(HOOK, stdin)
    assert code == 0
    assert out == b""
    assert err == ""


def test_a_closed_stdin_exits_zero():
    """An unreadable stdin must not end a SessionStart hook in a traceback."""
    code, out, err = drive(HOOK, stdin_devnull=True)
    assert code == 0
    assert out == b""
    assert err == ""


@pytest.mark.parametrize("raise_what", [
    'ValueError("boom")',
    "SystemExit(3)",
    "KeyboardInterrupt()",
    'MemoryError("nothing left")',
])
def test_a_body_that_raises_anything_at_all_exits_zero_with_an_empty_stdout(raise_what):
    """L7's gauge, driven as a process: `Exception`, `SystemExit` and bare
    `BaseException` subclasses all leave the hook at 0 with nothing on stdout."""
    code, out, err = drive(
        f"""
        from moxi_turns.hooks.run import run
        def body(data):
            raise {raise_what}
        raise SystemExit(run("SessionStart", body))
        """,
        b"{}",
    )
    assert code == 0
    assert out == b""
    assert err == ""


def test_an_unwritable_stdout_exits_zero():
    """L7: `run` returns 0 on every path including a stdout that cannot be written."""
    code, out, err = drive(
        """
        import sys
        from moxi_turns.hooks.run import run
        sys.stdout.close()
        rc = run("SessionStart", lambda d: "a payload nobody will read")
        sys.stderr.write(str(rc))
        """,
        b"{}",
    )
    assert code == 0
    assert err == "0"


def test_the_deadline_bounds_the_whole_process():
    """L5 as a process: a body past the bound writes nothing, reports once with
    `where="deadline"`, and the hook still exits 0."""
    code, out, err = drive(
        """
        import sys, time
        from moxi_turns.hooks.run import run
        seen = []
        def body(data):
            time.sleep(30)
            return "never"
        rc = run("SessionStart", body, seconds=0.2,
                 on_error=lambda exc, *, where: seen.append(where))
        sys.stderr.write(repr((rc, seen)))
        """,
        b"{}",
        timeout=20,
    )
    assert code == 0
    assert out == b""
    assert err == "(0, ['deadline'])"


def test_a_wall_during_the_read_leaves_the_pipe_empty():
    """As a process: the harness is slow to hand over its JSON, the wall fires inside
    the read, and the payload the body would have produced must never reach the pipe.
    A read guard that absorbed the wall would let these exact bytes out."""
    code, out, err = drive(
        """
        import sys, time
        from moxi_turns.hooks.run import run
        class Slow:
            def read(self):
                time.sleep(30)
                return "{}"
        seen = []
        rc = run("SessionStart", lambda d: "PAYLOAD THAT MUST NEVER BE WRITTEN",
                 stream=Slow(), seconds=0.2,
                 on_error=lambda exc, *, where: seen.append(where))
        sys.stderr.write(repr((rc, seen)))
        """,
        b"",
        timeout=20,
    )
    assert code == 0
    assert out == b""
    assert err == "(0, ['deadline'])"


def test_a_wall_inside_a_nested_guard_still_ends_the_hook():
    """As a process, in the shape a caller writes when it bounds one blocking call
    inside its hook. An inner guard that ate the wall would let the process run to
    completion and emit."""
    code, out, err = drive(
        """
        import sys, time
        from moxi_turns.hooks.run import run, guard
        seen = []
        def body(data):
            guard(time.sleep, 30)
            return "emitted long after the wall"
        start = time.monotonic()
        rc = run("SessionStart", body, seconds=0.2,
                 on_error=lambda exc, *, where: seen.append(where))
        sys.stderr.write(repr((rc, seen, time.monotonic() - start < 5)))
        """,
        b"{}",
        timeout=40,
    )
    assert code == 0
    assert out == b""
    assert err == "(0, ['deadline'], True)"


@pytest.mark.parametrize("raise_what", ["SystemExit(2)", "KeyboardInterrupt()"])
def test_a_logger_that_raises_a_base_exception_still_exits_zero(raise_what):
    """As a process. A logger whose `BaseException` escaped would raise out of the
    caller's `main`, and the process would exit 1 with a traceback: a hook raising out
    of `main`, the defect the kit exists to close."""
    code, out, err = drive(
        f"""
        from moxi_turns.hooks.run import run
        def logger(exc, *, where):
            raise {raise_what}
        def body(data):
            raise ValueError("the body failed")
        raise SystemExit(run("SessionStart", body, on_error=logger))
        """,
        b"{}",
    )
    assert code == 0
    assert out == b""
    assert err == ""


def test_the_silent_path_writes_no_bytes_at_all():
    code, out, err = drive(
        """
        from moxi_turns.hooks.run import run
        raise SystemExit(run("PreCompact", lambda data: None))
        """,
        json.dumps({"source": "compact"}),
    )
    assert code == 0
    assert out == b""


def test_a_consumer_keeps_its_own_exit_code_over_the_kit():
    """As a process: `guard` makes the body unable to raise, and the command computes
    its own code from its own flag. A kit that always exited 0 would silently delete
    every exit code such a command pins."""
    code, out, err = drive(
        """
        import sys
        from moxi_turns.hooks.run import guard
        from moxi_turns.hooks.wire import envelope
        from moxi_turns.hooks.run import emit
        online = guard(lambda: 1 / 0, default=None) is not None
        emit(envelope("SessionStart", "roster"))
        raise SystemExit(0 if online else 1)
        """,
        b"",
    )
    assert code == 1
    assert out == WIRE + b'"roster"}}\n'
    assert err == ""


def test_the_package_needs_nothing_on_the_path_or_in_the_environment():
    """L1 as a process: nothing here reads the environment, so nothing here needs one."""
    code, out, err = drive(HOOK, json.dumps({"cwd": "/x"}), env={})
    assert code == 0
    assert out == WIRE + b'"/x"}}\n'


def test_the_harness_can_drive_it_the_way_a_harness_does():
    """End to end through a real pipe rather than a captured buffer: the harness writes
    JSON to a pipe, closes it, and reads one line back."""
    proc = subprocess.Popen(
        [sys.executable, "-c", HOOK.strip()],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    out, err = proc.communicate(json.dumps({"cwd": "/piped"}).encode(), timeout=30)
    assert proc.returncode == 0
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "/piped"
    assert err == b""
