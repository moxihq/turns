"""Listener functions the listener tests register (stand-ins for a module's own)."""

import time

CALLS = []


def comms(event):
    CALLS.append(("comms", event.event, event.session_id, event.source))
    event.env["CHANGED"] = "by comms"
    return f"comms at {event.event}"


def cache(event):
    CALLS.append(("cache", event.event, event.env.get("CHANGED")))
    return "cache context"


def slow(event):
    time.sleep(1.0)
    return "too late"


def broken(event):
    raise RuntimeError("a bug in the listener")


def silent(event):
    return None


def not_text(event):
    return 42
