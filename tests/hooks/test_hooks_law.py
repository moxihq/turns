"""The ten laws of `moxi_turns.hooks`, one live gauge each, named as the package names them.

A law with no gauge is a sentence in a docstring. These are the sentences made into
tests, so the day the package grows by drift the thing it says it never grows, the suite
says so rather than a reader noticing.
"""

from __future__ import annotations

import ast
import inspect
import io
import json
import pathlib
import signal
import time
import tomllib

import pytest

import moxi_turns.module
from moxi_turns import hooks
from hooks_support import Collector, Recorder, drive

SRC = pathlib.Path(hooks.__file__).parent
ROOT = SRC.parents[2]
# `rglob`, not `glob`: a future subpackage is not a blind spot in any of the source pins
# below.
MODULES = sorted(p for p in SRC.rglob("*.py") if p.name != "__init__.py")
ALL = MODULES + sorted(SRC.rglob("__init__.py"))

#: The package's own import name, and the stdlib names L3 allows.
PACKAGE = "moxi_turns.hooks"
STDLIB = {"__future__", "json", "signal", "sys"}

#: The longest string the kit is allowed to author. The longest one it has is the wire's
#: own key, `hookSpecificOutput` (18), a mechanical token by anybody's reading. The
#: threshold sits two characters above that: below a default cut marker, below anything
#: that reads as a phrase, and tight enough that the next string to exceed it has to be
#: looked at. The allowlist below is the real pin; this is the cheap half.
TOKEN = 20


def _code_literals(path):
    """Every literal the module EXECUTES, docstrings excluded, because a rule about the
    words a kit owns is a rule about code and not about its reasoning."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(
                body[0].value, ast.Constant
            ):
                docstrings.add(id(body[0].value))
    return [
        (node, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and id(node) not in docstrings
    ]


# --- L1 — Stateless -----------------------------------------------------------------

def test_l1_the_suite_needs_no_environment():
    """Gauge: the suite needs no environment. Driven as the harness drives it, in a
    process handed nothing at all (no HOME, no PATH, no PYTHONPATH), because every fact
    the kit uses crossed the boundary as an argument."""
    code, out, err = drive(
        """
        import sys
        from moxi_turns.hooks.run import run
        raise SystemExit(run("SessionStart", lambda d: d.get("cwd")))
        """,
        json.dumps({"cwd": "/somewhere"}),
        env={},
    )
    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["additionalContext"] == "/somewhere"
    assert err == ""


# --- L2 — Env-free ------------------------------------------------------------------

def test_l2_no_module_reads_the_environment():
    """Gauge: `os.environ` and `getenv` appear nowhere in the package. A caller's own
    override variables, and the roots `moxi.paths` locates, stay in the caller."""
    for path in ALL:
        text = path.read_text(encoding="utf-8")
        for banned in ("os.environ", "getenv", "expanduser", "Path.home"):
            assert banned not in text, f"{path.name} reads the environment: {banned}"


# --- L3 — Standard library only, and nothing else of Moxi's -------------------------

#: What importing the package adds to a process. The control imports `json`, `signal`
#: and `__future__` in the same process: `json` drags its decoder, encoder, scanner and
#: `re` in with it. What is left is what the package added, and that includes turns' own
#: top package, which every import of the kit pays first. That parent must load empty
#: (its names load on first use), so the closure is the parent and the kit's own modules
#: and nothing else: no `moxi`, no other part of turns, no `socket` or `threading`.
_CLOSURE = """
import sys
base = set(sys.modules)
import __future__, json, signal
control = set(sys.modules) - base
mark = set(sys.modules)
%s
print(sorted((set(sys.modules) - mark) - control))
"""

OWN_MODULES = [
    "moxi_turns.hooks",
    "moxi_turns.hooks.run",
    "moxi_turns.hooks.text",
    "moxi_turns.hooks.wire",
]

#: What the closure may add: the kit's modules and their parent, loaded empty.
CLOSURE = ["moxi_turns", *OWN_MODULES]


def test_l3_the_import_closure_is_the_three_names_and_the_kit():
    """Gauge: import the whole kit in a clean subprocess; `sys.modules` gained nothing
    outside the L3 list and the kit's own modules."""
    code, out, err = drive(
        _CLOSURE % "import moxi_turns.hooks, moxi_turns.hooks.wire, "
                   "moxi_turns.hooks.text, moxi_turns.hooks.run"
    )
    assert code == 0, err
    assert json.loads(out.decode().replace("'", '"')) == CLOSURE


def test_l3_the_import_statements_are_json_sys_and_signal():
    """The closure test proves nothing heavy loaded. This proves the source says so; the
    two differ the day a module imports something the interpreter already had.

    The kit reaches its own modules by relative import, one level and no further, so
    the rest of turns stays out of reach as surely as the standard library's others."""
    for path in ALL:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in STDLIB, f"{path.name} imports {alias.name}"
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    assert node.level == 1 and node.module in {"run", "text", "wire"}, (
                        f"{path.name}:{node.lineno} reaches outside the package")
                else:
                    root = node.module.split(".")[0]
                    assert root in STDLIB, f"{path.name} imports {node.module}"


def test_l3_nothing_else_of_moxis_is_imported_or_shelled_to():
    """Nothing on a hook's path depends on another part of Moxi: the kit imports neither
    the core nor any module, the rest of turns included, and it reaches its own
    submodules only by the name of the package itself. Nor does it open a socket, spawn
    a subprocess or read a file: a caller's transport stays in the caller."""
    for path in ALL:
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names = [node.module]
            for name in names:
                top = name.split(".")[0]
                assert top != "moxi" and not top.startswith("moxi_"), (
                    f"{path.name} imports {name}")
        for banned in ("subprocess", "socket", "open(", "Path(", "pathlib",
                       "importlib"):
            assert banned not in text, f"{path.name} grew a transport: {banned}"


def test_l3_typing_is_never_imported_at_runtime():
    """Every module carries `from __future__ import annotations`, so the `TextIO`
    annotations are strings and `typing` never loads."""
    for path in ALL:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        futures = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "__future__"
            for alias in node.names
        }
        assert "annotations" in futures, f"{path.name} lacks the future import"


def test_l3_the_package_is_no_ones_command_and_no_ones_api():
    """The kit shares turns' distribution, so the build file's half of L3 is what that
    distribution promises about it: no console script runs a module of the kit itself
    (a library has no CLI, and this one selects no exit code), and the announcement's
    `API` is not the kit, so no other module may import it."""
    parsed = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    for group in ("scripts", "gui-scripts"):
        for name, target in (parsed["project"].get(group) or {}).items():
            assert not target.split(":")[0].startswith(PACKAGE), (
                f"console script {name} runs the kit itself")
    api = moxi_turns.module.API
    assert api is None or not api.startswith(PACKAGE)


# --- L4 — Imports nothing eagerly ---------------------------------------------------

def test_l4_importing_the_package_binds_no_submodule():
    """Gauge one: `import moxi_turns.hooks` binds no submodule. The saving today is nil
    and the law says so; this is the tripwire that makes a future eager import a visible
    change to `__init__.py` rather than a line added to a module somebody already
    imports."""
    code, out, err = drive(
        """
        import sys, moxi_turns.hooks as hooks
        print(sorted(n for n in sys.modules if n.startswith("moxi_turns.hooks")))
        print(sorted(k for k in vars(hooks) if k in ("run", "text", "wire")))
        hooks.wire, hooks.text, hooks.run
        print(sorted(n for n in sys.modules if n.startswith("moxi_turns.hooks")))
        """
    )
    assert code == 0, err
    bound, public, after = out.decode().splitlines()
    assert bound == "['moxi_turns.hooks']"
    assert public == "[]"
    assert after == repr(OWN_MODULES)


def test_l4_the_closure_of_run_is_the_l3_list_and_own_modules():
    """Gauge two: what importing `moxi_turns.hooks.run` adds is the L3 list and the kit's
    own modules, nothing else and nothing heavy, because the kit IS a hook's path."""
    code, out, err = drive(_CLOSURE % "import moxi_turns.hooks.run")
    assert code == 0, err
    assert json.loads(out.decode().replace("'", '"')) == CLOSURE


def test_l3_importing_the_kit_loads_no_moxi_and_no_socket():
    """b5-1 [L2]: the kit shares turns' package, whose `__init__` is the public API. A
    hook process that imports `moxi_turns.hooks` must not load the core or the endpoint's
    client with it: the parent loads empty and its names load on first use."""
    code, out, err = drive(
        """
        import sys
        import moxi_turns.hooks.run
        print(sorted(n for n in sys.modules
                     if n in ("socket", "threading", "moxi") or n.startswith("moxi.")
                     or (n.startswith("moxi_turns") and not n.startswith("moxi_turns.hooks")
                         and n != "moxi_turns")))
        import moxi_turns
        print(sorted(k for k in vars(moxi_turns) if k in moxi_turns.__all__))
        """
    )
    assert code == 0, err
    loaded, bound = out.decode().splitlines()
    assert loaded == "[]"
    assert bound == "[]"


def test_l4_an_unknown_attribute_is_still_an_attribute_error():
    with pytest.raises(AttributeError):
        hooks.nonesuch


# --- L5 — A hard per-hook time bound ------------------------------------------------

def test_l5_a_body_past_the_bound_returns_default_emits_nothing_and_reports_once():
    """Gauge: a body that sleeps past the bound returns `default`, emits nothing, and
    reports once."""
    seen = Collector()
    out = Recorder()

    def slow(data):
        time.sleep(5.0)
        return "this must never be written"

    assert hooks.run.run("SessionStart", slow, stream=io.StringIO("{}"), out=out,
                         seconds=0.05, on_error=seen) == 0
    assert out.writes == []
    assert seen.wheres == ["deadline"]


def test_l5_guard_returns_the_default_when_the_wall_fires():
    seen = Collector()
    sentinel = object()
    got = hooks.run.guard(time.sleep, 5.0, seconds=0.05, default=sentinel,
                          on_error=seen)
    assert got is sentinel
    assert seen.wheres == ["deadline"]


def test_l5_the_bound_is_off_unless_seconds_is_passed():
    """No deadline is armed without `seconds`, and `remaining()` says so."""
    assert hooks.run.remaining() is None
    inside = []
    hooks.run.guard(lambda: inside.append(hooks.run.remaining()))
    assert inside == [None]


def test_l5_off_the_main_thread_it_installs_nothing_and_says_so():
    """The stated limitation, gauged rather than trusted: `signal.setitimer` binds the
    main thread on POSIX only, so off it the kit installs no deadline and reports that
    through `on_error` instead of implying a bound it does not hold."""
    import threading

    seen = Collector()
    done = []

    def worker():
        done.append(hooks.run.guard(lambda: "ran", seconds=0.01, on_error=seen))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(10)
    assert done == ["ran"]
    assert seen.wheres == ["deadline"]
    assert isinstance(seen.calls[0][0], ValueError)


def test_l5_remaining_is_a_share_of_the_hooks_own_wall():
    """The reason the bound is worth owning: a caller derives its per-operation timeout
    from what is left rather than copying the same number into each call site."""
    seen = []
    hooks.run.guard(lambda: seen.append(hooks.run.remaining()), seconds=2.0)
    assert len(seen) == 1
    assert 0.0 < seen[0] <= 2.0
    assert hooks.run.remaining() is None


def test_l5_a_nested_guard_does_not_cancel_the_outer_wall():
    """The wall is the HOOK's, so an inner bound must hand it back. Without this, a
    caller that guards one subprocess inside the hook silently un-bounds the rest."""
    seen = []

    def outer():
        hooks.run.guard(lambda: None, seconds=0.01)
        time.sleep(0.05)
        seen.append(hooks.run.remaining())

    collector = Collector()
    hooks.run.guard(outer, seconds=1.0, on_error=collector)
    assert seen and 0.0 < seen[0] < 1.0
    assert collector.wheres == []


# The rule every gauge below turns on, stated once: a wall is absorbed ONLY by the guard
# that armed it. Everything else re-raises it untouched. The six L5 gauges above can all
# pass while the law is broken, because every one of them puts the sleep directly in the
# body, the one step of six where absorbing the wall happens to coincide with L5's
# required outcome.

def test_l5_a_wall_in_the_read_abandons_the_hook_with_nothing_on_stdout():
    """The wall fires while `read_event` is blocked on a slow stream. A read guard that
    ate it would return `{}`, and the hook would walk on and PRINT the payload, with the
    timer already spent, so everything after the read would run with no bound at all."""
    class SlowStream:
        def read(self):
            time.sleep(5.0)
            return "{}"

    out = Recorder()
    seen = Collector()
    assert hooks.run.run("SessionStart",
                         lambda d: "PAYLOAD THAT MUST NEVER BE WRITTEN",
                         stream=SlowStream(), out=out, seconds=0.05,
                         on_error=seen) == 0
    assert out.writes == []
    assert seen.wheres == ["deadline"]


def test_l5_an_inner_guard_never_absorbs_a_wall_it_did_not_arm():
    """A caller's body that wraps a blocking call in `guard`, which is exactly what
    "derive your per-operation timeout from `remaining()`" invites. An inner guard that
    ate the outer wall would let the hook run 0.55 s against a 0.05 s bound and emit."""
    out = Recorder()
    seen = Collector()
    inner = Collector()
    reached = []

    def body(data):
        hooks.run.guard(time.sleep, 5.0, on_error=inner)
        reached.append("past the inner guard")
        return "emitted long after the wall"

    start = time.monotonic()
    assert hooks.run.run("SessionStart", body, stream=io.StringIO("{}"), out=out,
                         seconds=0.05, on_error=seen) == 0
    elapsed = time.monotonic() - start
    assert reached == [], "the inner guard swallowed the hook's wall"
    assert out.writes == []
    assert seen.wheres == ["deadline"]
    assert inner.calls == [], "the wall is not the inner guard's to report"
    assert elapsed < 2.0, f"the hook ran {elapsed:.2f}s against a 0.05s wall"


def _slow(*args, **kw):
    time.sleep(5.0)
    return "never"


class _SlowStream:
    def read(self):
        time.sleep(5.0)
        return "{}"


@pytest.mark.parametrize("step", ["read", "body", "cap", "render", "emitted"])
def test_l5_a_wall_in_any_step_is_reported_once_by_the_guard_that_armed_it(
        step, monkeypatch):
    """Every step of `_once` is its own guard and not one of them arms anything, so
    whichever step the wall lands in, exactly one `deadline` report comes out and the
    hook ends there with nothing written. Five steps; a kit whose step guards absorbed
    the wall would give L5's outcome in `body` alone, and by accident."""
    kwargs = dict(stream=io.StringIO("{}"), body=lambda d: "a payload")
    if step == "read":
        kwargs["stream"] = _SlowStream()
    elif step == "body":
        kwargs["body"] = _slow
    elif step == "cap":
        monkeypatch.setattr(hooks.run, "clamp", _slow)
        kwargs.update(cap=10, marker="…")
    elif step == "render":
        kwargs["render"] = _slow
    elif step == "emitted":
        kwargs["on_emitted"] = _slow

    out = Recorder()
    seen = Collector()
    body = kwargs.pop("body")
    assert hooks.run.run("SessionStart", body, out=out, seconds=0.05, on_error=seen,
                         **kwargs) == 0
    assert seen.wheres == ["deadline"], f"{step}: {seen.wheres}"
    if step == "emitted":
        # `on_emitted` runs after the flush and under the same wall, so the payload is
        # already out; the wall still ends the hook and still reports exactly once.
        assert len(out.writes) == 1
    else:
        assert out.writes == [], f"{step} wrote a payload the wall had killed"


def test_l5_a_failed_arming_leaves_the_process_handler_as_it_found_it():
    """The handler must go in before the timer, there being no other order, so a
    `setitimer` that refuses has to take it straight back out. Left behind, `_fire`
    would stay installed for the rest of the process: any later SIGALRM in the caller's
    own code would raise a `_Deadline` in an unrelated frame, and the next kit `guard`
    would capture `_fire` as "previous" and restore it forever."""
    before = signal.getsignal(signal.SIGALRM)
    seen = Collector()
    assert hooks.run.guard(lambda: "ok", seconds=-1.0, on_error=seen) == "ok"
    assert signal.getsignal(signal.SIGALRM) is before
    assert signal.getsignal(signal.SIGALRM) is not hooks.run._fire
    assert seen.wheres == ["deadline"]
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0.0


@pytest.mark.parametrize("seconds", [0, 0.0, -1, -0.5])
def test_l5_a_non_positive_bound_is_a_caller_error_and_arms_nothing(seconds):
    """`setitimer(…, 0)` DISARMS, so a bound of zero is a wall that silently is not one.
    L5 says the bound is off when `seconds` is NOT passed; passing it and getting
    nothing is the one outcome the law does not admit, so it is a caller error: reported
    through `on_error` with `where="deadline"`, no deadline armed, and `remaining()`
    answering `None` rather than `0.0`.

    This is live in the shape `remaining()` invites, where a caller computes `seconds`
    from a budget that can reach zero and then derives
    `subprocess.run(timeout=remaining())` from it: without the refusal it would get no
    wall and a zero timeout in the same breath."""
    seen = Collector()
    inside = []
    got = hooks.run.guard(lambda: inside.append(hooks.run.remaining()) or "ran",
                          seconds=seconds, on_error=seen)
    assert got == "ran", "a refused bound must not stop the body"
    assert seen.wheres == ["deadline"]
    assert isinstance(seen.calls[0][0], ValueError)
    assert inside == [None], "remaining() described a wall that is not there"
    assert hooks.run.remaining() is None
    assert signal.getsignal(signal.SIGALRM) is not hooks.run._fire


def test_l5_a_non_positive_bound_through_run_still_returns_zero():
    out = Recorder()
    seen = Collector()
    assert hooks.run.run("SessionStart", lambda d: "x", stream=io.StringIO("{}"),
                         out=out, seconds=0, on_error=seen) == 0
    assert seen.wheres == ["deadline"]
    assert out.text.endswith("\n")


def test_l5_an_inner_wall_hands_the_outer_one_back_intact():
    """The gauge is the law rather than a window a wrong answer also fits.

    Outer 1.0 s; an inner guard arms 0.1 s and its body overruns, so the INNER wall fires
    and the inner guard absorbs it (it armed it). What the inner then hands back must be
    the outer's own timer, less exactly what the inner spent (a restore that forgot to
    subtract reads ~1.0 and fails the window below), with the outer's handler still in
    place. Then the outer wall must fire at its own deadline and not later."""
    marks = []
    handlers = []

    def inner_body():
        marks.append(("inside inner", hooks.run.remaining()))
        time.sleep(5.0)
        marks.append(("never", None))

    def outer_body():
        marks.append(("outer entry", hooks.run.remaining()))
        inner = Collector()
        hooks.run.guard(inner_body, seconds=0.1, on_error=inner)
        assert inner.wheres == ["deadline"], "the inner wall is the inner guard's"
        marks.append(("after inner", hooks.run.remaining()))
        handlers.append(signal.getsignal(signal.SIGALRM))
        time.sleep(5.0)
        marks.append(("never", None))

    seen = Collector()
    start = time.monotonic()
    got = hooks.run.guard(outer_body, seconds=1.0, default="outer default",
                          on_error=seen)
    elapsed = time.monotonic() - start

    assert got == "outer default"
    assert seen.wheres == ["deadline"], "the outer wall never fired"
    assert [name for name, _ in marks] == ["outer entry", "inside inner", "after inner"]
    assert 0.9 < marks[0][1] <= 1.0                  # the outer wall, just armed
    assert 0.0 < marks[1][1] <= 0.1                  # the inner wall, from inside it
    assert 0.85 < marks[2][1] < 0.95, marks[2][1]    # 1.0 less the inner's 0.1, restored
    assert handlers[0] is hooks.run._fire, "the inner disarm took the outer's handler"
    assert 0.9 < elapsed < 2.0, f"the outer wall fired at {elapsed:.2f}s, not 1.0s"
    assert hooks.run.remaining() is None
    assert signal.getsignal(signal.SIGALRM) is not hooks.run._fire


def test_l5_only_the_arming_guard_absorbs_a_wall_is_in_the_code_and_not_only_the_prose():
    """The docstring and the source have to agree on this invariant, and this fails if
    either moves."""
    source = (SRC / "run.py").read_text(encoding="utf-8")
    assert "if not armed:" in source and "raise" in source
    doc = " ".join(hooks.wire._Deadline.__doc__.split())
    assert "only the guard that ARMED a wall may absorb it" in doc
    assert "every other frame re-raises it untouched" in doc
    tree = ast.parse(source)
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "guard")
    handlers = [h for n in ast.walk(fn) if isinstance(n, ast.Try) for h in n.handlers]
    deadline = [h for h in handlers
                if isinstance(h.type, ast.Name) and h.type.id == "_Deadline"]
    assert len(deadline) == 1, "guard must have exactly one _Deadline clause"
    assert any(isinstance(n, ast.Raise) and n.exc is None
               for n in ast.walk(deadline[0])), "the clause must re-raise a foreign wall"


# --- L6 — A size cap that admits its own cuts ---------------------------------------

@pytest.mark.parametrize("marker", ["…", "[cut]", "\n[... truncated ...]"])
@pytest.mark.parametrize("length", [0, 1, 7, 40, 500])
@pytest.mark.parametrize("cap", [1, 5, 21, 40, 499, 500, 501])
def test_l6_the_result_fits_the_cap_and_admits_every_cut(marker, length, cap):
    """Gauge: for any `text`, `cap` and `marker`, the result is at most `cap` characters
    and ends with `marker` whenever anything was dropped.

    Quantified over `cap >= len(marker)` for the second half, because below that the cap
    has less room than the admission needs and the two halves cannot both hold; that
    corner has its own test, and the length bound is the half that wins."""
    text = "x" * length
    got = hooks.text.clamp(text, cap, marker=marker)
    assert len(got) <= cap or len(text) <= cap
    if len(text) > cap:
        if cap >= len(marker):
            assert got.endswith(marker), "a cut that does not admit itself is silent"
            assert len(got) <= cap
        assert len(got) <= cap


def test_l6_the_markers_length_is_reserved_before_anything_is_spent():
    """If the marker is the thing that does not fit, the cut is silent, and a silent cut
    is the failure the cap exists to avoid."""
    got = hooks.text.clamp("abcdefghij", 8, marker="[c]")
    assert got == "abcde[c]"
    assert len(got) == 8


def test_l6_text_that_fits_is_returned_untouched():
    assert hooks.text.clamp("abc", 3, marker="…") == "abc"
    assert hooks.text.clamp("", 0, marker="…") == ""


def test_l6_a_cap_with_less_room_than_the_admission_keeps_the_bound():
    """The corner the property gauge cannot cover, resolved and stated rather than
    discovered: the length bound wins, because overrunning the cap loses the whole
    payload to the harness's replacement while a cut marker only loses the admission."""
    assert hooks.text.clamp("abcdef", 2, marker="[cut]") == "[c"
    assert hooks.text.clamp("abcdef", 0, marker="[cut]") == ""
    assert hooks.text.clamp("abcdef", -3, marker="[cut]") == ""


def test_l6_marker_is_required_and_has_no_default():
    """The kit owns no string a reader ever sees, so there is no default marker to fall
    back on and no default cap to apply."""
    with pytest.raises(TypeError):
        hooks.text.clamp("abcdef", 3)
    params = inspect.signature(hooks.text.clamp).parameters
    assert params["marker"].default is inspect.Parameter.empty
    assert params["marker"].kind is inspect.Parameter.KEYWORD_ONLY


# --- L7 — The hook path cannot raise, and the kit never selects an exit code --------

class _Bare(BaseException):
    """Not an `Exception`. The class a caller's own `except Exception` would miss."""


@pytest.mark.parametrize("exc", [ValueError("x"), SystemExit(3), _Bare("x")])
def test_l7_every_class_of_raise_leaves_run_at_zero_with_empty_stdout(exc, capsys):
    """Gauge: a body raising each of `Exception`, `SystemExit` and a bare
    `BaseException` subclass still leaves `run` returning 0 with empty stdout."""
    out = Recorder()
    seen = Collector()

    def body(data):
        raise exc

    assert hooks.run.run("SessionStart", body, stream=io.StringIO("{}"), out=out,
                         on_error=seen) == 0
    assert out.writes == []
    assert seen.wheres == ["body"]
    assert capsys.readouterr().err == ""


def test_l7_run_returns_zero_on_an_unreadable_stdin():
    class Broken:
        def read(self):
            raise OSError(9, "bad file descriptor")

    out = Recorder()
    seen = Collector()
    assert hooks.run.run("SessionStart", lambda d: None, stream=Broken(), out=out,
                         on_error=seen) == 0
    assert seen.wheres == ["read"]


def test_l7_run_returns_zero_on_a_stdout_that_cannot_be_written():
    out = Recorder(fail=OSError(32, "broken pipe"))
    seen = Collector()
    assert hooks.run.run("SessionStart", lambda d: "hi", stream=io.StringIO("{}"),
                         out=out, on_error=seen) == 0
    assert seen.wheres == ["emit"]


def test_l7_the_kit_never_calls_sys_exit():
    """A command keeps its own exit codes because the kit computes none; a kit that
    exited would silently delete every code such a command pins."""
    for path in ALL:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert not (node.func.attr == "exit"
                            and getattr(node.func.value, "id", "") == "sys"), (
                    f"{path.name}:{node.lineno} selects an exit code")
            if isinstance(node, ast.Raise) and node.exc is not None:
                raised = node.exc
                raised = raised.func if isinstance(raised, ast.Call) else raised
                assert getattr(raised, "id", "") != "SystemExit", (
                    f"{path.name}:{node.lineno} selects an exit code")


@pytest.mark.parametrize("fail", [
    RuntimeError("the logger is down"),
    SystemExit(2),
    KeyboardInterrupt(),
    _Bare("the logger took a signal"),
])
def test_l7_a_broken_on_error_cannot_break_exit_zero(fail):
    """A broken logger cannot break exit 0, and `run` returns 0 on EVERY path. A
    `_report` that caught `Exception` only would let a logger raising
    `KeyboardInterrupt` or `SystemExit` escape the report, escape the inner guard,
    escape `run`'s outer guard on the second report and raise out of `run`: a hook
    raising out of `main`, the defect the kit exists to close.

    Not exotic: a logger that writes to a database under lock contention, with many
    sessions starting at once, spends real time in that write, and a Ctrl-C landing
    there is a `KeyboardInterrupt` inside the logger. The gauge parametrises the LOGGER
    over every class, not only the BODY, because a gauge over one logger class passes on
    the one class the code absorbs."""
    out = Recorder()
    seen = Collector(fail=fail)

    def body(data):
        raise ValueError("and so is the body")

    assert hooks.run.run("SessionStart", body, stream=io.StringIO("{}"), out=out,
                         on_error=seen) == 0
    assert out.writes == []
    assert seen.wheres == ["body"]


@pytest.mark.parametrize("fail", [SystemExit(2), KeyboardInterrupt()])
def test_l7_a_broken_on_error_cannot_break_a_bare_guard_either(fail):
    """A caller may adopt `guard` on its own with no `run` above it, so the bare
    primitive has to hold this by itself; there is no outer frame to catch for it."""
    seen = Collector(fail=fail)
    assert hooks.run.guard(lambda: 1 / 0, on_error=seen, default="held") == "held"
    assert seen.wheres == ["body"]


def test_l7_a_wall_inside_a_broken_logger_still_reaches_the_guard_that_armed_it():
    """`_report` absorbs every `BaseException` EXCEPT the wall, because a wall firing
    inside a logger is not the logger's failure and its owner is still owed it."""
    seen = Collector()
    reached = []

    def logger(exc, *, where):
        seen(exc, where=where)
        if where == "body":
            time.sleep(5.0)          # the wall fires inside the logger
            reached.append("the logger finished")

    def body(data):
        raise ValueError("the body failed first")

    out = Recorder()
    assert hooks.run.run("SessionStart", body, stream=io.StringIO("{}"), out=out,
                         seconds=0.05, on_error=logger) == 0
    assert reached == [], "the logger outlived a wall that should have cut it short"
    assert seen.wheres == ["body", "deadline"]
    assert out.writes == []


# --- L8 — Compose, then emit, once --------------------------------------------------

def test_l8_stdout_sees_exactly_one_write_per_run_or_none():
    """Gauge: a stdout double that records every `write` call sees exactly one per run,
    or none."""
    out = Recorder()
    assert hooks.run.run("SessionStart", lambda d: "payload",
                         stream=io.StringIO("{}"), out=out) == 0
    assert len(out.writes) == 1
    assert out.flushes == 1
    assert out.writes[0].endswith("\n")

    silent = Recorder()
    assert hooks.run.run("SessionStart", lambda d: None,
                         stream=io.StringIO("{}"), out=silent) == 0
    assert silent.writes == []


def test_l8_the_wall_is_lifted_across_the_one_write_and_put_back():
    """A wall delivered between `emit`'s write and its flush leaves the line in the
    buffer, and the interpreter flushes it at exit: the hook reports a deadline AND the
    harness gets the payload, which is the one outcome L5 forbids. The window is closed
    by lifting the kit's wall across the write and putting it back, measured here rather
    than raced, because a race has no deterministic gauge."""
    during = []
    after = []

    class Watched(Recorder):
        def write(self, chunk):
            during.append(signal.getitimer(signal.ITIMER_REAL)[0])
            return super().write(chunk)

        def flush(self):
            during.append(signal.getitimer(signal.ITIMER_REAL)[0])
            return super().flush()

    out = Watched()
    assert hooks.run.run("SessionStart", lambda d: "payload",
                         stream=io.StringIO("{}"), out=out, seconds=5.0,
                         on_emitted=lambda t: after.append(
                             hooks.run.remaining())) == 0
    assert during == [0.0, 0.0], f"the wall was live across the write: {during}"
    assert after and 4.0 < after[0] <= 5.0, f"the wall was not put back: {after}"


def test_l8_a_callers_own_timer_is_not_touched_by_the_write():
    """The shield lifts the KIT's wall and nothing else: with no kit wall standing, a
    timer the caller installed for its own reasons must survive the emit untouched."""
    previous = signal.signal(signal.SIGALRM, lambda *a: None)
    signal.setitimer(signal.ITIMER_REAL, 30.0)
    try:
        hooks.run.run("SessionStart", lambda d: "payload",
                      stream=io.StringIO("{}"), out=Recorder())
        assert 29.0 < signal.getitimer(signal.ITIMER_REAL)[0] <= 30.0
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_l8_a_render_that_raises_writes_nothing_at_all():
    """Nothing reaches stdout until the whole line exists, so a refusal produces no
    output rather than a truncated one."""
    out = Recorder()
    seen = Collector()

    def render(event, text):
        raise RuntimeError("no line for you")

    assert hooks.run.run("SessionStart", lambda d: "payload",
                         stream=io.StringIO("{}"), out=out, render=render,
                         on_error=seen) == 0
    assert out.writes == []
    assert seen.wheres == ["render"]


# --- L9 — stdout carries the payload or nothing -------------------------------------

def test_l9_every_failure_path_leaves_stderr_empty(capsys):
    """Gauge: every failure path asserts `err == ""` from the kit's own frames. Where a
    diagnostic goes is the caller's (a log, a database, stderr), and the kit must not
    force any of them."""
    class Broken:
        def read(self):
            raise OSError("closed")

    def raiser(data):
        raise ValueError("boom")

    paths = [
        dict(body=raiser, stream=io.StringIO("{}")),                     # body
        dict(body=lambda d: "x", stream=Broken()),                       # read
        dict(body=lambda d: 17, stream=io.StringIO("{}")),               # render
        dict(body=lambda d: "x", stream=io.StringIO("not json")),        # read
        dict(body=lambda d: "x", stream=io.StringIO("[1,2]")),           # read
        dict(body=lambda d: "x", stream=io.StringIO("{}"), cap=5),       # render, no marker
    ]
    for kwargs in paths:
        out = Recorder()
        seen = Collector()
        assert hooks.run.run("SessionStart", out=out, on_error=seen, **kwargs) == 0
        captured = capsys.readouterr()
        assert captured.err == "", f"the kit wrote to stderr: {captured.err!r}"
        assert captured.out == "", f"the kit wrote to the real stdout: {captured.out!r}"
        assert seen.calls, f"a failure was swallowed silently: {kwargs}"


def test_l9_the_package_never_touches_stderr():
    """Read from the code rather than the prose, because the prose has to explain the
    one stderr line the kit cannot prevent, and a substring pin would read that
    explanation as the violation."""
    for path in ALL:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr != "stderr", (
                    f"{path.name}:{node.lineno} writes a diagnostic itself")
            if isinstance(node, ast.Name):
                assert node.id != "stderr", f"{path.name}:{node.lineno} names stderr"


def test_l9_the_one_stderr_line_the_kit_cannot_prevent_is_stated_where_a_reader_looks():
    """A harness that closes the read end of the pipe early makes CPython print
    `Exception ignored while flushing sys.stdout: BrokenPipeError` at shutdown. It comes
    from no frame in the kit, so L9's gauge cannot see it and no test can catch it, which
    is exactly why it has to be written down instead: in the docstring a caller reads
    when it adopts `emit`, and in the package's own docstring."""
    sentence = "Exception ignored while flushing sys.stdout: BrokenPipeError"
    assert sentence in " ".join(hooks.run.emit.__doc__.split())
    assert sentence in " ".join(hooks.__doc__.split()), (
        "the package docstring lost the shutdown caveat")


# --- L10 — No module's words --------------------------------------------------------

#: Every string the kit owns, spelled out. A new one fails this test until somebody puts
#: it here, which is the point: the L10 tripwire is only a tripwire if adding a word
#: costs a deliberate edit. Four groups, and nothing else is allowed to exist.
OWNED = {
    # the wire's own keys, the longest strings the kit owns, and not the kit's words
    "hookSpecificOutput", "hookEventName", "additionalContext", ",", ":",
    # the six `where` tokens, and the separator in `__init__`'s lazy import
    "read", "body", "render", "emit", "deadline", "emitted", ".",
    # the public names, re-listed in `__all__` and `_SUBMODULES`
    "envelope", "field", "plain", "read_event", "clamp",
    "guard", "remaining", "run", "text", "wire",
    # the four exception messages, which reach `on_error` and never stdout
    "not a JSON object", "text must be str", "cap needs a marker", "seconds must be > 0",
    # the one byte `emit` adds
    "\n",
}


def test_l10_no_string_literal_is_longer_than_a_mechanical_token():
    """Gauge: no string literal in the package is longer than a mechanical token. No
    payload prose, no verb name, no primer, no roster label, no project noun, and, since
    `marker` has no default, no string any reader sees by default. The only strings the
    kit authors are exception messages handed to `on_error`, which never touch stdout
    and which the caller uses or discards.

    Three parts, because a one-part version passes a 43-character sentence built from
    two literals of 23 and 20, and a bound on each of them sees nothing. The scan is
    recursive, so a future subpackage is not a blind spot."""
    for path in ALL:
        for node, literal in _code_literals(path):
            if not isinstance(literal, str):
                continue
            assert len(literal) <= TOKEN, (
                f"{path.name}:{node.lineno} authors words a reader could see: "
                f"{literal!r}"
            )
            assert literal in OWNED, (
                f"{path.name}:{node.lineno} grew a string nobody listed: {literal!r}")


def test_l10_no_sentence_is_assembled_out_of_short_literals():
    """The second part. A bound on each literal is not a bound on what the kit says: `+`,
    `str.join` and f-string pieces are separate Constants and the parser folds none of
    them."""
    for path in ALL:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                pair = (node.left, node.right)
                assert not all(
                    isinstance(side, ast.Constant) and isinstance(side.value, str)
                    for side in pair
                ), f"{path.name}:{node.lineno} concatenates two string literals"
            if isinstance(node, ast.JoinedStr):
                pieces = "".join(
                    part.value for part in node.values
                    if isinstance(part, ast.Constant) and isinstance(part.value, str))
                assert len(pieces) <= TOKEN, (
                    f"{path.name}:{node.lineno} builds a sentence in an f-string")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "join" or not isinstance(
                    node.func.value, ast.Constant
                ), f"{path.name}:{node.lineno} joins literals into a longer string"


def test_l10_the_kit_holds_no_event_table():
    """`envelope` writes whatever name it is handed and there is no unknown-event branch,
    because the one rule the harness holds it to is that the name must match the hook
    that fired."""
    for path in ALL:
        for node, literal in _code_literals(path):
            for event in ("SessionStart", "UserPromptSubmit", "SessionEnd",
                          "PreCompact", "PostCompact", "Stop"):
                assert literal != event, f"{path.name} carries an event name"
    line = json.loads(hooks.wire.envelope("SomethingNobodyHasHeardOf", "x"))
    assert line["hookSpecificOutput"]["hookEventName"] == "SomethingNobodyHasHeardOf"


def test_l10_the_kit_holds_no_number_anybody_measured():
    """The harness's limit, a caller's budget and a caller's bound in seconds stay the
    caller's arguments; none of them is published here."""
    for path in ALL:
        for node, literal in _code_literals(path):
            assert literal not in (10_000, 9_500, 2.0, 220), (
                f"{path.name}:{node.lineno} publishes a caller's measurement"
            )
