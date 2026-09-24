"""`--log-file` is a destination a remote peer can write to, so it has a cap.

Every record that reaches the root logger lands in the file `--log-file`
names, and some of those records are raised by a peer rather than by the
operator: one malformed TCP connection to the web console costs a 74-byte
`uvicorn.error` warning at every verbosity, and `control/web_api.py`'s
refusal warning is request-driven as well (c64cast#503). A plain
`logging.FileHandler` has no size of its own, so the file's growth was the
peer's to choose.

What is pinned here is the bound rather than the arithmetic behind it:
`configure_logging` installs a `RotatingFileHandler`, the shipped numbers are
ones that actually rotate, the file set stops growing where the numbers say,
the tail of the run survives the rotation, and the redaction the file handler
wears applies to the rotated backups too. `AstSweepTest` holds the rest of the
package to the same shape, since a second plain `FileHandler` would reopen the
class somewhere else.
"""

from __future__ import annotations

import ast
import logging
import logging.handlers
import os
import pathlib
import tempfile
import unittest
from unittest.mock import patch

import c64cast
from c64cast.app import cli_commands
from c64cast.app.config import DebugCfg

#: The peer-driven record the issue measured, verbatim.
PEER_LINE = "Invalid HTTP request received."

LOGIN_LINE = "web console: open http://127.0.0.1:8123/api/login?token=s3cr3t&next=/"


def _sizes(path: str) -> dict[str, int]:
    """Every file the rotating handler owns at `path`, by name."""
    directory = pathlib.Path(path).parent
    stem = pathlib.Path(path).name
    return {
        p.name: p.stat().st_size
        for p in sorted(directory.iterdir())
        if p.name == stem or p.name.startswith(stem + ".")
    }


class _RestoresLogging(unittest.TestCase):
    """`configure_logging` replaces the root handlers and moves the levels of
    the loggers it holds back, all of which belong to the process."""

    HELD_BACK = ("urllib3", "urllib3.connectionpool", "uvicorn", "uvicorn.error")

    def setUp(self):
        root = logging.getLogger()
        handlers, level = root.handlers[:], root.level
        saved = {name: logging.getLogger(name).level for name in self.HELD_BACK}

        def restore() -> None:
            for handler in root.handlers[:]:
                if handler not in handlers:
                    handler.close()
            root.handlers[:] = handlers
            root.setLevel(level)
            for name, lvl in saved.items():
                logging.getLogger(name).setLevel(lvl)

        self.addCleanup(restore)

    def configure_file_only(self, verbosity: int = 0) -> str:
        """Configure logging onto a throwaway path and drop the terminal half.

        The terminal handler is what would print the driven records between
        the suite's dots; the file is where they are read back from anyway.
        """
        path = os.path.join(tempfile.mkdtemp(), "run.log")
        cli_commands.configure_logging(verbosity, log_file=path)
        root = logging.getLogger()
        for handler in root.handlers[:]:
            if not isinstance(handler, logging.FileHandler):
                root.removeHandler(handler)
                handler.close()
        return path

    def drive(self, count: int, message: str = PEER_LINE) -> None:
        """Raise `count` peer-shaped records onto the configured file."""
        peer = logging.getLogger("uvicorn.error")
        for _ in range(count):
            peer.warning(message)
        for handler in logging.getLogger().handlers:
            handler.flush()


class ShippedBoundTest(unittest.TestCase):
    def test_neither_shipped_bound_is_off(self):
        """`maxBytes=0` is how `RotatingFileHandler` spells "never rotate",
        and `backupCount=0` turns a rotation into a truncation — either one
        keeps the class open with the constants still in place."""
        self.assertGreater(cli_commands.LOG_FILE_MAX_BYTES, 0)
        self.assertGreater(cli_commands.LOG_FILE_BACKUP_COUNT, 0)

    def test_the_shipped_size_clears_a_per_scene_snapshot(self):
        """A record longer than `maxBytes` rotates the file on its own, one
        record per file. The longest one an ordinary run writes is
        `recording_metadata`'s per-scene SCENE_CONFIG_JSON line, measured at
        1,618 bytes for a video scene on a default config."""
        self.assertGreater(cli_commands.LOG_FILE_MAX_BYTES, 64 * 1024)

    def test_the_config_help_states_the_shipped_bound(self):
        """`[debug].log_file`'s help renders into `--describe`, the JSON
        schema and the reference appendix, and cannot reach the constants it
        quotes — config.py is below cli_commands.py in the import order."""
        help_text = DebugCfg.__dataclass_fields__["log_file"].metadata["help"]
        megabytes = cli_commands.LOG_FILE_MAX_BYTES // (1024 * 1024)
        self.assertIn(f"{megabytes} MiB", help_text)
        self.assertIn(f"{cli_commands.LOG_FILE_BACKUP_COUNT} rotated backups", help_text)


class InstalledHandlerTest(_RestoresLogging):
    def test_configure_logging_installs_a_rotating_handler(self):
        self.configure_file_only()
        handlers = logging.getLogger().handlers
        files = [h for h in handlers if isinstance(h, logging.FileHandler)]
        rotating = [h for h in handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
        self.assertEqual(len(files), 1)
        self.assertEqual(len(rotating), 1)
        self.assertEqual(rotating[0].maxBytes, cli_commands.LOG_FILE_MAX_BYTES)
        self.assertEqual(rotating[0].backupCount, cli_commands.LOG_FILE_BACKUP_COUNT)

    def test_it_still_redacts(self):
        """Rotation is a second `format()` call per record — the size check
        measures the line it is about to write — so the formatter has to be
        the redacting one on the handler that rotates, not beside it."""
        self.configure_file_only()
        handler = logging.getLogger().handlers[0]
        self.assertIsInstance(handler.formatter, cli_commands.RedactingFormatter)

    def test_a_path_that_cannot_be_opened_is_reported_not_raised(self):
        path = os.path.join(tempfile.mkdtemp(), "no-such-dir", "run.log")
        with self.assertLogs("c64cast", level=logging.WARNING) as cm:
            cli_commands.configure_logging(0, log_file=path)
        self.assertIn("could not open log file", cm.output[0])
        files = [h for h in logging.getLogger().handlers if isinstance(h, logging.FileHandler)]
        self.assertEqual(files, [])


@patch.object(cli_commands, "LOG_FILE_MAX_BYTES", 2048)
@patch.object(cli_commands, "LOG_FILE_BACKUP_COUNT", 2)
class BoundedGrowthTest(_RestoresLogging):
    """The bound itself, driven with the record the issue measured.

    `configure_logging` reads both constants from the module body at call
    time, so the patch above is what lets a test cross the threshold in
    kilobytes rather than in the shipped tens of megabytes. The ratio between
    what was written and what survives is the assertion — a handler that did
    not rotate would hold all of it.
    """

    RECORDS = 300

    def cap(self) -> int:
        return cli_commands.LOG_FILE_MAX_BYTES * (cli_commands.LOG_FILE_BACKUP_COUNT + 1)

    def test_the_file_set_stops_growing_at_the_cap(self):
        path = self.configure_file_only()
        self.drive(self.RECORDS)
        sizes = _sizes(path)
        written = sum(sizes.values())
        unrotated = self.RECORDS * 74
        self.assertGreater(unrotated, self.cap() * 2, "the flood has to clear the cap to test it")
        self.assertLessEqual(written, self.cap())
        self.assertEqual(len(sizes), cli_commands.LOG_FILE_BACKUP_COUNT + 1)

    def test_the_backups_are_kept_not_discarded(self):
        """`backupCount=0` truncates at the cap instead, which throws the
        whole history away at the moment the file fills."""
        path = self.configure_file_only()
        self.drive(self.RECORDS)
        backups = [name for name in _sizes(path) if name != "run.log"]
        self.assertEqual(sorted(backups), ["run.log.1", "run.log.2"])

    def test_the_newest_records_survive(self):
        path = self.configure_file_only()
        self.drive(self.RECORDS - 1)
        self.drive(1, message="the last thing that happened")
        with open(path, encoding="utf-8") as f:
            tail = f.read().splitlines()[-1]
        self.assertTrue(tail.endswith("the last thing that happened"), tail)

    def test_no_rotated_file_carries_the_token(self):
        """The redaction is on the live handler; a backup is a file that
        handler wrote and then renamed, so the two cannot disagree — but the
        rename is exactly the step that would carry an unredacted line out of
        reach of a test that only reads `run.log`."""
        path = self.configure_file_only()
        console = logging.getLogger("c64cast.control")
        for _ in range(self.RECORDS):
            console.info(LOGIN_LINE)
        for handler in logging.getLogger().handlers:
            handler.flush()
        directory = pathlib.Path(path).parent
        for name in _sizes(path):
            with self.subTest(file=name):
                text = (directory / name).read_text(encoding="utf-8")
                self.assertNotIn("s3cr3t", text)
                self.assertIn("token=REDACTED", text)


class AstSweepTest(unittest.TestCase):
    """No module under `c64cast/` may install a plain `FileHandler`.

    One unrotated handler was the defect; a second one added later would be
    the same defect under a different name, and it would not be found by a
    test that reads only `configure_logging`.
    """

    UNROTATED = {"FileHandler", "WatchedFileHandler"}

    def _handler_name(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Attribute):
            return node.attr
        if isinstance(node, ast.Name):
            return node.id
        return None

    def test_the_package_installs_no_unrotated_file_handler(self):
        root = pathlib.Path(c64cast.__file__).parent
        offenders: list[str] = []
        for source in sorted(root.rglob("*.py")):
            tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = self._handler_name(node.func)
                if name in self.UNROTATED:
                    offenders.append(f"{source.relative_to(root)}:{node.lineno} {name}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
