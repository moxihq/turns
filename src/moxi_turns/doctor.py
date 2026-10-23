"""`turns.check()`: rows for a doctor. turns has no command of its own; comms'
doctor calls this to answer "can comms reach this session?".

The rows this release gives: turns' config, the registered listeners, and the live
endpoints (dead ones are swept on the way). Each harness adapter adds its own rows by
naming a `rows()` function in `ADAPTER_ROWS` (the Claude adapter's: each plugin's presence
and version, `endpoint.json`, and the Claude Code versions the probes passed; the
OpenCode host's: its binary).
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass

from moxi.config import ConfigError

from . import client, config, listeners

#: `"package.module:function"` of each adapter's rows, in the order they are shown.
ADAPTER_ROWS: tuple[str, ...] = (
    "moxi_turns.claude_setup:rows", "moxi_turns.codex:rows", "moxi_turns.opencode:rows",
)


@dataclass(frozen=True)
class Row:
    """One line of a doctor: what was checked, whether it is fine, and what was found."""

    name: str
    ok: bool
    detail: str


def check() -> list[Row]:
    rows = [_config_row()]
    rows.extend(_listener_rows())
    rows.extend(_endpoint_rows())
    for target in ADAPTER_ROWS:
        module_name, _, attr = target.partition(":")
        try:
            rows.extend(getattr(importlib.import_module(module_name), attr)())
        except Exception as exc:  # noqa: BLE001 - a doctor reports, it does not raise
            rows.append(Row(target, False, f"could not be checked: "
                                           f"{type(exc).__name__}: {exc}"))
    return rows


def _config_row() -> Row:
    reader = config.reader()
    try:
        settings = reader.start()
    except ConfigError as exc:
        return Row("config", False, str(exc))
    allow = ", ".join(settings["commands.allow"]) or "none"
    return Row("config", True, f"{settings.file}: hold.wait_ms {settings['hold.wait_ms']}, "
                               f"listen.wait_ms {settings['listen.wait_ms']}, "
                               f"commands.allow {allow}")


def _listener_rows() -> list[Row]:
    registry = listeners.load()
    modules: dict[str, set[str]] = {}
    for listener in registry.listeners:
        modules.setdefault(listener.module, set()).update(listener.events)
    rows = [Row(f"listener {name}", True, "registered for " + ", ".join(
        e for e in listeners.EVENTS if e in events)) for name, events in sorted(modules.items())]
    rows.extend(Row(f"listener {b.module}", False, b.reason) for b in registry.broken)
    return rows


def _endpoint_rows() -> list[Row]:
    live = client.sessions()
    rows = [Row("endpoints", True, f"{len(live)} live under {client.runtime_dir()}")]
    for s in live:
        hello = s.hello
        mod = f", mod {hello['mod_version']}" if hello.get("mod_version") else ""
        trouble = hello.get("trouble")
        detail = (f"session {s.session_id or '(not yet known)'}; can "
                  f"{', '.join(sorted(s.can)) or 'nothing'}; endpoint "
                  f"{hello.get('endpoint_version')}{mod}")
        if trouble:
            detail += f"; {trouble}"
        rows.append(Row(f"endpoint {s.harness} pid {s.pid}", not bool(trouble), detail))
    return rows


__all__ = ["ADAPTER_ROWS", "Row", "check"]
