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
release, a file some other tool put there, or simply no timer having fired
yet are all normal states for an appliance that has been up for five
minutes. (Not a torn write — `atomic_write_text` fsyncs a temp file and
`os.replace`s it, so no reader ever sees half of one.)

Tolerant here means *never raises and never guesses*, which is why the read
path type-checks every field instead of coercing it: `float()`, `bool()` and
`str()` cannot fail, so they turn a mistyped field into a confident wrong
answer where rejecting it reads as the routine "nothing recorded yet". The
strictness is not academic — the file is written by a lower-privileged
account than the one that renders it (see `_version`).
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, TypeVar

from c64cast.control.transport import atomic_write_text

from . import paths, upgrade

log = logging.getLogger(__name__)

_T = TypeVar("_T")

SECONDS_PER_DAY = 24 * 60 * 60

_FUTURE_TOLERANCE_S = SECONDS_PER_DAY
"""How far ahead of `now` a recorded timestamp may sit before `is_stale`
stops believing it — a day, which covers a clock skewed by a time-zone
mistake or an NTP correction and nothing more."""

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


def _timestamp(value: Any) -> float:
    """A timestamp as read from JSON, raising when it cannot be one.

    `float()` alone let through three values that turn `is_stale` off for
    good: `json.loads` accepts the bare literals `NaN` and `Infinity`, and
    every comparison in `is_stale` reads as "not stale" for a NaN, for an
    infinity, and for a date far enough in the future. An age that cannot be
    computed is not an age, and the whole job of these fields is to say how
    old the held answer is — so a record carrying one is no record at all.
    `bool` is excluded explicitly because it is a subclass of `int`, so
    `"checked_at": true` would otherwise read as the epoch's first second."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"not a timestamp: {value!r}")
    stamp = float(value)
    if not math.isfinite(stamp) or stamp <= 0:
        raise ValueError(f"not a usable timestamp: {stamp!r}")
    return stamp


def _version(value: Any) -> str:
    """A version string as read from JSON, raising when it isn't one.

    Charset-checked (`upgrade.looks_like_version`), not just type-checked,
    because this is a privilege crossing:
    `packaging/systemd/c64cast-update-check.service` writes this file as the
    unprivileged `c64cast` account, while `packaging/motd/98-c64cast-update`
    renders `motd_line`'s text from `/etc/update-motd.d/`, which pam_motd
    runs **as root** at every login — and the unit's own comment says both
    surfaces have to resolve the same file or the feature does nothing, so
    the working configuration is exactly the one where a low-privilege
    account owns a file root reads. A bare `str()` imposed no constraint at
    all: a newline in one of these forges an extra, official-looking MOTD
    line ("SECURITY: apply the hotfix now: curl … | sudo sh"), an ESC or OSC
    payload rewrites the banner around it, and a JSON list became its
    repr."""
    if not upgrade.looks_like_version(value):
        raise ValueError(f"not a version: {value!r}")
    return value


def _flag(value: Any) -> bool:
    """A boolean as read from JSON, raising when it isn't one. `bool()`
    accepted every non-null value and `bool("false")` is `True`, so a
    mistyped field spelled a pending upgrade that does not exist — and
    `rechecked` cannot correct it, since a record whose `running_version`
    already matches is returned untouched."""
    if not isinstance(value, bool):
        raise TypeError(f"not a boolean: {value!r}")
    return value


def _optional(value: Any, parse: Callable[[Any], _T]) -> _T | None:
    """`parse(value)`, with JSON's null passed straight through. Absent and
    null are the same "no value" here — a file written before
    `unanswered_since` existed says nothing about it either way."""
    return None if value is None else parse(value)


def _read_slot(target: Path) -> UpdateCheck:
    """`target`'s contents as an `UpdateCheck`, raising anything that goes
    wrong for :func:`read_update_state` to absorb.

    Every field is type-checked rather than coerced. `float()`, `bool()` and
    `str()` never raise, so a mistyped field became a confident wrong answer
    — the login banner offering a release the box already runs — where a
    rejection reads as the routine "no check has ever run"."""
    data: Any = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise TypeError(f"not a JSON object: {type(data).__name__}")
    return UpdateCheck(
        checked_at=_timestamp(data["checked_at"]),
        running_version=_version(data["running_version"]),
        latest_version=_optional(data["latest_version"], _version),
        newer=_optional(data["newer"], _flag),
        unanswered_since=_optional(data.get("unanswered_since"), _timestamp),
    )


def read_update_state(*, path: Path | None = None) -> UpdateCheck | None:
    """The last recorded check, or `None` if there isn't one to trust — no
    file, unreadable, not JSON, or a field that isn't what it claims to be.
    Never raises.

    The guard is one broad `except` rather than a list of exception types,
    because the list was wrong: `Path.read_text(encoding="utf-8")` signals
    invalid bytes with `UnicodeDecodeError`, a `ValueError`, and the read was
    wrapped in `except OSError` alone — so a file holding one bad byte raised
    out of a "never raises" function into `GET /api/update` and into the
    `/etc/update-motd.d/` script that runs at every login. `record_check`
    reads before it writes, so the one run that would have replaced the bad
    file died first and the slot stayed broken until a human deleted it.

    A missing file is the routine "no check has ever run" and stays silent;
    anything else leaves a debug breadcrumb naming what was wrong."""
    target = path if path is not None else paths.update_check_path()
    try:
        return _read_slot(target)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.debug("update state at %s is unusable (%s), ignoring", target, e)
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
    upgrade taken between the two attempts still settles it.

    A write that cannot succeed is reported and dropped, not raised. The
    caller (`cli_commands.run_check_for_updates`) records the answer *before*
    it prints the one it already holds, so a data root that is read-only,
    full, or holding a directory where the file belongs used to turn
    `--check-for-updates --write-state` into a traceback that also threw away
    the network answer the user asked for — and made
    `c64cast-update-check.service` fail with a stack trace despite the
    `SuccessExitStatus` it carefully enumerates.

    **Single writer by convention.** The read-modify-write is not locked, so
    two concurrent runs (the appliance's daily timer overlapping an admin's
    manual `--write-state`) both see the same previous slot and the loser's
    contribution is dropped — bounded, since the slot holds one last-known
    answer and the next successful check heals it, but not nil: a failed
    attempt that read before a successful one wrote will carry the older
    answer forward and re-date the silence from now. Add a second writer (a
    web-console "check now" button is the obvious one) and this wants an
    `O_EXCL` lockfile beside `update_check.json` around both steps."""
    target = path if path is not None else paths.update_check_path()
    try:
        write_update_state(
            _carrying_last_answer(attempt, read_update_state(path=target)), path=target
        )
    except OSError as e:
        log.warning("could not record the update check at %s: %s", target, e)


def is_stale(check: UpdateCheck | None, now: float) -> bool:
    """Whether this host has gone `STALE_AFTER_DAYS` without hearing from
    PyPI, which makes whatever answer it holds too old to quote as current.

    Dated from `unanswered_since` — when the silence began — falling back to
    `checked_at` for a host that is still being answered, where the last
    attempt *is* the last thing it learned. `checked_at` alone would never
    do: an appliance offline for a year still bumps it daily as its timer
    fails. `now` is a parameter rather than a clock read so this stays a
    pure function, testable without patching time.

    A date it cannot believe — not finite, or more than `_FUTURE_TOLERANCE_S`
    ahead of `now` — counts as stale rather than fresh: "can't tell how old
    this is" is not "recent", the same asymmetry `_checkout_is_dirty`'s
    `None` gets in `upgrade.py`. Without it, one bogus timestamp in the file
    disabled the module's only safeguard against quoting a dead answer,
    permanently and with no other symptom — an appliance that had missed a
    year of security releases saying nothing about it at either surface."""
    if check is None:
        return False
    last_answered = check.checked_at if check.unanswered_since is None else check.unanswered_since
    if not math.isfinite(last_answered):
        return True
    age = now - last_answered
    return age < -_FUTURE_TOLERANCE_S or age > STALE_AFTER_DAYS * SECONDS_PER_DAY


def motd_line(check: UpdateCheck | None, now: float) -> str:
    """The line `/etc/update-motd.d/` prints at login, or `""` when there is
    nothing worth saying at a login prompt — no check has ever run, or the
    last one found this install current and recently enough to believe.

    A pending upgrade outranks a stale check, the same way it does in the
    console's banner: naming the release to move to already says everything
    "we haven't heard from PyPI" would, and an admin who acts on it fixes
    both.

    The stale line says no check has *succeeded*, not that PyPI stopped
    answering. `is_stale` falls back to `checked_at` when `unanswered_since`
    is None, and in that branch the last attempt did answer — the no-timer
    laptop whose owner simply hasn't asked in a while — so blaming PyPI
    there sent an admin hunting a network fault that does not exist.

    Both versions in the line reach a terminal, which is the one sink that
    acts on control characters; `read_update_state` is where they are gated
    (:func:`_version`), so nothing shaped unlike a version can arrive
    here."""
    if check is None:
        return ""
    if check.newer and check.latest_version is not None:
        return (
            f"A newer c64cast release is available: {check.latest_version} "
            f"(running {check.running_version}). Upgrade with: c64cast --upgrade"
        )
    if is_stale(check, now):
        return (
            f"No update check has succeeded in over {STALE_AFTER_DAYS} days: this machine "
            f"cannot say whether c64cast {check.running_version} is still current. "
            "Check with: c64cast --check-for-updates"
        )
    return ""
