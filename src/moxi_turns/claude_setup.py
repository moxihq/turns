"""turns' Claude plugin on disk: `SETUP`, `claude_plugin_dirs()`, and its doctor rows.

The plugin ships inside the package as a folder marketplace, `moxi_turns/claude/`,
holding `.claude-plugin/marketplace.json` and two plugins: the runner (`moxi-turns`)
and the gate (`moxi-turns-gate`) (§5.10, [R28]).

`SETUP` has two steps (§5.10):

1. Write `endpoint.json` beside the runner: the absolute endpoint command, which is
   `claude.endpoint` when the key is set and otherwise this environment's
   `moxi-turns-endpoint` (`moxi.paths.script`, never a PATH lookup, [R19]). Every run
   rewrites it, so a changed key or a moved environment takes effect at the next
   `moxi install turns`. The file is generated and never tracked.
2. `claude plugin marketplace add <folder>`, then install both plugins at user scope,
   skipping each one already done.

Every load of the plugin needs step 1; only a user-scope install needs step 2. A
launcher that loads the plugins for one launch calls `claude_plugin_dirs()`, which does
step 1 for the environment it runs in and returns both plugin folders for `--plugin-dir`.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger("moxi.turns.claude")

#: The marketplace and plugin names (`claude plugin install moxi-turns@moxi-turns`).
MARKETPLACE = "moxi-turns"
RUNNER = "moxi-turns"
GATE = "moxi-turns-gate"
#: The file the runner reads through `${CLAUDE_PLUGIN_ROOT}` (§5.7).
ENDPOINT_FILE = "endpoint.json"
#: The endpoint's console script.
ENDPOINT_SCRIPT = "moxi-turns-endpoint"
#: The Claude Code versions the probes passed on ([R33], `build/probes-b6.md`).
PASSED_VERSIONS = ("2.1.296",)
#: The bound on each `claude` command SETUP and the rows run.
CLAUDE_TIMEOUT_S = 60.0


class SetupError(Exception):
    """A setup step that could not be done, with why."""


def claude_root() -> Path:
    """The folder marketplace inside the package. A test points this at a copy of its
    own, so no test writes the `endpoint.json` of the environment it runs in."""
    return Path(__file__).resolve().parent / "claude"


def plugin_dir(name: str, root: Path | None = None) -> Path:
    """A plugin's folder, as the marketplace names it (`source`), else `<root>/<name>`."""
    root = claude_root() if root is None else root
    try:
        market = json.loads((root / ".claude-plugin" / "marketplace.json")
                            .read_text(encoding="utf-8"))
        for entry in market.get("plugins") or ():
            if isinstance(entry, dict) and entry.get("name") == name:
                source = entry.get("source")
                if isinstance(source, str) and source:
                    return (root / source).resolve()
    except (OSError, ValueError, AttributeError):
        pass
    return root / name


def endpoint_command(env: dict[str, str] | None = None) -> Path:
    """The endpoint command this environment and turns' config give now. Raises
    `SetupError` when there is none."""
    from moxi import paths
    from moxi.config import ConfigError

    from . import config

    try:
        configured = config.reader(env).start()["claude.endpoint"]
    except ConfigError as exc:
        raise SetupError(f"turns' config cannot be read: {exc}") from None
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            raise SetupError(f"claude.endpoint {configured!r} is not an absolute path")
        return path
    try:
        return paths.script(ENDPOINT_SCRIPT)
    except FileNotFoundError as exc:
        raise SetupError(str(exc)) from None


def write_endpoint_file(root: Path | None = None, env: dict[str, str] | None = None) -> Path:
    """SETUP's first step: write `endpoint.json` beside the runner. Returns its path."""
    command = endpoint_command(env)
    target = plugin_dir(RUNNER, root) / ENDPOINT_FILE
    if not target.parent.is_dir():
        raise SetupError(f"the runner plugin's folder {target.parent} does not exist")
    from .endpoint import package_version

    data = json.dumps({"command": str(command), "environment": sys.prefix,
                       "version": package_version()}, indent=1) + "\n"
    tmp = target.with_name(f".{ENDPOINT_FILE}.tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        raise SetupError(f"cannot write {target}: {exc}") from None
    return target


def read_endpoint_file(root: Path | None = None) -> dict | None:
    try:
        data = json.loads((plugin_dir(RUNNER, root) / ENDPOINT_FILE).read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def claude_plugin_dirs() -> tuple[Path, Path]:
    """The runner's and the gate's folders, for a per-launch `--plugin-dir` each.

    First writes `endpoint.json` for the environment this runs in (SETUP's first step),
    so a per-launch load gets both plugins and an endpoint with no user-scope install.
    Raises `moxi_turns.TurnsError` when it cannot be written.
    """
    from .client import TurnsError

    root = claude_root()
    try:
        write_endpoint_file(root)
    except SetupError as exc:
        raise TurnsError(f"the Claude plugin cannot be set up: {exc}") from None
    return plugin_dir(RUNNER, root), plugin_dir(GATE, root)


# -- SETUP -------------------------------------------------------------------------------


def _claude() -> str | None:
    return shutil.which("claude")


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, check=False,
                          timeout=CLAUDE_TIMEOUT_S, stdin=subprocess.DEVNULL)


def _json_list(claude: str, *args: str) -> list:
    done = _run([claude, "plugin", *args, "--json"])
    if done.returncode != 0:
        raise SetupError(f"`claude plugin {' '.join(args)} --json` failed "
                         f"(exit {done.returncode}): {done.stderr.strip()}")
    try:
        data = json.loads(done.stdout)
    except ValueError:
        raise SetupError(f"`claude plugin {' '.join(args)} --json` printed no JSON") \
            from None
    return data if isinstance(data, list) else []


def install_plugins(root: Path | None = None) -> list[str]:
    """SETUP's second step. Returns one line per action; raises `SetupError`."""
    root = claude_root() if root is None else root
    claude = _claude()
    if claude is None:
        raise SetupError("`claude` is not on PATH; install Claude Code, then run "
                         "`moxi install turns` again")
    lines = []
    markets = {m.get("name"): m for m in _json_list(claude, "marketplace", "list")
               if isinstance(m, dict)}
    have = markets.get(MARKETPLACE)
    folder = str(root.resolve())
    if have is None:
        done = _run([claude, "plugin", "marketplace", "add", folder])
        if done.returncode != 0:
            raise SetupError(f"`claude plugin marketplace add {folder}` failed "
                             f"(exit {done.returncode}): {done.stderr.strip()}")
        lines.append(f"added the marketplace {MARKETPLACE} from {folder}")
    else:
        where = have.get("path") or have.get("installLocation")
        if where and Path(where).resolve() != Path(folder):
            raise SetupError(
                f"a marketplace named {MARKETPLACE} is already added from {where}, not "
                f"{folder}; remove it with `claude plugin marketplace remove "
                f"{MARKETPLACE}` and run `moxi install turns` again")
        lines.append(f"the marketplace {MARKETPLACE} is already added")
    installed = {p.get("id") for p in _json_list(claude, "list")
                 if isinstance(p, dict) and p.get("scope") == "user"}
    for plugin in (RUNNER, GATE):
        ident = f"{plugin}@{MARKETPLACE}"
        if ident in installed:
            lines.append(f"{ident} is already installed")
            continue
        done = _run([claude, "plugin", "install", ident])
        if done.returncode != 0:
            raise SetupError(f"`claude plugin install {ident}` failed "
                             f"(exit {done.returncode}): {done.stderr.strip()}")
        lines.append(f"installed {ident}")
    return lines


def setup() -> int:
    """turns' `SETUP` (§4.6, §5.10): write `endpoint.json`, then install both plugins.
    Safe to repeat. Prints what it did; returns 0, or 1 when a step failed."""
    try:
        path = write_endpoint_file()
        print(f"turns: wrote {path}")
    except SetupError as exc:
        print(f"turns: cannot write endpoint.json: {exc}", file=sys.stderr)
        return 1
    try:
        for line in install_plugins():
            print(f"turns: {line}")
    except (SetupError, OSError, subprocess.TimeoutExpired) as exc:
        print(f"turns: the Claude plugin is not installed at user scope: {exc}. "
              "A launcher can still load it per launch with --plugin-dir.",
              file=sys.stderr)
        return 1
    return 0


# -- doctor rows -------------------------------------------------------------------------


def rows() -> list:
    """`check()`'s Claude rows (§5.10): each plugin's presence and version, whether
    `endpoint.json` names the command the config and the environment give now, and the
    Claude Code version against the ones the probes passed."""
    from .doctor import Row

    root = claude_root()
    out = []
    for plugin in (RUNNER, GATE):
        folder = plugin_dir(plugin, root)
        try:
            manifest = json.loads((folder / ".claude-plugin" / "plugin.json")
                                  .read_text("utf-8"))
            out.append(Row(f"claude plugin {plugin}", True,
                           f"version {manifest.get('version', '?')} at {folder}"))
        except (OSError, ValueError) as exc:
            out.append(Row(f"claude plugin {plugin}", False,
                           f"missing from the package at {folder}: {exc}"))
    written = read_endpoint_file(root)
    try:
        want = str(endpoint_command())
    except SetupError as exc:
        want = None
        out.append(Row("claude endpoint.json", False, f"no endpoint command: {exc}"))
    if want is not None:
        if written is None:
            out.append(Row("claude endpoint.json", False,
                           "missing: the plugin spawns no endpoint; run `moxi install "
                           "turns` (or a launcher's claude_plugin_dirs())"))
        elif written.get("command") != want:
            out.append(Row("claude endpoint.json", False,
                           f"names {written.get('command')!r}, but the config and this "
                           f"environment give {want!r}; run `moxi install turns`"))
        else:
            out.append(Row("claude endpoint.json", True, f"names {want}"))
    out.append(_version_row(Row))
    return out


def _version_row(Row):
    claude = _claude()
    if claude is None:
        return Row("claude code", False, "`claude` is not on PATH")
    try:
        done = _run([claude, "--version"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Row("claude code", False, f"`claude --version` failed: {exc}")
    version = (done.stdout.split() or ["?"])[0]
    if version in PASSED_VERSIONS:
        return Row("claude code", True, f"{version}, which the probes passed")
    return Row("claude code", False,
               f"{version}, which the probes have not seen (passed: "
               f"{', '.join(PASSED_VERSIONS)})")


__all__ = ["MARKETPLACE", "RUNNER", "GATE", "ENDPOINT_FILE", "ENDPOINT_SCRIPT",
           "PASSED_VERSIONS", "SetupError", "claude_root", "plugin_dir", "endpoint_command",
           "write_endpoint_file", "read_endpoint_file", "claude_plugin_dirs",
           "install_plugins", "setup", "rows"]
