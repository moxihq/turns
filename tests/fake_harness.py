"""A fake harness: an adapter whose turns the test starts and ends by hand.

With `auto` on, a handed start or deliver enters at once (the gate's re-check, then the
start's own draft and subagent checks, then the turn starts with the tie); with it off the
test calls `enter()` itself, so a turn can be typed between the submit and the entry.
"""

from __future__ import annotations

import itertools
import os
import threading

from moxi_turns.harness import CommandRequest, Delivery, Harness, Live
from moxi_turns.results import Ran


class FakeHarness(Harness):
    name = "fake"

    def __init__(self, can=("start", "steer", "hold", "clear", "exit"), *, auto=True,
                 pid=None):
        self.can = frozenset(can)
        self.pid = os.getpid() if pid is None else pid
        self.cwd = os.getcwd()
        self.auto = auto
        self.agents = 0
        self.draft = False
        self.live_fails = False
        self.submitted: list[Delivery] = []
        self.appended: list[Delivery] = []
        self.commands: list[CommandRequest] = []
        self.auto_commands = True
        self.open_turn: str | None = None
        self._turns = itertools.count(1)
        self._sessions = itertools.count(2)
        self._lock = threading.Lock()

    # -- the core calls these ----------------------------------------------------------

    def live(self) -> Live:
        if self.live_fails:
            raise OSError("the prompt box cannot be read")
        return Live(self.agents, self.draft)

    def submit(self, delivery: Delivery) -> None:
        self.submitted.append(delivery)
        if self.auto:
            self.enter(delivery)

    def append(self, delivery: Delivery) -> None:
        self.appended.append(delivery)

    def command(self, request: CommandRequest) -> None:
        self.commands.append(request)
        if self.auto_commands:
            self.run_command(request)

    # -- the test drives these ---------------------------------------------------------

    def enter(self, delivery: Delivery) -> str | None:
        """The prompt enters the session: the re-check, then the start's own checks."""
        reason = self.endpoint.entering(delivery.id)
        if reason is None and delivery.refuse_draft and self.draft:
            reason = "draft"
        if reason is None and delivery.refuse_agents and self.agents:
            reason = "busy-agents"
        if reason is not None:
            self.endpoint.refused(delivery.id, reason)
            return reason
        self.start_turn(delivery.id)
        return None

    def start_turn(self, delivery_id: str | None = None) -> str:
        turn_id = f"t{next(self._turns)}"
        self.open_turn = turn_id
        self.endpoint.turn_started(turn_id, delivery_id)
        return turn_id

    def end_turn(self, outcome: str = "answer") -> None:
        turn_id, self.open_turn = self.open_turn, None
        self.endpoint.turn_ended(turn_id, outcome)

    def run_command(self, request: CommandRequest) -> None:
        if self.endpoint.command_entering(request.id) is not None:
            return
        if request.name == "clear":
            new_id = f"s-{next(self._sessions)}"
            self.endpoint.ready("clear", new_id)
            self.endpoint.command_settled(request.id, Ran(new_id))
        else:
            self.endpoint.command_settled(request.id, Ran(None))
