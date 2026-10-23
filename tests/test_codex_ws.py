"""The WebSocket request loop on a synthetic Unix app-server."""

from __future__ import annotations

import json
import threading

import pytest
from websockets.sync.server import unix_serve

from moxi_turns import codex_protocol as proto
from moxi_turns.codex_ws import AppServerClient


def test_initialize_requests_and_notifications_share_one_connection(tmp_path):
    path = tmp_path / "app.sock"
    initialized = threading.Event()

    def handler(conn):
        for raw in conn:
            message = json.loads(raw)
            if message.get("method") == "initialize":
                conn.send(json.dumps({"id": message["id"], "result": {"ready": True}}))
            elif message.get("method") == "initialized":
                initialized.set()
                conn.send(json.dumps({"method": "thread/status/changed",
                                      "params": {"status": {"type": "idle"}}}))
            elif message.get("method") == "echo":
                conn.send(json.dumps({"id": message["id"],
                                      "result": {"value": message["params"]}}))
            elif message.get("method") == "fail":
                conn.send(json.dumps({"id": message["id"], "error": {
                    "code": -32000, "message": "synthetic failure",
                }}))

    server = unix_serve(handler, path=path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    notes = []
    client = AppServerClient(
        path, on_notification=lambda method, params, rid=None: notes.append(
            (method, params)
        ), timeout=2,
    ).connect()
    try:
        assert client.initialize() == {"ready": True}
        assert initialized.wait(1)
        assert client.call("echo", "hello") == {"value": "hello"}
        with pytest.raises(proto.CodexError, match="synthetic failure"):
            client.call("fail")
        for _ in range(100):
            if notes:
                break
            threading.Event().wait(0.01)
        assert notes == [("thread/status/changed", {"status": {"type": "idle"}})]
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=2)


def test_a_call_says_whether_its_request_went_out(tmp_path):
    # A request never sent (not connected, the send raised) raises plain
    # AppServerOffline; one sent and never answered (the connection closed after the
    # send, or the wait ran out) raises NoReply, its subclass; an error answer raises
    # CodexError.
    path = tmp_path / "app.sock"

    def handler(conn):
        for raw in conn:
            message = json.loads(raw)
            if message.get("method") == "drop":
                conn.close()
                return
            if message.get("method") == "fail":
                conn.send(json.dumps({"id": message["id"], "error": {
                    "code": -32000, "message": "synthetic failure",
                }}))
            # "silent" is never answered

    server = unix_serve(handler, path=path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    class BrokenSend:
        def send(self, data):
            raise OSError("broken pipe")

        def close(self):
            pass

    try:
        client = AppServerClient(path, timeout=2)
        with pytest.raises(proto.AppServerOffline) as caught:
            client.call("echo")
        assert not isinstance(caught.value, proto.NoReply)

        client = AppServerClient(path, timeout=2).connect()
        with pytest.raises(proto.CodexError, match="synthetic failure"):
            client.call("fail")
        with pytest.raises(proto.NoReply, match="timed out"):
            client.call("silent", timeout=0.05)
        with pytest.raises(proto.NoReply, match="closed the connection"):
            client.call("drop")
        client.close()

        client = AppServerClient(path, timeout=2).connect()
        real, client._conn = client._conn, BrokenSend()
        with pytest.raises(proto.AppServerOffline, match="send failed") as caught:
            client.call("echo")
        assert not isinstance(caught.value, proto.NoReply)
        real.close()
    finally:
        server.shutdown()
        thread.join(timeout=2)


def test_a_frame_with_unicode_line_breaks_inside_a_string_is_read_whole(tmp_path):
    # The app-server writes JSON with raw non-ASCII text, and U+0085, U+2028, U+2029 and
    # \x1c to \x1e may sit unescaped inside a string. Only "\n" ends a frame; splitting
    # on the others cut such a frame and dropped it as non-JSON.
    text = "a\u0085b c d\x1ce\x1df\x1eg"
    path = tmp_path / "app.sock"

    def handler(conn):
        for raw in conn:
            message = json.loads(raw)
            if message.get("method") == "echo":
                conn.send(json.dumps({"method": "item/completed",
                                      "params": {"text": text}}, ensure_ascii=False)
                          + "\n" + json.dumps({"id": message["id"],
                                               "result": {"value": text}},
                                              ensure_ascii=False))

    server = unix_serve(handler, path=path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    notes = []
    client = AppServerClient(
        path, on_notification=lambda method, params, rid=None: notes.append(
            (method, params)
        ), timeout=2,
    ).connect()
    try:
        assert client.call("echo") == {"value": text}
        assert notes == [("item/completed", {"text": text})]
    finally:
        client.close()
        server.shutdown()
        thread.join(timeout=2)
