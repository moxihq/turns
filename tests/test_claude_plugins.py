"""The Claude plugins inside the package: their layout as a
folder marketplace, `claude plugin validate` on each, and their own tests under
`claude plugin test`. The last two need Claude Code on PATH and are skipped without it."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

import moxi_turns

CLAUDE_DIR = Path(moxi_turns.__file__).parent / "claude"
PLUGINS = ("moxi-turns", "moxi-turns-gate")

needs_claude = pytest.mark.skipif(shutil.which("claude") is None, reason="Claude Code is not on PATH")


def test_the_folder_marketplace_lists_both_plugins_by_relative_path():
    doc = json.loads((CLAUDE_DIR / ".claude-plugin" / "marketplace.json").read_text())
    assert doc["name"] == "moxi-turns"
    assert [(p["name"], p["source"]) for p in doc["plugins"]] == [
        ("moxi-turns", "./moxi-turns"), ("moxi-turns-gate", "./moxi-turns-gate")]
    for name in PLUGINS:
        manifest = json.loads((CLAUDE_DIR / name / ".claude-plugin" / "plugin.json").read_text())
        assert manifest["name"] == name
        hooks = json.loads((CLAUDE_DIR / name / "hooks" / "hooks.json").read_text())
        assert hooks == {"modules": ["./register.ts"]}


def test_the_runner_reports_the_version_its_manifest_names():
    manifest = json.loads((CLAUDE_DIR / "moxi-turns" / ".claude-plugin" / "plugin.json").read_text())
    source = (CLAUDE_DIR / "moxi-turns" / "hooks" / "register.ts").read_text()
    assert f"const MOD_VERSION = '{manifest['version']}'" in source


def test_endpoint_json_is_never_tracked():
    ignore = (Path(__file__).parents[1] / ".gitignore").read_text().splitlines()
    assert "src/moxi_turns/claude/moxi-turns/endpoint.json" in ignore


def _claude(*args: str, timeout: int = 240) -> subprocess.CompletedProcess:
    return subprocess.run(["claude", "plugin", *args], capture_output=True, text=True,
                          timeout=timeout, stdin=subprocess.DEVNULL)


@needs_claude
@pytest.mark.parametrize("folder", ["", *PLUGINS])
def test_claude_plugin_validate_passes(folder):
    out = _claude("validate", str(CLAUDE_DIR / folder))
    assert out.returncode == 0, out.stdout + out.stderr
    assert "Validation passed" in out.stdout


@needs_claude
@pytest.mark.parametrize("name", PLUGINS)
def test_claude_plugin_test_passes(name):
    out = _claude("test", str(CLAUDE_DIR / name), timeout=420)
    assert out.returncode == 0, (out.stdout + out.stderr)[-4000:]
    assert " 0 fail" in out.stdout + out.stderr
