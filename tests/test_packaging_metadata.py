"""Drift guards for the published package metadata in pyproject.toml.

Everything here fails only at install time — long after CI is green and
the wheel is on PyPI — which is exactly why it is worth asserting in the suite:

  1. The `all` extra is hand-maintained and must stay the union of every other
     extra, or a `c64cast[all]` install silently misses a feature.
  2. Published dependencies must be bounded ranges, never `==`. An exact pin in
     package metadata makes us unsolvable against any environment that already
     has numpy or opencv. (`uv.lock` keeps dev/CI exact — that's the right
     place for it.)
  3. The `readme` key and the PEP 561 `py.typed` marker are both "invisible
     until published" blockers: no readme renders a blank PyPI page, and a
     py.typed that isn't in package-data ships a wheel type checkers ignore.
  4. Same for the packaged examples + JSON schema: they live under the package
     only so the wheel can carry them, and a data file no `package-data` glob
     matches is missing for every installed user while a checkout looks fine.
  5. `doctor._EXTRAS` is the table `--doctor` probes and Appendix I is printed
     from. An extra missing from it is never probed and never documented —
     which is exactly what happened to `wled`, silently, for as long as the two
     lists were kept in sync by hand alone.
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYPROJECT = os.path.join(_REPO, "pyproject.toml")


def _load() -> dict:
    with open(_PYPROJECT, "rb") as f:
        return tomllib.load(f)


def _req_name(spec: str) -> str:
    """The distribution name from a requirement string, normalized per PEP 503."""
    for sep in ("[", ">", "<", "=", "!", "~", ";", " "):
        spec = spec.split(sep, 1)[0]
    return spec.strip().replace("_", "-").replace(".", "-").lower()


class TestExtrasUnion(unittest.TestCase):
    def test_all_extra_is_the_union_of_every_other_extra(self) -> None:
        extras = _load()["project"]["optional-dependencies"]
        self.assertIn("all", extras, "the `all` extra disappeared")

        union = {req for name, reqs in extras.items() if name != "all" for req in reqs}
        got = set(extras["all"])

        # Report both directions — "which feature did `all` stop installing"
        # and "what did it pick up that no real extra asks for" are different
        # mistakes with different fixes.
        self.assertEqual(
            got,
            union,
            "the `all` extra drifted from the other extras.\n"
            f"  missing from `all`: {sorted(union - got) or 'none'}\n"
            f"  in `all` only:      {sorted(got - union) or 'none'}",
        )

    def test_all_extra_agrees_on_version_bounds(self) -> None:
        """A dependency named by two extras (fastapi, uvicorn) must carry the
        same bounds in both, or resolution depends on which extra you asked
        for."""
        extras = _load()["project"]["optional-dependencies"]
        seen: dict[str, tuple[str, str]] = {}
        for extra, reqs in extras.items():
            for req in reqs:
                name = _req_name(req)
                prev = seen.get(name)
                if prev is not None:
                    self.assertEqual(
                        prev[1],
                        req,
                        f"{name} is pinned differently in [{prev[0]}] and [{extra}]",
                    )
                seen[name] = (extra, req)


class TestDependencyBounds(unittest.TestCase):
    # yt-dlp is the one intentional exception: CalVer, network-facing, and its
    # site extractors break constantly, so it floats. See the `yt` extra.
    _UNBOUNDED_OK = {"yt-dlp"}

    def _published_requirements(self) -> list[tuple[str, str]]:
        proj = _load()["project"]
        out = [("dependencies", r) for r in proj["dependencies"]]
        for extra, reqs in proj["optional-dependencies"].items():
            out.extend((f"optional-dependencies.{extra}", r) for r in reqs)
        return out

    def test_no_exact_pins_in_published_metadata(self) -> None:
        for where, req in self._published_requirements():
            with self.subTest(req=req):
                self.assertNotIn(
                    "==",
                    req,
                    f"{where}: `{req}` is an exact pin. Published metadata takes "
                    "bounded ranges (>=known-good,<next-breaking); uv.lock is "
                    "where exact versions belong.",
                )

    def test_every_requirement_has_a_floor(self) -> None:
        for where, req in self._published_requirements():
            with self.subTest(req=req):
                self.assertIn(
                    ">=",
                    req,
                    f"{where}: `{req}` has no lower bound — a resolver may pick "
                    "a version older than anything CI has tested.",
                )

    def test_every_requirement_has_a_ceiling(self) -> None:
        for where, req in self._published_requirements():
            if _req_name(req) in self._UNBOUNDED_OK:
                continue
            with self.subTest(req=req):
                self.assertIn(
                    "<",
                    req,
                    f"{where}: `{req}` has no upper bound — the next breaking "
                    "release of it breaks every c64cast install. Add one, or "
                    "add the name to _UNBOUNDED_OK with a reason.",
                )


class TestExtrasAreProbedAndDocumented(unittest.TestCase):
    def test_doctor_knows_every_extra(self) -> None:
        from c64cast.app.doctor import _EXTRAS

        declared = set(_load()["project"]["optional-dependencies"]) - {"all"}
        known = {name for name, _module, _used_for in _EXTRAS}
        self.assertEqual(
            known,
            declared,
            "doctor._EXTRAS drifted from [project.optional-dependencies].\n"
            f"  declared but never probed: {sorted(declared - known) or 'none'}\n"
            f"  probed but not declared:   {sorted(known - declared) or 'none'}",
        )


class TestPublishedFiles(unittest.TestCase):
    def test_readme_key_points_at_a_real_file(self) -> None:
        readme = _load()["project"].get("readme")
        self.assertIsNotNone(readme, "no `readme` key — the PyPI page renders blank")
        self.assertTrue(os.path.isfile(os.path.join(_REPO, str(readme))))

    def test_license_files_point_at_real_files(self) -> None:
        for rel in _load()["project"].get("license-files", []):
            with self.subTest(rel=rel):
                self.assertTrue(os.path.isfile(os.path.join(_REPO, rel)))

    def test_py_typed_exists_and_is_packaged(self) -> None:
        self.assertTrue(
            os.path.isfile(os.path.join(_REPO, "c64cast", "py.typed")),
            "PEP 561 marker missing — consumers see an untyped package",
        )
        package_data = _load()["tool"]["setuptools"]["package-data"]["c64cast"]
        self.assertIn(
            "py.typed",
            package_data,
            "py.typed exists but isn't in package-data, so the wheel won't ship it",
        )

    def test_every_packaged_data_file_is_covered_by_a_package_data_glob(self) -> None:
        # The wheel carries only .py plus what these globs match, and nothing
        # in CI notices a missing one: `--config example:hello` keeps working
        # from the checkout and 404s for everyone who installed.
        patterns = _load()["tool"]["setuptools"]["package-data"]["c64cast"]
        pkg = os.path.join(_REPO, "c64cast")
        shipped = [
            os.path.relpath(os.path.join(root, name), pkg)
            for sub in ("examples", "data")
            for root, _dirs, files in os.walk(os.path.join(pkg, sub))
            for name in files
            if os.path.splitext(name)[1] in (".toml", ".json")
        ]
        self.assertTrue(shipped, "no packaged examples/schema found at all")
        for rel in shipped:
            with self.subTest(file=rel):
                posix = rel.replace(os.sep, "/")
                self.assertTrue(
                    any(fnmatch.fnmatch(posix, pat) for pat in patterns),
                    f"{posix} matches no package-data glob {patterns} — "
                    "it would be missing from the wheel",
                )


if __name__ == "__main__":
    unittest.main()
