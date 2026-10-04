"""Every name a diag tool imports still exists.

The diag tools under scripts/diags/ run only by hand against real hardware, so
nothing else notices when a rename in the package leaves one importing a name
that is gone. This reads each tool's imports from its source without running
it, and resolves them: ``c64cast`` names against the imported package, and the
tools' imports of one another (``_diaglib`` and friends) against the top-level
names of that file, plus what a module ``__getattr__`` answers.
"""

from __future__ import annotations

import ast
import importlib
import tempfile
import unittest
from pathlib import Path
from types import ModuleType

_DIAGS = Path(__file__).resolve().parents[1] / "scripts" / "diags"
_LOCAL_PREFIX = "scripts.diags."


def _lazy_attribute_names(getattr_def: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    """The names a PEP 562 module ``__getattr__`` answers: the string constants
    its parameter is compared against (``if name == "X":``)."""
    param = getattr_def.args.args[0].arg
    names: set[str] = set()
    for node in ast.walk(getattr_def):
        if (
            isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == param
        ):
            names.update(
                c.value
                for c in node.comparators
                if isinstance(c, ast.Constant) and isinstance(c.value, str)
            )
    return names


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()

    def visit(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
                if node.name == "__getattr__" and not isinstance(node, ast.ClassDef):
                    names.update(_lazy_attribute_names(node))
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    names.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(node, (ast.If, ast.Try, ast.With)):
                visit(node.body)
                visit(getattr(node, "orelse", []))
                visit(getattr(node, "finalbody", []))
                for handler in getattr(node, "handlers", []):
                    visit(handler.body)

    visit(tree.body)
    return names


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


_LOCAL_NAMES = {path.stem: _top_level_names(_parse(path)) for path in _DIAGS.glob("*.py")}


def _local_stem(module: str) -> str | None:
    stem = module.removeprefix(_LOCAL_PREFIX)
    return stem if stem in _LOCAL_NAMES else None


def _resolves(module: str, name: str) -> bool:
    """Whether ``from module import name`` would find ``name``."""
    stem = _local_stem(module)
    if stem is not None:
        return name in _LOCAL_NAMES[stem]
    owner = importlib.import_module(module)
    if hasattr(owner, name):
        return True
    if not hasattr(owner, "__path__"):
        return False
    try:
        importlib.import_module(f"{module}.{name}")
    except ModuleNotFoundError:
        return False
    return True


def _submodule(module: str, name: str) -> str | None:
    """The module ``from module import name`` binds, when it is one this sweep
    checks, so reads through that name can be checked too."""
    if _local_stem(module) is not None:
        return None
    bound = getattr(importlib.import_module(module), name, None)
    if isinstance(bound, ModuleType) and _is_checked(bound.__name__):
        return bound.__name__
    return None


def _is_checked(module: str) -> bool:
    return module.split(".")[0] == "c64cast" or _local_stem(module) is not None


def _unresolved(path: Path) -> list[str]:
    """Each import in ``path`` naming something its source no longer defines,
    plus each ``alias.attr`` read through a checked module alias that misses."""
    tree = _parse(path)
    missing: list[str] = []
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not _is_checked(alias.name):
                    continue
                if _local_stem(alias.name) is None:
                    importlib.import_module(alias.name)
                if alias.asname or "." not in alias.name:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if not _is_checked(node.module):
                continue
            for alias in node.names:
                if alias.name == "*":
                    continue
                if not _resolves(node.module, alias.name):
                    missing.append(f"line {node.lineno}: from {node.module} import {alias.name}")
                    continue
                submodule = _submodule(node.module, alias.name)
                if submodule is not None:
                    aliases[alias.asname or alias.name] = submodule
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and not _resolves(aliases[node.value.id], node.attr)
        ):
            missing.append(f"line {node.lineno}: {node.value.id}.{node.attr}")
    return missing


class DiagImportsResolveTests(unittest.TestCase):
    def test_every_diag_tool_imports_only_names_that_exist(self):
        tools = sorted(_DIAGS.glob("*.py"))
        self.assertTrue(tools, f"no diag tools found under {_DIAGS}")
        for path in tools:
            with self.subTest(tool=path.name):
                try:
                    missing = _unresolved(path)
                except ModuleNotFoundError as exc:
                    if (exc.name or "").split(".")[0] == "c64cast":
                        raise
                    self.skipTest(f"{path.name}: optional dependency {exc.name} not installed")
                self.assertEqual(missing, [], f"{path.name} imports names that do not exist")


class UnresolvedImportDetectionTests(unittest.TestCase):
    """The sweep above passes on a clean tree; these prove it can fail."""

    def _check(self, source: str) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.py"
            path.write_text(source, encoding="utf-8")
            return _unresolved(path)

    def test_a_renamed_package_constant_is_reported(self):
        missing = self._check("from c64cast.audio.audio_handlers import REU_PUMP_CIA1_LATCH\n")
        self.assertEqual(
            missing, ["line 1: from c64cast.audio.audio_handlers import REU_PUMP_CIA1_LATCH"]
        )

    def test_a_missing_diaglib_attribute_is_reported(self):
        missing = self._check("import _diaglib as d\nd.no_such_helper()\n")
        self.assertEqual(missing, ["line 2: d.no_such_helper"])

    def test_a_missing_name_from_a_sibling_tool_is_reported(self):
        missing = self._check("from ring_race_probe import no_such_name\n")
        self.assertEqual(missing, ["line 1: from ring_race_probe import no_such_name"])

    def test_names_that_exist_pass(self):
        missing = self._check(
            "import _diaglib as d\n"
            "from c64cast.hw.c64 import CIA1\n"
            "from c64cast.audio import audio_handlers\n"
            "print(d.U64_URL, CIA1.TIMER_A_LO, audio_handlers.REU_PUMP_CIA1_LATCH_8KHZ)\n"
        )
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
