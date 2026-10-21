"""The stable command and terminal pieces of the Codex window."""

import threading

from moxi_turns.codex_window import RESTORE_SCREEN, WindowSession, window_argv


def test_window_attaches_to_the_hosts_socket_and_thread():
    assert window_argv("/bin/codex", "thread-1", "/run/app.sock") == [
        "/bin/codex", "-c", "check_for_update_on_startup=false", "resume",
        "thread-1", "--remote", "unix:///run/app.sock",
    ]


def test_terminal_restore_leaves_the_alternate_screen_and_shows_the_cursor():
    assert RESTORE_SCREEN == b"\x1b[?1049h\x1b[?1049l\x1b[?25h"


def test_ready_wait_prints_once_and_gives_up_with_current_reason():
    class Host:
        generation = None

        @staticmethod
        def give_up_reason():
            return "resume settings differ"

        def window_failed(self, reason):
            self.failed = reason

    host = Host()
    said = []
    window = WindowSession(host, binary="codex", cwd="/", env={}, ready_wait=0.01,
                           fd=99, say=said.append)
    assert window._wait_ready() is None
    assert len(said) == 1 and "waiting for the Codex thread" in said[0]
    assert host.failed == "resume settings differ"


def test_exited_window_checks_generation_before_calling_it_operator_close():
    class Generation:
        ready = threading.Event()
        ended = threading.Event()
        thread_id = "thread-1"

    Generation.ready.set()

    class Host:
        generation = Generation()

        def generation_answers(self, generation, timeout):
            self.checked = (generation, timeout)
            return False

        def drop_generation(self, generation):
            self.dropped = generation
            session._stop.set()

        def window_closed(self, status):
            raise AssertionError("a lost generation is not an operator close")

    class SyntheticWindowSession(WindowSession):
        def spawn(self, generation):
            return type("Window", (), {"exit_code": 0})()

        def poll(self, window):
            return True

        def reap_group(self, window):
            pass

        def end(self, window):
            pass

    host = Host()
    session = SyntheticWindowSession(host, binary="codex", cwd="/", env={}, fd=99)
    session.run()
    assert host.checked[0] is host.generation
    assert host.dropped is host.generation
