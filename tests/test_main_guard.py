"""Every test module ends with its `if __name__ == "__main__":` guard.

Discovery imports a module whole, so a class defined after the guard still
runs under `make test`. Running the file directly does not: `unittest.main()`
fires from inside the guard before the rest of the module has been executed,
and every test below it silently drops out of the run. Five modules had grown
classes there — test_hw_provision ran 19 of its 66 tests that way.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

_TESTS = pathlib.Path(__file__).resolve().parent


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def code_after_main_guard(source: str) -> int | None:
    """The line of a main guard that has statements after it, else None."""
    body = ast.parse(source).body
    for index, node in enumerate(body):
        if _is_main_guard(node) and index != len(body) - 1:
            return node.lineno
    return None


class CodeAfterMainGuardTest(unittest.TestCase):
    def test_a_guard_on_the_last_statement_passes(self):
        source = 'import unittest\n\nif __name__ == "__main__":\n    unittest.main()\n'
        self.assertIsNone(code_after_main_guard(source))

    def test_a_class_after_the_guard_is_named_by_the_guard_line(self):
        source = 'if __name__ == "__main__":\n    pass\n\n\nclass Late:\n    pass\n'
        self.assertEqual(code_after_main_guard(source), 1)

    def test_no_test_module_has_code_after_its_main_guard(self):
        offenders = [
            f"{path.name}:{line}"
            for path in sorted(_TESTS.glob("test_*.py"))
            if (line := code_after_main_guard(path.read_text(encoding="utf-8"))) is not None
        ]
        self.assertEqual(offenders, [], "move the main guard to the end of each module")


if __name__ == "__main__":
    unittest.main()
