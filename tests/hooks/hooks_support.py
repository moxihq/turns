"""Shared machinery for the hook tests.

Every hook test that matters is a pipe test: the hook contract is not "these functions
return the right objects", it is "this process, given this stdin, prints these bytes on
stdout and exits 0", and the two differ in exactly the places that break a session's
start.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap


def drive(code: str, stdin: bytes | str = b"", *, env=None, stdin_devnull=False,
          timeout: int = 30):
    """Run `code` as a process with `stdin` on its stdin.

    Returns `(exit_code, stdout_bytes, stderr_text)`. stdout comes back as BYTES on
    purpose: the wire is a byte spelling, and a test that decodes first cannot see a
    trailing space or a second newline.
    """
    if isinstance(stdin, str):
        stdin = stdin.encode("utf-8")
    proc = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        input=None if stdin_devnull else stdin,
        stdin=subprocess.DEVNULL if stdin_devnull else None,
        capture_output=True,
        env=os.environ.copy() if env is None else env,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr.decode("utf-8", "replace")


class Recorder:
    """A stdout double that counts every `write` and every `flush` (the L8 gauge)."""

    def __init__(self, fail: BaseException | None = None):
        self.writes: list[str] = []
        self.flushes = 0
        self.fail = fail

    def write(self, chunk):
        if self.fail is not None:
            raise self.fail
        self.writes.append(chunk)
        return len(chunk)

    def flush(self):
        self.flushes += 1

    @property
    def text(self) -> str:
        return "".join(self.writes)


class Collector:
    """An `on_error` that keeps what it was handed, under the one signature."""

    def __init__(self, fail: BaseException | None = None):
        self.calls: list[tuple[BaseException, str]] = []
        self.fail = fail

    def __call__(self, exc, *, where):
        self.calls.append((exc, where))
        if self.fail is not None:
            raise self.fail

    @property
    def wheres(self) -> list[str]:
        return [where for _, where in self.calls]
