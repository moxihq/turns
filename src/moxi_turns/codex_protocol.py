"""Pure helpers for the Codex app-server JSON-RPC protocol.

The app-server's Unix socket carries WebSockets, not JSON lines. A connection must
initialize before any other request and must start or resume a thread to receive that
thread's notifications. Delivery is steer-first because a turn can begin between an
idle reading and an input request.
"""

from __future__ import annotations

import re

JSONRPC = "2.0"
INVALID_REQUEST = -32600
NO_ACTIVE_TURN_MESSAGE = "no active turn to steer"
_MISMATCH_RE = re.compile(r"expected active turn id `([^`]*)` but found `([^`]*)`")
ZERO_TURN_ID = "00000000-0000-0000-0000-000000000000"

STATUS_COMPLETED = "completed"
STATUS_IN_PROGRESS = "inProgress"
TERMINAL_STATUSES = ("completed", "interrupted", "failed")


class AppServerOffline(Exception):
    """The app-server socket is gone, closed, or did not answer in time."""


class NoReply(AppServerOffline):
    """A request went out and no answer came: the connection closed after the send, or
    the wait ran out. Codex may have acted on it, so a delivery it carried is not a
    refusal (RULE K1). Plain `AppServerOffline` means the request was never sent."""


class CodexError(Exception):
    """A JSON-RPC error returned by the app-server."""

    def __init__(self, code: int, message: str, data=None):
        super().__init__(f"{message} (code {code})")
        self.code = code
        self.message = message
        self.data = data


class NoActiveTurn(CodexError):
    """The thread is idle, so delivery may fall back to ``turn/start``."""


class ActiveTurnMismatch(CodexError):
    """A steer named a stale turn; the refusal names the live one."""

    def __init__(self, code: int, message: str, expected: str, found: str, data=None):
        super().__init__(code, message, data)
        self.expected = expected
        self.found = found


def classify_error(error: dict) -> CodexError:
    code = error.get("code", 0)
    message = str(error.get("message", ""))
    data = error.get("data")
    if code == INVALID_REQUEST:
        match = _MISMATCH_RE.search(message)
        if match:
            return ActiveTurnMismatch(code, message, match.group(1), match.group(2), data)
        if NO_ACTIVE_TURN_MESSAGE in message:
            return NoActiveTurn(code, message, data)
    return CodexError(code, message, data)


def request(rid, method: str, params=None) -> dict:
    frame = {"jsonrpc": JSONRPC, "id": rid, "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def notification(method: str, params=None) -> dict:
    frame = {"jsonrpc": JSONRPC, "method": method}
    if params is not None:
        frame["params"] = params
    return frame


def text_input(text: str) -> list[dict[str, str]]:
    return [{"type": "text", "text": text}]


def initialize_params(client_name: str, version: str) -> dict:
    return {"clientInfo": {"name": client_name, "version": version},
            "capabilities": {"experimentalApi": True}}


def resume_params(thread_id: str, instructions: str | None = None,
                  options=None) -> dict:
    params = dict(options or {})
    params.update({"threadId": str(thread_id), "excludeTurns": True,
                   "approvalPolicy": "never"})
    if instructions:
        params["developerInstructions"] = instructions
    return params


def start_thread_params(cwd: str, instructions: str | None = None,
                        options=None) -> dict:
    params = dict(options or {})
    params.update({"cwd": cwd, "approvalPolicy": "never"})
    if instructions:
        params["developerInstructions"] = instructions
    return params


def steer_params(thread_id: str, expected_turn_id: str | None, text: str) -> dict:
    return {"threadId": str(thread_id),
            "expectedTurnId": str(expected_turn_id or ZERO_TURN_ID),
            "input": text_input(text)}


def start_params(thread_id: str, text: str) -> dict:
    return {"threadId": str(thread_id), "input": text_input(text)}


def thread_is_active(thread: dict | None) -> bool:
    return bool(thread and (thread.get("status") or {}).get("type") == "active")


def active_turn_id(turns_result: dict | None) -> str | None:
    for turn in (turns_result or {}).get("data") or []:
        if turn.get("status") == STATUS_IN_PROGRESS:
            return turn.get("id")
    return None


__all__ = [
    "JSONRPC", "INVALID_REQUEST", "NO_ACTIVE_TURN_MESSAGE", "ZERO_TURN_ID",
    "STATUS_COMPLETED", "STATUS_IN_PROGRESS", "TERMINAL_STATUSES", "AppServerOffline",
    "NoReply", "CodexError", "NoActiveTurn", "ActiveTurnMismatch", "classify_error",
    "request", "notification",
    "text_input", "initialize_params", "resume_params", "start_thread_params",
    "steer_params", "start_params", "thread_is_active", "active_turn_id",
]
