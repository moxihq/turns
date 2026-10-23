"""The module contract check over this repo, as `moxi check` runs it at each commit.

A contributor's clone and a pull request run the same check as the commit hook.
"""

from pathlib import Path

import pytest

from moxi.contract import check

ROOT = Path(__file__).resolve().parents[1]


def test_the_repo_keeps_the_module_contract():
    assert [str(finding) for finding in check(ROOT)] == []


def test_every_public_name_loads_on_first_use():
    """The package loads its names lazily; each one in `__all__` resolves to
    the object its submodule defines (a host's namespace, named as its submodule, is
    the submodule), and an unknown name is an AttributeError."""
    import importlib

    import moxi_turns

    for name in moxi_turns.__all__:
        where = moxi_turns._WHERE[name]
        module = importlib.import_module(f"moxi_turns.{where}")
        want = module if where == name else getattr(module, name)
        assert getattr(moxi_turns, name) is want, name
    assert set(moxi_turns._WHERE) == set(moxi_turns.__all__)
    with pytest.raises(AttributeError):
        moxi_turns.nonesuch
