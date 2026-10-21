"""turns: the one interface to each harness's session (Moxi's `moxi_turns`).

Only turns puts input into a session (a turn, mail, `/clear`, `/exit`, and the context a
module adds at a session's start or at a prompt), and only turns takes session events
out of the harness. This package is turns' whole public API, the announcement's
`API = "moxi_turns"`; every submodule is internal.

    import moxi_turns as turns

    s = turns.find(session_id="…")   # or find(pid=…); turns.sessions() lists live ones
    s.harness                        # "claude" | "codex" | "opencode"
    s.can                            # frozenset of: start, steer, hold, clear, exit
    f = s.facts()                    # Facts(session_id, epoch, turn_open, agents, draft, at)

    d = s.start(text, *, if_epoch=None, refuse_draft=False, refuse_agents=False)
    d = s.deliver(text, *, steer=False)
    d = s.steer(text)
    d.wait_started(timeout)  # Started(turn_id) | Refused(reason) | Lost(reason)
    d.wait_ended(timeout)    # Ended(turn_id, outcome) | Refused(reason) | Lost(reason)

    r = s.command("clear" | "exit", *, if_epoch=None)  # Ran(new_session_id) | Refused | Failed
    for ev in s.watch(): ...  # Ready(source, session_id) | TurnStarted | TurnEnded | Gone
    h = s.hold(on_stop)       # one holder per session; h.close()
    turns.check()             # rows for a doctor
    turns.claude_plugin_dirs()  # the Claude plugin folders, for a per-launch --plugin-dir
    s = turns.opencode.launch(cwd, *, instructions=None, env=None, options=None)
                              # an OpenCode host, experimental (§5.7)

The wire under it is turns' socket protocol, `contracts/socket-protocol.md`.

The package imports nothing when it loads: each name above loads its submodule on first
use (PEP 562). A hook process imports `moxi_turns.hooks`, which pays this package first,
and must not pay the core, sockets or threads with it (the hooks' law L3).
"""

from __future__ import annotations

#: Each public name and the submodule that holds it.
_WHERE = {
    "codex": "codex",
    **dict.fromkeys(("find", "sessions", "Session", "Delivery", "Hold", "TurnsError"),
                    "client"),
    **dict.fromkeys(("check", "Row"), "doctor"),
    "claude_plugin_dirs": "claude_setup",
    "opencode": "opencode",     # a host's namespace (`turns.opencode.launch`): the module
    **dict.fromkeys(("Facts", "Started", "Refused", "Lost", "Ended", "Ran", "Failed",
                     "Ready", "TurnStarted", "TurnEnded", "Gone", "StopFacts",
                     "SessionEvent", "CAPABILITIES", "OUTCOMES", "DELIVERY_REFUSALS",
                     "COMMAND_REFUSALS", "READY_SOURCES"), "results"),
}

__all__ = [
    "find", "sessions", "check", "codex", "claude_plugin_dirs", "opencode", "Session", "Delivery", "Hold",
    "Row", "TurnsError",
    "Facts", "Started", "Refused", "Lost", "Ended", "Ran", "Failed", "Ready",
    "TurnStarted", "TurnEnded", "Gone", "StopFacts", "SessionEvent",
    "CAPABILITIES", "OUTCOMES", "DELIVERY_REFUSALS", "COMMAND_REFUSALS", "READY_SOURCES",
]


def __getattr__(name: str):
    if name == "codex":
        value = __import__(f"{__name__}.codex", fromlist=[name])
        globals()[name] = value
        return value
    where = _WHERE.get(name)
    if where is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module = __import__(f"{__name__}.{where}", fromlist=[name])
    # A name that is its submodule's own name is a host's namespace: the module itself.
    value = module if where == name else getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted({*globals(), *__all__})
