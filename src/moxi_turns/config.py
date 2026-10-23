"""turns' settings, read through `moxi.config` from `CONFIG/turns/config.toml`.

The schema is `settings.toml` in this package (the announcement's `SETTINGS`). One more
input sits outside the file: `MOXI_TURNS_COMMANDS`, a launcher's grant of session commands
to one launch, read from the environment the endpoint inherits from its session.
"""

from __future__ import annotations

from collections.abc import Mapping

from moxi import config as moxi_config

from .results import COMMANDS

MODULE = "turns"
#: A launcher stamps `clear,exit` (or one of them) into one launch's environment.
COMMANDS_ENV = "MOXI_TURNS_COMMANDS"


def schema() -> moxi_config.Schema:
    return moxi_config.Schema.from_resource(__package__, "settings.toml")


def reader(env: Mapping[str, str] | None = None) -> moxi_config.Config:
    """A started-on-demand `moxi.config.Config` for turns."""
    return moxi_config.Config(MODULE, schema(), env=env)


def allowed(settings_allow: object, env: Mapping[str, str]) -> frozenset[str]:
    """The session commands consented to: the config's `commands.allow` and the launch's
    stamp together. Names that are not session commands are dropped."""
    names: set[str] = set()
    if isinstance(settings_allow, (list, tuple)):
        names.update(n for n in settings_allow if isinstance(n, str))
    stamp = env.get(COMMANDS_ENV, "")
    names.update(part.strip() for part in stamp.split(","))
    return frozenset(n for n in names if n in COMMANDS)


__all__ = ["MODULE", "COMMANDS_ENV", "schema", "reader", "allowed"]
