"""Nine public names in three modules, with the signatures the package docstring states.

A kit's surface is a promise about what a consumer may call and what it may pass. A
signature that drifts by one default breaks a consumer that never changed.
"""

from __future__ import annotations

import inspect

from moxi_turns import hooks

P = "POSITIONAL_OR_KEYWORD"
KW = "KEYWORD_ONLY"
VAR = "VAR_POSITIONAL"


def _params(fn):
    return [
        (p.name, p.kind.name, p.default if p.default is not p.empty else ...)
        for p in inspect.signature(fn).parameters.values()
    ]


def test_the_surface_is_nine_names_in_three_modules():
    assert hooks.wire.__all__ == ["envelope", "field", "plain", "read_event"]
    assert hooks.text.__all__ == ["clamp"]
    assert hooks.run.__all__ == ["emit", "guard", "remaining", "run"]
    names = (hooks.wire.__all__ + hooks.text.__all__ + hooks.run.__all__)
    assert len(names) == 9
    assert len(set(names)) == 9


def _public(module):
    """Public names a consumer could reach. `annotations` is the `__future__` flag every
    module carries so its annotations stay strings (L3) — a compiler switch, not a name
    anybody calls."""
    return {
        k for k, v in vars(module).items()
        if not k.startswith("_") and k != "annotations" and not inspect.ismodule(v)
    }


def test_the_package_exposes_the_three_modules_and_nothing_else():
    assert hooks.__all__ == ["run", "text", "wire"]
    assert _public(hooks) <= {"run", "text", "wire"}


def test_no_module_carries_a_public_name_the_surface_does_not_name():
    """Every name a consumer can reach in a kit module is one of the nine. `run` holds
    `envelope`, `read_event` and `clamp` because it composes them; nothing else is
    reachable anywhere, so the package surface and its stated list cannot drift."""
    nine = set(hooks.wire.__all__) | set(hooks.text.__all__) | set(
        hooks.run.__all__)
    for module in (hooks.wire, hooks.text, hooks.run):
        public = _public(module)
        assert set(module.__all__) <= public, f"{module.__name__} lost a name"
        assert public <= nine, f"{module.__name__}: {public - nine}"


def test_the_signatures_are_the_stated_signatures():
    assert _params(hooks.wire.read_event) == [
        ("stream", P, None),
        ("on_error", KW, None),
    ]
    assert _params(hooks.wire.field) == [("data", P, ...), ("keys", VAR, ...)]
    assert _params(hooks.wire.envelope) == [("event", P, ...), ("text", P, ...)]
    assert _params(hooks.wire.plain) == [("event", P, ...), ("text", P, ...)]

    # `marker` is keyword-only and has no default: the kit words no admission itself.
    assert _params(hooks.text.clamp) == [
        ("text", P, ...),
        ("cap", P, ...),
        ("marker", KW, ...),
    ]

    assert _params(hooks.run.guard) == [
        ("body", P, ...),
        ("args", VAR, ...),
        ("on_error", KW, None),
        ("seconds", KW, None),
        ("default", KW, None),
        ("where", KW, "body"),
    ]
    assert _params(hooks.run.emit) == [("line", P, ...), ("out", P, None)]
    assert _params(hooks.run.remaining) == []
    assert _params(hooks.run.run) == [
        ("event", P, ...),
        ("body", P, ...),
        ("stream", KW, None),
        ("out", KW, None),
        ("cap", KW, None),
        ("marker", KW, None),
        ("seconds", KW, None),
        ("render", KW, hooks.wire.envelope),
        ("on_error", KW, None),
        ("on_emitted", KW, None),
    ]


def test_the_where_vocabulary_is_the_six_tokens():
    """`on_error(exc, *, where)` has one signature throughout and `where` is one of six
    short mechanical tokens."""
    assert hooks.wire._WHERE == (
        "read", "body", "render", "emit", "deadline", "emitted",
    )


def test_the_docstring_states_the_two_shapes_and_the_setitimer_limitation():
    """The package docstring says out loud that the kit never swallows a
    failure silently, never selects an exit code, and that `setitimer` binds the main
    thread on POSIX only — rather than implying a guarantee it does not hold."""
    doc = " ".join(hooks.__doc__.split())
    assert "never swallows a failure silently" in doc
    assert "never selects a process exit code" in doc
    assert "signal.setitimer" in doc
    assert "binds the main thread on POSIX only" in doc
