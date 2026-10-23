"""The values turns hands back: facts, delivery and command results, and session events.

Each is a frozen dataclass with a `to_wire()` and a `from_wire()`, so the endpoint writes
it on the socket and the client reads it back as the same value (the contract,
`contracts/socket-protocol.md`, spells each one). Callers reach them as `moxi_turns.<Name>`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any

#: What a session can be asked to do (`Session.can`).
CAPABILITIES = ("start", "steer", "hold", "clear", "exit")

#: How a turn ended (`Ended.outcome`, `TurnEnded.outcome`).
OUTCOMES = ("answer", "aborted", "error", "refusal")

#: Why a start, deliver or steer was refused (`Refused.reason`). `idle` is steer's alone:
#: no turn runs to take the text. `dropped`: another plugin's hook dropped the prompt.
DELIVERY_REFUSALS = ("busy-turn", "stale-turn", "draft", "busy-agents", "gone",
                     "unsupported", "dropped", "idle", "loading")

#: Why a session command was refused: the delivery reasons it shares, and three of a
#: command's own (no consent, one already in flight, one accepted in the last 60 s).
COMMAND_REFUSALS = ("busy-turn", "stale-turn", "draft", "busy-agents", "gone",
                    "unsupported", "not-allowed", "in-flight", "rate")

#: Where a `Ready` came from: a new session, a resume, a `/clear` or a compaction.
READY_SOURCES = ("startup", "resume", "clear", "compact")

#: The session commands.
COMMANDS = ("clear", "exit")


class _Wire:
    """`to_wire()` gives `{"kind": KIND, ...fields}`; `from_wire()` reads it back."""

    KIND = ""

    def to_wire(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.KIND}
        for f in fields(self):  # type: ignore[arg-type]
            value = getattr(self, f.name)
            out[f.name] = list(value) if isinstance(value, tuple) else value
        return out

    @classmethod
    def _build(cls, raw: dict[str, Any]):
        values = {}
        for f in fields(cls):  # type: ignore[arg-type]
            if f.name in raw:
                value = raw[f.name]
                values[f.name] = tuple(value) if isinstance(value, list) else value
        return cls(**values)


@dataclass(frozen=True)
class Facts(_Wire):
    """One observation of a session (`Session.facts()`).

    `epoch` is the current session's count of main-loop turns started; `turn_open` says
    one runs now; `agents` counts subagents pending, running or waiting, None when they
    could not be listed (unknown, which counts as busy); `draft` says the
    person has typed into the prompt box (False where the harness cannot tell); `at` is
    the wall-clock time of the read. `stale` is True when the turn ledger moved while the
    live facts were read, so the reading is not one moment.
    """

    KIND = "facts"
    session_id: str | None
    epoch: int
    turn_open: bool
    agents: int | None
    draft: bool
    at: float
    stale: bool = False


@dataclass(frozen=True)
class Started(_Wire):
    """A delivery went in: the harness turn that took it."""

    KIND = "started"
    turn_id: str | None


@dataclass(frozen=True)
class Refused(_Wire):
    """A delivery or command that did not go in, and why (one of the closed sets above)."""

    KIND = "refused"
    reason: str


#: The reason prefix of a `Lost` the endpoint decided while it lived: the delivery's
#: submit closed with no turn tied to it (a resolve with no turn held, a rejection, the
#: runner's bound). Any other `Lost` means the endpoint went away.
LOST_UNTIED = "untied"
#: The reason prefix of a `Lost` from the endpoint's own bound on an unsettled delivery.
LOST_EXPIRED = "expired"


@dataclass(frozen=True)
class Lost(_Wire):
    """turns cannot say which turn, if any, took the delivery.

    Either the endpoint went away first (a reload, a crash, the session ended), or the
    endpoint lived and the delivery's submit closed with no turn tied to it (reason
    `untied: …`), or the harness neither started nor refused it in time (reason
    `expired: …`). `untied` tells the two apart: True for the last two, where the
    endpoint is still there and only this delivery is unaccounted for.
    """

    KIND = "lost"
    reason: str

    @property
    def untied(self) -> bool:
        """True when the endpoint lived and decided this delivery reached no turn; False
        when the endpoint went away (or could not be reached)."""
        return self.reason.startswith((LOST_UNTIED, LOST_EXPIRED))


@dataclass(frozen=True)
class Ended(_Wire):
    """The turn that took a delivery ended, with its outcome (one of `OUTCOMES`)."""

    KIND = "ended"
    turn_id: str | None
    outcome: str


@dataclass(frozen=True)
class Ran(_Wire):
    """A session command ran. For `/clear`, the new session's id."""

    KIND = "ran"
    new_session_id: str | None = None


@dataclass(frozen=True)
class Failed(_Wire):
    """A session command was accepted but did not run, or no answer came in time (the
    caller's wait, or the endpoint's bound: reason `expired: …`)."""

    KIND = "failed"
    reason: str


@dataclass(frozen=True)
class Passed(_Wire):
    """The wire's answer to an `/exit` the harness's gate let through. It is not a
    result a caller sees: the library turns it into `Ran` once the harness process is gone,
    or `Failed("not-exited")` when it still runs after the bound."""

    KIND = "passed"


@dataclass(frozen=True)
class Ready(_Wire):
    """A session started, resumed, was cleared or compacted (`source`)."""

    KIND = "ready"
    source: str | None
    session_id: str | None


@dataclass(frozen=True)
class TurnStarted(_Wire):
    """A main-loop turn started, whoever caused it. `delivery_id` names the delivery that
    started it, or is None for the person's own turn."""

    KIND = "turn-started"
    turn_id: str | None
    session_id: str | None
    delivery_id: str | None = None


@dataclass(frozen=True)
class TurnEnded(_Wire):
    """A main-loop turn ended. `delivery_id` is the delivery that started it, if any;
    `steered` the deliveries steered into it."""

    KIND = "turn-ended"
    turn_id: str | None
    outcome: str
    session_id: str | None
    delivery_id: str | None = None
    steered: tuple[str, ...] = ()


@dataclass(frozen=True)
class Gone(_Wire):
    """The endpoint is gone: the session ended, or the endpoint went away."""

    KIND = "gone"
    reason: str


@dataclass(frozen=True)
class StopFacts(_Wire):
    """What the harness says at a Stop of the main loop, for the holder's `on_stop`.

    Each fact is None when the harness did not give it.
    """

    KIND = "stop"
    turn_id: str | None = None
    session_id: str | None = None
    stop_hook_active: bool | None = None
    task_types: tuple[str, ...] | None = None
    crons: int | None = None


@dataclass(frozen=True)
class SessionEvent:
    """What a listener is called with: the event and the session's facts.

    `event` is `ready`, `prompt`, `turn` or `gone`; `source` is a `ready`'s source;
    `turn_id` and `outcome` are a `turn`'s; `reason` is a `gone`'s. `env` is the session's
    environment, which the endpoint inherited from the session.
    """

    event: str
    harness: str
    session_id: str | None
    env: dict[str, str] = field(repr=False, default_factory=dict)
    source: str | None = None
    turn_id: str | None = None
    outcome: str | None = None
    reason: str | None = None


_KINDS = {cls.KIND: cls for cls in (Facts, Started, Refused, Lost, Ended, Ran, Failed,
                                     Passed, Ready, TurnStarted, TurnEnded, Gone,
                                     StopFacts)}


def from_wire(raw: Any):
    """The value a `to_wire()` dict stands for. Raises `ValueError` on anything else."""
    if not isinstance(raw, dict) or raw.get("kind") not in _KINDS:
        raise ValueError(f"not a turns value: {raw!r}")
    try:
        return _KINDS[raw["kind"]]._build(raw)
    except TypeError as exc:
        raise ValueError(f"malformed {raw.get('kind')} value: {exc}") from None


__all__ = ["CAPABILITIES", "OUTCOMES", "DELIVERY_REFUSALS", "COMMAND_REFUSALS",
           "READY_SOURCES", "COMMANDS", "LOST_UNTIED", "LOST_EXPIRED", "Facts", "Started",
           "Refused", "Lost", "Ended", "Ran", "Failed", "Passed", "Ready", "TurnStarted", "TurnEnded", "Gone", "StopFacts",
           "SessionEvent", "from_wire"]
