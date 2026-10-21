"""The socket protocol's framing, version and errors, and the values on it."""

from __future__ import annotations

import json
import socket

import pytest

import moxi_turns as turns
from moxi_turns import protocol, results


def raw(ep, *frames):
    """Send frames on one connection; read one reply each."""
    with protocol.connect(ep.sock_path) as conn:
        out = []
        for frame in frames:
            conn.send(frame)
            out.append(conn.recv(2))
        return out


def test_every_value_round_trips():
    values = [turns.Facts("s", 1, True, 2, False, 3.5, True), turns.Started("t"),
              turns.Refused("draft"), turns.Lost("x"), turns.Ended("t", "aborted"),
              turns.Ran("s2"), turns.Ran(), turns.Failed("x"), turns.Ready("clear", "s"),
              turns.TurnStarted("t", "s", "d"), turns.TurnEnded("t", "answer", "s", "d",
                                                                ("e", "f")),
              turns.Gone("x"), turns.StopFacts("t", "s", True, ("shell",), 2)]
    for v in values:
        assert results.from_wire(json.loads(json.dumps(v.to_wire()))) == v
    with pytest.raises(ValueError):
        results.from_wire({"kind": "nope"})


def test_version_unknown_op_and_bad_requests(make_endpoint):
    ep, _ = make_endpoint()
    v2, unknown, no_text, bad_epoch, bad_flag, ok = raw(
        ep, {"v": 2, "id": 1, "op": "hello"}, {"v": 1, "id": 2, "op": "dance"},
        {"v": 1, "id": 3, "op": "start"}, {"v": 1, "id": 4, "op": "start", "text": "x",
                                          "if_epoch": "0"},
        {"v": 1, "id": 5, "op": "deliver", "text": "x", "steer": "yes"},
        {"v": 1, "id": 6, "op": "hello"})
    assert (v2["ok"], v2["code"], v2["versions"]) == (False, "version", [1])
    assert unknown["code"] == "unknown-op" and unknown["id"] == 2
    assert no_text["code"] == bad_epoch["code"] == bad_flag["code"] == "bad-request"
    assert ok["ok"] and ok["id"] == 6 and ok["hello"]["nonce"] == ep.nonce


def test_a_frame_that_is_not_json_ends_the_connection(make_endpoint):
    ep, _ = make_endpoint()
    s = socket.socket(socket.AF_UNIX)
    s.connect(str(ep.sock_path))
    s.sendall(b"not json\n")
    conn = protocol.LineConn(s)
    reply = conn.recv(2)
    assert reply["code"] == "bad-request"
    assert conn.recv(2) is None
    conn.close()


def test_probe(make_endpoint, turns_env):
    ep, _ = make_endpoint()
    assert protocol.probe(ep.sock_path)[0] == protocol.LIVE
    assert protocol.probe(turns_env / "nothing")[0] == protocol.STALE


def test_check_rows(make_endpoint, turns_env):
    from conftest import write_config

    ep, _ = make_endpoint()
    rows = {r.name: r for r in turns.check()}
    assert rows["config"].ok and "hold.wait_ms 2000" in rows["config"].detail
    assert rows["endpoints"].detail.startswith("1 live")
    assert any(name.startswith("endpoint fake pid") for name in rows)
    write_config(turns_env, "[hold]\nwait_ms = 'soon'\n")
    rows = {r.name: r for r in turns.check()}
    assert not rows["config"].ok and "hold.wait_ms" in rows["config"].detail


def test_the_announcement_loads_with_interface_1():
    from moxi import modules

    modules.reset()
    found = modules.load("turns", interface=1)
    assert found.state == modules.PRESENT, found.reason
    assert found.api is turns and found.settings == "settings.toml"
