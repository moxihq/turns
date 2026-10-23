# turns

turns is the Moxi module that drives a coding agent's session. Other modules call it to
put input into a session (a new turn, a delivery held until the running turn ends, a
steer into the running turn, `/clear` and `/exit`) and to take session events out: the
session ready, a turn started or ended, the session gone. A holder can be asked at each
Stop before the turn ends, and a registered module can add context at a session's start
or at a prompt. Each session turns can reach has an endpoint with a Unix socket and a
ledger of the session's turns (a small process of its own for a Claude Code session, and
part of the process that called `launch()` for a Codex or OpenCode session), so whether a
session is idle and whether a delivered turn ran are read from the harness's own turn
events. Claude Code sessions are
reached through two Claude Code plugins that ship inside the package; Codex sessions run
under a host turns launches; OpenCode sessions run under an experimental host. turns is
a library with no command of its own. Its API is the `moxi_turns` package, and the wire
under it is described in [contracts/socket-protocol.md](contracts/socket-protocol.md).

## Install

```sh
uv tool install moxi
moxi install turns
```

`moxi install turns` installs turns with `moxi`, runs its setup, and offers the `codex`
extra. The setup writes the Claude plugin's `endpoint.json` and installs both Claude Code
plugins at user scope with `claude plugin`, so it needs `claude` on PATH for that step. A
launcher can instead load the plugins for one launch with `--plugin-dir`, using
`moxi_turns.claude_plugin_dirs()`.

### Before turns is on PyPI

Install the core from git, then name turns' repo with `--from`:

```sh
uv tool install git+https://github.com/moxihq/moxi
moxi install turns --from 'moxi-turns @ git+https://github.com/moxihq/turns'
```

## Requirements

- Linux and Python 3.11 or later.
- `moxi` (0.1).
- The `codex` extra adds `websockets`, which the Codex host needs; it also needs `codex`
  on PATH.
- Claude Code sessions need Claude Code with the two plugins loaded; OpenCode sessions
  need `opencode` on PATH or in `~/.opencode/bin`.

## License

MIT. See [LICENSE](LICENSE).
