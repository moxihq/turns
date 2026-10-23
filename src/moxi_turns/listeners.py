"""The listener group: modules registered with turns for session events.

A module registers by declaring an entry point in the group `moxi.turns.listeners`,
named by the module, whose target is a stdlib-only module holding `LISTENERS`:

    [project.entry-points."moxi.turns.listeners"]
    comms = "moxi_comms.listeners"

    # moxi_comms/listeners.py
    LISTENERS = (
        (("ready", "prompt"), "moxi_comms.context:answer"),
    )

Each row names the events it wants (`ready`, `prompt`, `turn`, `gone`) and a function,
`"package.module:function"`, called with one `SessionEvent`. For `ready` and `prompt` it
returns text or None, and turns hands the texts, joined in module-name order, to the
harness as the event's added context. For `turn` and `gone` the return is ignored.

Asking: every function that names the event runs in its own thread; all start together
and are bounded together by one `listen.wait_ms`. An exception or a late answer is logged
once and counts as None, so a broken listener costs its own context and never the
session's turn. A function is imported in its thread at its first ask, so a slow import
costs that ask and not the endpoint's start.
"""

from __future__ import annotations

import importlib
import logging
import re
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace

from moxi import names

from .results import SessionEvent

log = logging.getLogger("moxi.turns.listeners")

#: The entry-point group.
GROUP = "moxi.turns.listeners"
#: The events a listener may name.
EVENTS = ("ready", "prompt", "turn", "gone")
#: The events whose answers turns passes back.
CONTEXT_EVENTS = ("ready", "prompt")
#: How listener texts are joined into one added context.
JOIN = "\n\n"

_TARGET = re.compile(r"[A-Za-z_]\w*(\.[A-Za-z_]\w*)*:[A-Za-z_]\w*")


@dataclass
class Listener:
    """One row of one module's `LISTENERS`."""

    module: str
    events: frozenset[str]
    target: str
    _fn: Callable[[SessionEvent], object] | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def function(self) -> Callable[[SessionEvent], object]:
        with self._lock:
            if self._fn is None:
                module_name, _, attr = self.target.partition(":")
                self._fn = getattr(importlib.import_module(module_name), attr)
            return self._fn


@dataclass(frozen=True)
class Broken:
    """A registration that could not be read, and why (for `check()`)."""

    module: str
    reason: str


@dataclass(frozen=True)
class Registry:
    """What the group held when it was loaded."""

    listeners: tuple[Listener, ...] = ()
    broken: tuple[Broken, ...] = ()

    def naming(self, event: str) -> list[Listener]:
        return [ls for ls in self.listeners if event in ls.events]


def load(entries: Iterable | None = None) -> Registry:
    """Read the group from this environment's metadata (or from `entries`, each with
    `.name` and `.load()`, for tests). Never raises."""
    if entries is None:
        try:
            from importlib.metadata import entry_points

            entries = list(entry_points(group=GROUP))
        except Exception as exc:  # noqa: BLE001 - a broken environment costs listeners
            log.warning("reading the %s group failed: %s", GROUP, exc)
            return Registry()
    by_name: dict[str, list] = {}
    for ep in entries:
        by_name.setdefault(ep.name, []).append(ep)
    listeners: list[Listener] = []
    broken: list[Broken] = []
    for name in sorted(by_name):
        claims = by_name[name]
        if len(claims) > 1:
            broken.append(Broken(name, f"{len(claims)} distributions register listeners "
                                       f"named {name!r}; uninstall all but one"))
            continue
        if not names.is_module_name(name):
            broken.append(Broken(name, f"{name!r} is not a Moxi module name"))
            continue
        try:
            rows = _rows(claims[0].load())
        except Exception as exc:  # noqa: BLE001
            broken.append(Broken(name, f"its registration does not load: "
                                       f"{type(exc).__name__}: {exc}"))
            continue
        listeners.extend(Listener(name, events, target) for events, target in rows)
    for b in broken:
        log.warning("listeners of %s are off: %s", b.module, b.reason)
    return Registry(tuple(listeners), tuple(broken))


def _rows(module) -> list[tuple[frozenset[str], str]]:
    rows = getattr(module, "LISTENERS", None)
    if not isinstance(rows, (tuple, list)):
        raise ValueError("it defines no LISTENERS tuple")
    out = []
    for row in rows:
        if not (isinstance(row, (tuple, list)) and len(row) == 2):
            raise ValueError(f"the row {row!r} is not (events, target)")
        events, target = row
        if isinstance(events, str):
            events = (events,)
        events = frozenset(events)
        unknown = sorted(e for e in events if e not in EVENTS)
        if not events or unknown:
            raise ValueError(f"the row {row!r} names events {unknown or '(none)'}; "
                             f"use {', '.join(EVENTS)}")
        if not isinstance(target, str) or not _TARGET.fullmatch(target):
            raise ValueError(f"the row {row!r} has target {target!r}; use "
                             '"package.module:function"')
        out.append((events, target))
    return out


def ask(registry: Registry, event: SessionEvent, wait_s: float, *,
        wait: bool = False) -> str | None:
    """Ask every listener that names `event.event`, bounded together by `wait_s`.

    For `ready` and `prompt`, the texts joined in module-name order, or None. For `turn`
    and `gone` the answers are ignored and this returns None at once, the calls running on,
    unless `wait` asks it to wait for them up to `wait_s` too (the endpoint's own end, so
    a `gone` call is not cut off by the process exiting).
    """
    chosen = registry.naming(event.event)
    if not chosen:
        return None
    answers: list[object] = [None] * len(chosen)
    done = [False] * len(chosen)
    # Each call gets its own copy of the environment, so no listener changes another's.
    threads = [threading.Thread(target=_call,
                                args=(ls, replace(event, env=dict(event.env)), answers,
                                      done, i),
                                name=f"turns-listener-{ls.module}", daemon=True)
               for i, ls in enumerate(chosen)]
    for t in threads:
        t.start()
    context = event.event in CONTEXT_EVENTS
    if not (context or wait):
        return None
    deadline = time.monotonic() + max(0.0, wait_s)
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
    if not context:
        return None
    texts = []
    for i, listener in enumerate(chosen):
        if not done[i]:
            log.warning("listener %s (%s) did not answer %s within %d ms; it counts as "
                        "no answer", listener.module, listener.target, event.event,
                        int(wait_s * 1000))
            continue
        answer = answers[i]
        if answer is None:
            continue
        if not isinstance(answer, str):
            log.warning("listener %s (%s) answered %s with a %s, not text; it counts as "
                        "no answer", listener.module, listener.target, event.event,
                        type(answer).__name__)
            continue
        if answer:
            texts.append(answer)
    return JOIN.join(texts) if texts else None


def _call(listener: Listener, event: SessionEvent, answers: list, done: list,
          i: int) -> None:
    try:
        answers[i] = listener.function()(event)
    except Exception as exc:  # noqa: BLE001 - a broken listener costs its own answer
        log.warning("listener %s (%s) raised at %s: %s: %s; it counts as no answer",
                    listener.module, listener.target, event.event, type(exc).__name__, exc)
        answers[i] = None
    done[i] = True


__all__ = ["GROUP", "EVENTS", "CONTEXT_EVENTS", "JOIN", "Listener", "Broken", "Registry",
           "SessionEvent", "load", "ask"]
