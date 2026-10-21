"""Shared fixtures: a short runtime root (AF_UNIX paths stop at 108 bytes), a config home
of the test's own, and an endpoint driven by the fake harness."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fake_harness import FakeHarness
from moxi_turns import listeners
from moxi_turns.endpoint import Endpoint


@pytest.fixture
def turns_env(monkeypatch, tmp_path_factory):
    root = tmp_path_factory.mktemp("mt")
    (root / "cfg").mkdir()
    monkeypatch.setenv("MOXI_TURNS_RUNTIME_DIR", str(root / "rt"))
    monkeypatch.setenv("MOXI_TURNS_CONFIG_HOME", str(root / "cfg"))
    monkeypatch.delenv("MOXI_TURNS_COMMANDS", raising=False)
    monkeypatch.delenv("MOXI_TURNS_HOME", raising=False)
    yield root


def write_config(root: Path, text: str) -> None:
    (root / "cfg" / "turns").mkdir(parents=True, exist_ok=True)
    (root / "cfg" / "turns" / "config.toml").write_text(text)


@pytest.fixture
def make_endpoint(turns_env):
    made = []

    def make(harness=None, *, registry=None, session_id="s-1", env=None):
        harness = harness or FakeHarness()
        ep = Endpoint(harness, registry=registry or listeners.Registry(),
                      session_id=session_id, env=env)
        made.append(ep.open())
        return ep, harness

    yield make
    for ep in made:
        ep.stop("the test ended")


@pytest.fixture(autouse=True)
def claude_root(monkeypatch, tmp_path_factory):
    """Every test sees a folder marketplace of its own, never the package's: a test run in
    an environment the fleet loads its plugin from must not rewrite that environment's
    `endpoint.json` (design-records §16.1, M5)."""
    from moxi_turns import claude_setup

    root = tmp_path_factory.mktemp("claude")
    (root / ".claude-plugin").mkdir()
    (root / ".claude-plugin" / "marketplace.json").write_text(json.dumps({
        "name": "moxi-turns",
        "plugins": [{"name": "moxi-turns", "source": "./moxi-turns"},
                    {"name": "moxi-turns-gate", "source": "./moxi-turns-gate"}]}))
    for name in ("moxi-turns", "moxi-turns-gate"):
        (root / name / ".claude-plugin").mkdir(parents=True)
        (root / name / ".claude-plugin" / "plugin.json").write_text(
            json.dumps({"name": name, "version": "0.1.0"}))
    monkeypatch.setattr(claude_setup, "claude_root", lambda: root)
    return root
