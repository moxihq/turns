"""What an adapter gives the endpoint core: the harness half of an endpoint.

One adapter per harness: the Claude Code endpoint half, the Codex host, the
OpenCode host. The endpoint core (`endpoint.Endpoint`) owns the socket, the turn ledger,
the deliveries, the holder and the listeners; the adapter owns the harness. They meet
here.

The core calls the adapter:

    attach(endpoint)      once, before the socket opens
    live()                the live facts: subagents and the prompt box (`Live`)
    submit(delivery)      put the text in as a new turn (a start, or a deliver)
    append(delivery)      put the text into the running turn (a steer)
    command(request)      run `/clear` or `/exit`
    expired(delivery_id)  the core expired a delivery it handed (`PENDING_TTL_S`): the
                          adapter lets go of it (the base does nothing)
    close()               the endpoint is stopping

`submit`, `append` and `command` return at once; what happened comes back through the
endpoint's adapter-side calls:

    endpoint.ready(source, session_id, announce=True) -> str | None
                                          a start, resume, clear or compaction; returns
                                          the listeners' added context; `announce=False`
                                          raises no Ready (the host raised it already)
    endpoint.identify(session_id, ready=None)
                                          the harness learned its session id before
                                          a Ready event; updates addressing, and with
                                          `ready` raises Ready with that source and
                                          asks no listener (an accepted load)
    endpoint.prompt() -> str | None       a UserPromptSubmit; returns the added context
    endpoint.turn_started(turn_id, delivery_id=None)
                                          a main-loop turn started; the adapter's tie
                                          names the delivery that took it
    endpoint.turn_started(turn_id, held=True)
                                          a main-loop turn started whose delivery, if
                                          any, is decided later (Claude Code's tie):
                                          the ledger opens it now, its events wait
    endpoint.turn_tied(turn_id, delivery_id) / endpoint.turn_released(turn_id)
                                          that decision: the held turn took the
                                          delivery, or is the person's; its held
                                          events go out in order
    endpoint.lost(delivery_id, reason)    the adapter cannot say which turn, if any,
                                          took a delivery (reason `untied: …`)
    endpoint.learn_session(session_id)    the adapter learned the session's id
    endpoint.turn_ended(turn_id, outcome) the open main-loop turn ended
    endpoint.entering(delivery_id) -> str | None
                                          a start is about to take effect: the ledger
                                          re-check; a reason refuses it
    endpoint.refused(delivery_id, reason) the adapter refused a delivery it was handed
    endpoint.steer_missed(delivery_id)     a steer raced the turn's end; detach it from
                                           that turn and take the ordinary deliver path
    endpoint.on_stop(facts) -> str | None a Stop of the main loop: the holder's answer
    endpoint.command_entering(request_id) -> str | None
                                          a command is about to take effect: the re-check
    endpoint.command_settled(request_id, result)
                                          a command ran (`Ran`), failed or was refused
    endpoint.gone(reason)                 the session ended

The adapter's duty to settle: every delivery handed to `submit` ends in exactly one
`turn_started(…, delivery.id)` or `turn_tied(…, delivery.id)` (the tie),
`refused(delivery.id, reason)` or `lost(delivery.id, reason)`, and every
request handed to `command` in one `command_settled(request.id, result)`. A `submit` or
`command` that raises counts as settled (the delivery is refused `gone`, the command
fails). Until it settles, a delivery holds the endpoint's one in-flight slot (later
starts refuse `busy-turn`, later delivers queue) and a command holds the one command
slot (later commands refuse `in-flight`). The core does not trust this duty: past
`endpoint.PENDING_TTL_S` it settles the delivery `Lost` or the command `Failed` with
reason `expired`, frees the slot, ignores the adapter's late word about it, and answers
`expired` to its `entering` or `command_entering`, which the adapter takes as a refusal.
A steer (`append`) holds no slot; it ends with the turn it went into. An adapter whose
steer gets an atomic "no active turn" refusal calls `steer_missed` before reporting the
racing turn's end. The core then delivers the text as one new turn.

A submitted start carries its preconditions (`Delivery.refuse_draft`,
`Delivery.refuse_agents`), which the adapter checks with the live facts where the start
takes effect, after `entering` passes.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Live:
    """The facts the ledger does not hold. `agents` counts subagents pending, running or
    waiting, or is None when the harness could not list them; `draft` says the person has
    typed into the prompt box (an adapter that cannot read the box says True).

    Rule: an unreadable subagent list is unknown, and unknown counts as
    busy. It never reads as 0: `facts().agents` is None, a start that asked for the
    subagent refusal refuses `busy-agents`, and a session command refuses `busy-agents`."""

    agents: int | None = 0
    draft: bool = False


@dataclass(frozen=True)
class Delivery:
    """One text handed to the adapter."""

    id: str
    text: str
    kind: str                    # "start", "deliver" or "steer"
    refuse_draft: bool = False
    refuse_agents: bool = False
    fallback: bool = False       # a steer-first deliver may become a new turn


@dataclass(frozen=True)
class CommandRequest:
    """One session command handed to the adapter."""

    id: str
    name: str                    # "clear" or "exit"


class Harness:
    """The base an adapter builds on. Every method but `attach` is the adapter's to write."""

    #: "claude", "codex" or "opencode".
    name: str = ""
    #: What this session can do: a subset of `results.CAPABILITIES`.
    can: frozenset[str] = frozenset()
    #: The harness process's pid, which `find(pid=…)` matches.
    pid: int = 0
    #: The session's working directory.
    cwd: str = ""
    #: The version of the harness-side code the endpoint talks to (the Claude runner), or
    #: None.
    mod_version: str | None = None

    def attach(self, endpoint) -> None:
        self.endpoint = endpoint

    def live(self) -> Live:
        return Live()

    def loading(self) -> bool:
        """Whether this adapter has no accepted load to receive input."""
        return False

    def submit(self, delivery: Delivery) -> None:
        raise NotImplementedError

    def append(self, delivery: Delivery) -> None:
        raise NotImplementedError

    def command(self, request: CommandRequest) -> None:
        raise NotImplementedError

    def expired(self, delivery_id: str) -> None:
        pass

    def close(self) -> None:
        pass


__all__ = ["Live", "Delivery", "CommandRequest", "Harness"]
