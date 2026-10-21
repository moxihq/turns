"""The size cap that admits its own cuts.

One function, carrying one rule: the marker's own length is reserved BEFORE anything is
spent, because if the marker is the thing that does not fit then the cut is silent, and
a silent cut is the failure the whole cap exists to avoid. Claude Code does not truncate
an oversized `additionalContext`: past its limit it replaces the payload, so a cap that
overruns loses everything and a cut that does not say so loses the reader's knowledge
that anything was cut.

The kit owns the admission. The number and the wording are the caller's: `marker` is
required and has no default, and there is no default cap. The kit owns no string a
reader ever sees, so a cap without a marker is a caller error rather than an invitation
for the kit to word something.
"""

from __future__ import annotations

__all__ = ["clamp"]


def clamp(text: str, cap: int, *, marker: str) -> str:
    """`text` when it fits in `cap`; otherwise `text` cut to `cap` with `marker` appended.

    The result is at most `cap` characters and ends with `marker` whenever anything was
    dropped (L6). Two corners, resolved the only way that keeps the first half of that
    sentence true:

    - `cap` below `len(marker)` cannot both fit and carry the whole admission. The
      length bound wins and the marker is itself cut: a caller who leaves less room than
      the admission needs has already lost the admission, and overrunning `cap` would
      lose the payload to the harness's replacement instead.
    - a negative `cap` is read as zero.
    """
    if len(text) <= cap:
        return text
    room = cap - len(marker)
    if room <= 0:
        return marker[: max(cap, 0)]
    return text[:room] + marker
