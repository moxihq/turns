"""`moxi-turns-ping`: the hook command a Codex host puts on its app-server's argv (§5.7).

Codex has no mods, so turns reaches a Codex session through the host it launches, and
the host takes the pings of Codex's own hooks (SessionStart, UserPromptSubmit, Stop,
Interrupt, PostCompact). This command renders every hook body through turns' hook code
(`moxi_turns.hooks`, §5.11). The host's side, and so the ping itself, is stage [B7]'s;
until a host stamps where to ping, the command reads the event and prints nothing: a
hook that adds no context and never fails the session.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from .hooks.run import run

    def body(event: dict) -> None:
        return None          # no host to ping until [B7] builds it

    args = argv if argv is not None else sys.argv[1:]
    return run(args[0] if args else "", body, seconds=5.0)


if __name__ == "__main__":
    sys.exit(main())
