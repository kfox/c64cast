"""Recording what the last PyPI update check found, so a surface that isn't
this terminal can report it too.

`upgrade.py` answers "is there a newer release" on demand for a human running
`--check-for-updates`; nothing before this module persisted that answer
anywhere. `--check-for-updates --write-state` records an `UpdateCheck` here,
and two readers pick it up without ever querying PyPI themselves: the web
console's `GET /api/update` (`c64cast/control/web_api.py`) and, on the
appliance image, an `/etc/update-motd.d/` script via `c64cast --motd-line`
(`packaging/motd/`). Neither installs anything — see `upgrade.run_upgrade`
for the one command that does, and it always asks first.

Tolerant on read for the same reason `JsonSlotStore` is
(`c64cast/control/transport.py`): a missing or corrupt file reads as "no
check has ever run" rather than raising, since a stale write from an older
release, a half-written file from a killed process, or simply no timer
having fired yet are all normal states for an appliance that has been up for
five minutes.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from c64cast.control.transport import atomic_write_text

from . import paths, upgrade

log = logging.getLogger(__name__)

SECONDS_PER_DAY = 24 * 60 * 60

STALE_AFTER_DAYS = 30
"""How long a machine may go without a *successful* check before both
surfaces say so instead of quoting an answer that old.

Long enough that a laptop never sees it — its owner runs
`--check-for-updates` when they think of it, and a month between thoughts is
a real month — and short enough to catch an appliance that has been off the
internet since the day it was flashed. The web console reads this number off
`GET /api/update` rather than carrying its own copy, so there is one
threshold and not two that can drift."""


@dataclass(frozen=True)
class UpdateCheck:
    """The last PyPI answer, and when it was last looked for.

    `checked_at` is the last *attempt*; `latest_version` and `newer` are the
    last *answer*, which may be older — see `record_check`, which carries a
    good answer across an attempt that failed. They are both `None` only
    while no attempt has ever come back with one, mirroring
    `upgrade.is_newer`'s own tri-state for the uncomparable case.

    `unanswered_since` dates the run of failures the slot is currently in —
    when PyPI last stopped answering — and is `None` while the last attempt
    did answer. It is what tells a reader how old the held answer is, which
    `checked_at` cannot: an appliance offline for a year still bumps
    `checked_at` daily as its timer fails. Absent from files written before
    the field existed, hence the default."""

    checked_at: float
    running_version: str
    latest_version: str | None
    newer: bool | None
    unanswered_since: float | None = None


def write_update_state(check: UpdateCheck, *, path: Path | None = None) -> None:
    """Persist `check` verbatim, overwriting whatever was there before — this
    is a single "last known answer" slot, not a log.

    The low-level writer. A finished check calls `record_check` instead,
    which decides what the slot should hold; this one is for a caller that
    already knows (a test fixture, a future importer)."""
    target = path if path is not None else paths.update_check_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(target, json.dumps(asdict(check), indent=2) + "\n")


def _optional_float(value: Any) -> float | None:
    """A nullable timestamp as read from JSON. Absent and null are the same
    "no value" here — a file written before `unanswered_since` existed says
    nothing about it either way."""
    return None if value is None else float(value)


def read_update_state(*, path: Path | None = None) -> UpdateCheck | None:
    """The last recorded check, or `None` if there isn't one to trust — no
    file, unreadable, not JSON, or missing/mistyped fields. Never raises."""
    target = path if path is not None else paths.update_check_path()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data: Any = json.loads(raw)
    except ValueError:
        log.debug("update state at %s is not valid JSON, ignoring", target)
        return None
    if not isinstance(data, dict):
        log.debug("update state at %s is not a JSON object, ignoring", target)
        return None
    try:
        return UpdateCheck(
            checked_at=float(data["checked_at"]),
            running_version=str(data["running_version"]),
            latest_version=None if data["latest_version"] is None else str(data["latest_version"]),
            newer=None if data["newer"] is None else bool(data["newer"]),
            unanswered_since=_optional_float(data.get("unanswered_since")),
        )
    except (KeyError, TypeError, ValueError):
        log.debug("update state at %s has an unexpected shape, ignoring", target)
        return None


def _reanswer(check: UpdateCheck, running_version: str) -> UpdateCheck:
    """`check` with `newer` recomputed from its recorded `latest_version`
    against `running_version`. The core of `rechecked`, split out so a
    caller that already has a check in hand gets one back."""
    if check.running_version == running_version:
        return check
    newer = (
        None
        if check.latest_version is None
        else upgrade.is_newer(check.latest_version, running_version)
    )
    return replace(check, running_version=running_version, newer=newer)


def rechecked(check: UpdateCheck | None, running_version: str) -> UpdateCheck | None:
    """`check` re-answered against the version running *now*.

    A recorded `newer` was computed against whatever was running when the
    check ran, and an upgrade since then leaves it stale — a box that took
    the very release the file names would go on offering it until some later
    check overwrote the file, which on a machine that ran one manual
    `--write-state` may be never. `upgrade.is_newer` is a pure version
    comparison with no network in it, so both readers re-answer from the
    recorded `latest_version` rather than trust the recorded verdict."""
    return None if check is None else _reanswer(check, running_version)


def _unanswered_since(attempt: UpdateCheck, previous: UpdateCheck | None) -> float:
    """When the run of failures `attempt` belongs to began: whenever the slot
    already says so, else `attempt` itself is where it started."""
    if previous is None or previous.unanswered_since is None:
        return attempt.checked_at
    return previous.unanswered_since


def _carrying_last_answer(attempt: UpdateCheck, previous: UpdateCheck | None) -> UpdateCheck:
    """What the slot should hold after `attempt` — itself when it answered
    (which also ends any run of failures), else the last answer worth
    keeping, stamped with `attempt`'s time and the date its silence began.
    See `record_check` for why."""
    if attempt.latest_version is not None:
        return attempt
    dated = replace(attempt, unanswered_since=_unanswered_since(attempt, previous))
    if previous is None or previous.latest_version is None:
        return dated
    kept = replace(previous, checked_at=dated.checked_at, unanswered_since=dated.unanswered_since)
    return _reanswer(kept, attempt.running_version)


def record_check(attempt: UpdateCheck, *, path: Path | None = None) -> None:
    """Record what `attempt` found, and what it didn't.

    An attempt that came back with no answer (PyPI unreachable) has learned
    nothing about which release is current, so it contributes only its
    `checked_at` and leaves the last real answer standing. Writing its empty
    hands instead would retract a pending-upgrade notice the moment a name
    server hiccupped — and leave it retracted until some later attempt
    succeeded, which is a day on the appliance's timer and possibly never on
    a machine that ran one manual `--write-state`. The carried-forward
    answer is re-answered for the version running now (`_reanswer`), so an
    upgrade taken between the two attempts still settles it."""
    target = path if path is not None else paths.update_check_path()
    write_update_state(_carrying_last_answer(attempt, read_update_state(path=target)), path=target)


def is_stale(check: UpdateCheck | None, now: float) -> bool:
    """Whether this host has gone `STALE_AFTER_DAYS` without hearing from
    PyPI, which makes whatever answer it holds too old to quote as current.

    Dated from `unanswered_since` — when the silence began — falling back to
    `checked_at` for a host that is still being answered, where the last
    attempt *is* the last thing it learned. `checked_at` alone would never
    do: an appliance offline for a year still bumps it daily as its timer
    fails. `now` is a parameter rather than a clock read so this stays a
    pure function, testable without patching time."""
    if check is None:
        return False
    last_answered = check.checked_at if check.unanswered_since is None else check.unanswered_since
    return now - last_answered > STALE_AFTER_DAYS * SECONDS_PER_DAY


def motd_line(check: UpdateCheck | None, now: float) -> str:
    """The line `/etc/update-motd.d/` prints at login, or `""` when there is
    nothing worth saying at a login prompt — no check has ever run, or the
    last one found this install current and recently enough to believe.

    A pending upgrade outranks a stale check, the same way it does in the
    console's banner: naming the release to move to already says everything
    "we haven't heard from PyPI" would, and an admin who acts on it fixes
    both."""
    if check is None:
        return ""
    if check.newer and check.latest_version is not None:
        return (
            f"A newer c64cast release is available: {check.latest_version} "
            f"(running {check.running_version}). Upgrade with: c64cast --upgrade"
        )
    if is_stale(check, now):
        return (
            f"No answer from PyPI in over {STALE_AFTER_DAYS} days: this machine cannot say "
            f"whether c64cast {check.running_version} is still current. "
            "Check with: c64cast --check-for-updates"
        )
    return ""
