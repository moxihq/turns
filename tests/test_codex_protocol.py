"""The socket-free part of the Codex app-server protocol."""

import pytest

from moxi_turns import codex_protocol as proto


def test_the_two_steer_refusals_are_typed():
    idle = proto.classify_error(
        {"code": -32600, "message": "no active turn to steer"}
    )
    assert isinstance(idle, proto.NoActiveTurn)

    mismatch = proto.classify_error({
        "code": -32600,
        "message": "expected active turn id `old` but found `live`",
    })
    assert isinstance(mismatch, proto.ActiveTurnMismatch)
    assert (mismatch.expected, mismatch.found) == ("old", "live")

    with pytest.raises(proto.CodexError):
        raise proto.classify_error({"code": -32600, "message": "Not initialized"})


def test_steer_uses_the_zero_id_as_its_idle_probe():
    assert proto.steer_params("thread-1", None, "hello") == {
        "threadId": "thread-1",
        "expectedTurnId": proto.ZERO_TURN_ID,
        "input": [{"type": "text", "text": "hello"}],
    }


def test_active_turn_discovery_ignores_terminal_turns():
    result = {"data": [
        {"id": "done", "status": "completed"},
        {"id": "live", "status": "inProgress"},
    ]}
    assert proto.active_turn_id(result) == "live"
    assert proto.active_turn_id({"data": result["data"][:1]}) is None


def test_thread_start_carries_only_the_inputs_the_launch_api_owns():
    assert proto.start_thread_params("/work", "be concise") == {
        "cwd": "/work",
        "approvalPolicy": "never",
        "developerInstructions": "be concise",
    }
    assert proto.resume_params("thread-1") == {
        "threadId": "thread-1", "excludeTurns": True, "approvalPolicy": "never",
    }


def test_thread_loads_carry_the_callers_options_without_rewriting_them():
    options = {"model": "sample", "sandbox": "workspace-write",
               "config": {"agents.enabled": False}}
    assert proto.start_thread_params("/work", "rules", options) == {
        **options, "cwd": "/work", "approvalPolicy": "never",
        "developerInstructions": "rules",
    }
    assert proto.resume_params("thread-1", "rules", options) == {
        **options, "threadId": "thread-1", "excludeTurns": True,
        "approvalPolicy": "never", "developerInstructions": "rules",
    }
