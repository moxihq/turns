"""The turn ledger: one per endpoint, the one place idle and ran are read from.

The adapter feeds it every main-loop turn start and end with the harness's turn id, and
says which delivery a turn took (the tie, per harness). `facts()`, a start's
preconditions and every `wait_ended` read it; nothing reads a timer, a status line or a
guess from outside the session.

`epoch` is the current session's count of main-loop turns started. A new session id (a
`/clear`, a resume into another session) starts it again at 0, so it answers "has this
session run a turn". `version` moves on every change, a new session id included, so a
start that checked one reading and lands at another knows the ledger moved under it.

The ledger holds no lock of its own: the endpoint's lock guards it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Reading:
    """One read of the ledger."""

    version: int
    session_id: str | None
    epoch: int
    turn_open: bool
    turn_id: str | None


@dataclass
class Turn:
    """One main-loop turn, open or ended."""

    turn_id: str | None
    session_id: str | None
    delivery_id: str | None = None
    steered: list[str] = field(default_factory=list)
    outcome: str | None = None
    #: The turn's events wait for a decision about which delivery, if any, it took:
    #: the ledger has it open or ended, `watch()` has not seen it yet.
    held: bool = False
    #: Whether a held turn's end has been recorded (its `TurnEnded` waits too).
    ended_held: bool = False
    #: The harness was told the turn may end (a Codex Stop answered unblocked). The turn
    #: stays open until the harness says it ended, but takes no more steers.
    ending: bool = False


class TurnLedger:
    def __init__(self, session_id: str | None = None):
        self.version = 0
        self.session_id = session_id
        self.earlier: list[str] = []
        self.epoch = 0
        self.open: Turn | None = None

    def read(self) -> Reading:
        return Reading(self.version, self.session_id, self.epoch, self.open is not None,
                       self.open.turn_id if self.open else None)

    def new_session(self, session_id: str | None) -> bool:
        """The session's id is now `session_id`. True when it changed."""
        if not session_id or session_id == self.session_id:
            return False
        if self.session_id is None:
            # The first id this launch learns (on Claude Code the runner sends it once the
            # endpoint is up, and a turn may already have started): the same session, so
            # its count stands and any open turn joins it.
            self.session_id = session_id
            if self.open is not None and self.open.session_id is None:
                self.open.session_id = session_id
            self.version += 1
            return True
        if self.session_id not in self.earlier:
            self.earlier.append(self.session_id)
        self.session_id = session_id
        self.epoch = 0
        self.version += 1
        return True

    def started(self, turn_id: str | None, delivery_id: str | None, *,
                held: bool = False) -> tuple[Turn | None, Turn]:
        """A main-loop turn started. Returns `(previous, turn)`: `previous` is a turn left
        open without an end (a missed end), which this closes with outcome `error`.
        `held`: the turn's events wait for the tie."""
        previous = None
        if self.open is not None:
            previous = self.open
            previous.outcome = "error"
        self.open = Turn(turn_id, self.session_id, delivery_id, held=held)
        self.epoch += 1
        self.version += 1
        return previous, self.open

    def steered_into(self, delivery_id: str) -> Turn | None:
        """A delivery was steered into the open turn. The turn, or None when none is open
        or the open one is ending."""
        if self.open is None or self.open.ending:
            return None
        self.open.steered.append(delivery_id)
        return self.open

    def ending(self) -> Turn | None:
        """The open turn may end at any moment: it stays open and takes no more steers.
        The turn, or None when none is open. The reading does not change, so `version`
        does not move."""
        if self.open is not None:
            self.open.ending = True
        return self.open

    def ended(self, outcome: str) -> Turn | None:
        """The open turn ended with `outcome`. The turn, or None when none was open."""
        turn = self.open
        if turn is None:
            return None
        turn.outcome = outcome
        self.open = None
        self.version += 1
        return turn


__all__ = ["Reading", "Turn", "TurnLedger"]
