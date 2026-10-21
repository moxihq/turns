"""App-server liveness and child ownership, with synthetic processes."""

import socket
import sys

import pytest

from moxi_turns.codex_appserver import AppServer, AppServerBusy, socket_answers


def test_a_socket_file_is_not_liveness(tmp_path):
    path = tmp_path / "app.sock"
    path.touch()
    assert not socket_answers(path, timeout=0.01)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    path.unlink()
    server.bind(str(path))
    server.listen(1)
    try:
        assert socket_answers(path)
    finally:
        server.close()


def test_a_live_socket_is_never_replaced(tmp_path):
    path = tmp_path / "app.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(path))
    listener.listen(1)
    child = AppServer(
        path, argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        with pytest.raises(AppServerBusy):
            child.start()
        assert child.process is None
    finally:
        listener.close()


def test_stop_signals_only_the_child_the_server_spawned(tmp_path):
    path = tmp_path / "app.sock"
    child = AppServer(
        path, argv=[sys.executable, "-c", "import time; time.sleep(30)"],
    ).start()
    pid = child.process.pid
    child.stop(grace=0.2)
    assert child.process is None
    assert pid > 0
