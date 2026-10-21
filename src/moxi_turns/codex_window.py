"""The Codex window attached to a hosted thread through ``--remote``.

The host owns the window process group. If its app-server generation ends, it closes
the old window and opens another after the resumed thread is ready. If the operator
closes a live window, the host ends too.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import signal
import subprocess
import sys
import termios
import threading
import time

log = logging.getLogger("moxi.turns.codex.window")

ENTER_ALT_SCREEN = b"\x1b[?1049h"
LEAVE_ALT_SCREEN = b"\x1b[?1049l"
SHOW_CURSOR = b"\x1b[?25h"
RESTORE_SCREEN = ENTER_ALT_SCREEN + LEAVE_ALT_SCREEN + SHOW_CURSOR
JOB_CONTROL_SIGNALS = (signal.SIGTSTP, signal.SIGTTIN, signal.SIGTTOU)

TERM_GRACE = 5.0
PR_SET_PDEATHSIG = 1
READY_WAIT = 180.0
POLL = 0.1
GENERATION_CHECK_WAIT = 2.0
WAITING = "waiting for the Codex thread to be ready (up to {wait:.0f}s; Ctrl-C stops)"


def die_with_parent(parent_pid: int):
    """Return a Linux ``preexec_fn`` that sends SIGTERM when the parent dies."""
    if not sys.platform.startswith("linux"):
        return None

    def _pre():
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGTERM), 0, 0, 0)
        if os.getppid() != parent_pid:
            os._exit(0)

    return _pre


def terminal_fd():
    for fd in (0, 1, 2):
        with contextlib.suppress(OSError):
            if os.isatty(fd):
                return fd
    with contextlib.suppress(OSError):
        return os.open("/dev/tty", os.O_RDWR | os.O_NOCTTY)
    return None


def in_terminal_foreground(fd) -> bool:
    if fd is None:
        return False
    try:
        return os.tcgetpgrp(fd) == os.getpgrp()
    except OSError:
        return False


def window_argv(binary: str, thread_id: str, socket_path) -> list[str]:
    return [binary, "-c", "check_for_update_on_startup=false", "resume",
            str(thread_id), "--remote", f"unix://{socket_path}"]


class Terminal:
    """The terminal mode and foreground group borrowed by one window."""

    def __init__(self, fd):
        self.fd = fd
        self.saved = None
        self.hung_up = False

    def save(self) -> None:
        try:
            self.saved = termios.tcgetattr(self.fd)
        except termios.error:
            self.saved = None

    def give(self, pgid: int) -> None:
        with contextlib.suppress(OSError):
            os.tcsetpgrp(self.fd, pgid)

    def take_back(self) -> None:
        if self.hung_up:
            return
        try:
            os.tcsetpgrp(self.fd, os.getpgrp())
            if self.saved is not None:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            os.write(self.fd, RESTORE_SCREEN)
        except (OSError, termios.error) as exc:
            log.warning("could not take the terminal back: %s", exc)


class Window:
    def __init__(self, proc: subprocess.Popen, generation):
        self.proc = proc
        self.pgid = proc.pid
        self.generation = generation
        self.status: int | None = None

    @property
    def exited(self) -> bool:
        return self.status is not None

    @property
    def exit_code(self) -> int | None:
        return None if self.status is None else os.waitstatus_to_exitcode(self.status)


class WindowSession:
    """Background owner for windows opened on successive app-server generations."""

    def __init__(self, host, *, binary: str, cwd: str, env, ready_wait=READY_WAIT,
                 fd=None, say=None):
        self.host = host
        self.binary = binary
        self.cwd = cwd
        self.env = env
        self.ready_wait = ready_wait
        self.fd = terminal_fd() if fd is None else fd
        self.say = say or (lambda message: print(message, file=sys.stderr, flush=True))
        if self.fd is None:
            raise RuntimeError("window=True needs a controlling terminal")
        self.terminal = Terminal(self.fd)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.windows: list[Window] = []

    def start(self) -> "WindowSession":
        self._thread = threading.Thread(
            target=self.run, daemon=True, name="turns-codex-window",
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        for window in list(self.windows):
            if not window.exited:
                self.end(window)
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=TERM_GRACE * 2)

    def run(self) -> None:
        while not self._stop.is_set():
            generation = self._wait_ready()
            if generation is None:
                return
            window = self.spawn(generation)
            while not self._stop.is_set():
                if generation.ended.is_set():
                    self.end(window)
                    self.terminal.take_back()
                    break
                if self.poll(window):
                    self.reap_group(window)
                    self.terminal.take_back()
                    if not generation.ended.is_set() and not self._stop.is_set():
                        if not self.host.generation_answers(
                                generation, GENERATION_CHECK_WAIT):
                            self.host.drop_generation(generation)
                            break
                        self.host.window_closed(window.exit_code)
                    return
                self._stop.wait(POLL)
            else:  # pragma: no cover - the condition exits through the loop body
                continue
            if self._stop.is_set():
                self.end(window)
                self.terminal.take_back()
                return

    def _wait_ready(self):
        deadline = time.monotonic() + self.ready_wait
        said = False
        while not self._stop.is_set() and time.monotonic() < deadline:
            generation = self.host.generation
            if (generation is not None and generation.ready.is_set()
                    and not generation.ended.is_set() and generation.thread_id):
                return generation
            if not said:
                self.say(WAITING.format(wait=self.ready_wait))
                said = True
            self._stop.wait(POLL)
        if not self._stop.is_set():
            self.host.window_failed(self.host.give_up_reason())
        return None

    def spawn(self, generation) -> Window:
        argv = window_argv(self.binary, generation.thread_id,
                           self.host.app_socket)
        self.terminal.save()
        fd = self.fd
        parent = os.getpid()
        parent_death = die_with_parent(parent)

        def preexec():
            os.setpgid(0, 0)
            with contextlib.suppress(OSError):
                os.tcsetpgrp(fd, os.getpid())
            for sig in JOB_CONTROL_SIGNALS:
                signal.signal(sig, signal.SIG_DFL)
            if parent_death is not None:
                parent_death()

        proc = subprocess.Popen(
            argv, cwd=self.cwd, env=self.env, stdin=fd, stdout=fd, stderr=fd,
            preexec_fn=preexec, close_fds=True,
        )
        with contextlib.suppress(OSError):
            os.setpgid(proc.pid, proc.pid)
        self.terminal.give(proc.pid)
        window = Window(proc, generation)
        self.windows.append(window)
        log.info("window pid %s opened on thread %s", proc.pid, generation.thread_id)
        return window

    def poll(self, window: Window) -> bool:
        if window.exited:
            return True
        try:
            pid, status = os.waitpid(window.proc.pid, os.WNOHANG | os.WUNTRACED)
        except ChildProcessError:
            window.status = 0
            window.proc.returncode = 0
            return True
        if pid == 0:
            return False
        if os.WIFSTOPPED(status):
            self.terminal.give(window.pgid)
            with contextlib.suppress(OSError):
                os.killpg(window.pgid, signal.SIGCONT)
            return False
        window.status = status
        window.proc.returncode = os.waitstatus_to_exitcode(status)
        return True

    def end(self, window: Window) -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if window.exited and not self._group_alive(window):
                return
            with contextlib.suppress(OSError):
                os.killpg(window.pgid, sig)
            with contextlib.suppress(OSError):
                os.killpg(window.pgid, signal.SIGCONT)
            deadline = time.monotonic() + TERM_GRACE
            while time.monotonic() < deadline:
                self.poll(window)
                if window.exited and not self._group_alive(window):
                    return
                time.sleep(0.02)

    def reap_group(self, window: Window) -> None:
        if self._group_alive(window):
            self.end(window)

    @staticmethod
    def _group_alive(window: Window) -> bool:
        try:
            os.killpg(window.pgid, 0)
            return True
        except OSError:
            return False


__all__ = [
    "READY_WAIT", "TERM_GRACE", "ENTER_ALT_SCREEN", "LEAVE_ALT_SCREEN", "SHOW_CURSOR",
    "RESTORE_SCREEN", "JOB_CONTROL_SIGNALS", "Terminal", "Window", "WindowSession",
    "die_with_parent", "in_terminal_foreground", "terminal_fd", "window_argv",
]
