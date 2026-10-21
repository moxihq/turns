"""turns' SETUP in its two steps, `claude_plugin_dirs()`, and the Claude doctor rows
(§5.10). `claude` is a stand-in on PATH; no test runs the real one, and none writes the
package's own `endpoint.json` (conftest's `claude_root`, design-records §16.1 M5)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

import moxi_turns as turns
from conftest import write_config
from moxi import paths
from moxi_turns import claude_setup

FAKE_CLAUDE = """#!/bin/sh
echo "$*" >> "$FAKE_CLAUDE_DIR/log"
case "$*" in
  "--version") echo "$FAKE_CLAUDE_VERSION (Claude Code)" ;;
  "plugin marketplace list --json") cat "$FAKE_CLAUDE_DIR/markets.json" ;;
  "plugin list --json") cat "$FAKE_CLAUDE_DIR/plugins.json" ;;
esac
exit 0
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(FAKE_CLAUDE)
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    state = tmp_path / "state"
    state.mkdir()
    (state / "markets.json").write_text("[]")
    (state / "plugins.json").write_text("[]")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_DIR", str(state))
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "2.1.296")
    return state


def _log(state: Path) -> list[str]:
    try:
        return (state / "log").read_text().splitlines()
    except OSError:
        return []


def test_no_test_sees_the_package_folder(claude_root):
    package = Path(claude_setup.__file__).resolve().parent / "claude"
    assert claude_setup.claude_root() == claude_root != package


def test_endpoint_json_names_this_environment_script(turns_env, claude_root):
    path = claude_setup.write_endpoint_file()
    assert path == claude_root / "moxi-turns" / "endpoint.json"
    data = json.loads(path.read_text())
    assert data["command"] == str(paths.script("moxi-turns-endpoint"))
    assert Path(data["command"]).is_absolute()


def test_the_config_key_wins_and_must_be_absolute(turns_env, claude_root):
    write_config(turns_env, '[claude]\nendpoint = "/opt/elsewhere/moxi-turns-endpoint"\n')
    claude_setup.write_endpoint_file()
    assert claude_setup.read_endpoint_file()["command"] == "/opt/elsewhere/moxi-turns-endpoint"
    write_config(turns_env, '[claude]\nendpoint = "relative/endpoint"\n')
    with pytest.raises(claude_setup.SetupError):
        claude_setup.write_endpoint_file()


def test_claude_plugin_dirs_writes_endpoint_json_first(turns_env, claude_root):
    runner, gate = turns.claude_plugin_dirs()
    assert (runner, gate) == ((claude_root / "moxi-turns").resolve(),
                              (claude_root / "moxi-turns-gate").resolve())
    assert (runner / "endpoint.json").is_file()


def test_claude_plugin_dirs_raises_when_it_cannot_set_up(turns_env, claude_root):
    import shutil

    shutil.rmtree(claude_root / "moxi-turns")
    with pytest.raises(turns.TurnsError):
        turns.claude_plugin_dirs()


def test_setup_adds_the_marketplace_and_both_plugins_then_skips(turns_env, claude_root,
                                                                 fake_claude, capsys):
    assert claude_setup.setup() == 0
    log = _log(fake_claude)
    assert f"plugin marketplace add {claude_root.resolve()}" in log
    assert "plugin install moxi-turns@moxi-turns" in log
    assert "plugin install moxi-turns-gate@moxi-turns" in log
    # Done once: the second run skips each step but still rewrites endpoint.json.
    (fake_claude / "markets.json").write_text(json.dumps(
        [{"name": "moxi-turns", "source": "directory", "path": str(claude_root)}]))
    (fake_claude / "plugins.json").write_text(json.dumps(
        [{"id": "moxi-turns@moxi-turns", "scope": "user"},
         {"id": "moxi-turns-gate@moxi-turns", "scope": "user"}]))
    (fake_claude / "log").unlink()
    (claude_root / "moxi-turns" / "endpoint.json").unlink()
    assert claude_setup.setup() == 0
    assert not [line for line in _log(fake_claude) if "add" in line or "install" in line]
    assert (claude_root / "moxi-turns" / "endpoint.json").is_file()


def test_setup_refuses_a_marketplace_of_the_same_name_elsewhere(turns_env, claude_root,
                                                                fake_claude, capsys):
    (fake_claude / "markets.json").write_text(json.dumps(
        [{"name": "moxi-turns", "source": "directory", "path": "/somewhere/else"}]))
    assert claude_setup.setup() == 1
    assert "/somewhere/else" in capsys.readouterr().err
    assert (claude_root / "moxi-turns" / "endpoint.json").is_file()   # step one stands


def test_setup_without_claude_still_writes_endpoint_json(turns_env, claude_root,
                                                         monkeypatch, capsys):
    monkeypatch.setattr(claude_setup, "_claude", lambda: None)
    assert claude_setup.setup() == 1
    assert (claude_root / "moxi-turns" / "endpoint.json").is_file()
    assert "--plugin-dir" in capsys.readouterr().err


def _rows():
    return {row.name: row for row in claude_setup.rows()}


def test_rows_name_the_plugins_endpoint_json_and_the_version(turns_env, claude_root,
                                                             fake_claude):
    rows = _rows()
    assert rows["claude plugin moxi-turns"].ok and rows["claude plugin moxi-turns-gate"].ok
    assert not rows["claude endpoint.json"].ok and "missing" in rows["claude endpoint.json"].detail
    assert rows["claude code"].ok
    claude_setup.write_endpoint_file()
    assert _rows()["claude endpoint.json"].ok
    write_config(turns_env, '[claude]\nendpoint = "/opt/moved/moxi-turns-endpoint"\n')
    assert not _rows()["claude endpoint.json"].ok      # names a command no longer given
    os.environ["FAKE_CLAUDE_VERSION"] = "9.9.9"
    row = _rows()["claude code"]
    assert not row.ok and "9.9.9" in row.detail


def test_check_carries_the_claude_rows(turns_env, fake_claude):
    names = [row.name for row in turns.check()]
    assert "claude endpoint.json" in names and "claude code" in names
