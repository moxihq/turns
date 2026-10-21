"""The announcement of Moxi's turns module, which moxi.modules reads.

turns is a library with no user command: other modules call `moxi_turns`, and the
internal entry points the harness adapters add are spawned, never typed.
"""

INTERFACES = frozenset({1})
API = "moxi_turns"
CLI = None
SUMMARY = "Drive a coding-agent session's turns: start, hold, steer and read a turn on any harness."
OPTIONAL = {"codex": "launch Codex sessions through a supervised app-server host"}
GATED = {"moxi-turns-ping": "codex"}
INTERNAL_ENV = {
    "MOXI_TURNS_COMMANDS": "a launcher's grant of session commands (clear, exit) to the "
                           "one launch it stamps",
}
SETTINGS = "settings.toml"
SETUP = "moxi_turns.claude_setup:setup"
SEGMENT = None
