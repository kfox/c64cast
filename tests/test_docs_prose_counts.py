"""Guards the counts the books state in prose against the live registries.

The appendices cannot drift, because they are generated. The chapters that
introduce them can: "There are ten kinds of scene and thirteen overlays" is
typed by hand, and adding an overlay makes it quietly wrong in a printed book
while every generated table stays right.

Each claim below is the sentence as it is written, with the number spelled out
as the prose spells it. The test fails two ways on purpose: when the number no
longer matches the registry, and when the sentence is gone altogether — a
rewrite that drops the claim should have to say so here rather than silently
retire the guard.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from c64cast.app import introspect
from c64cast.scenes import effects, generators

_DOCS = Path(__file__).resolve().parent.parent / "docs"

_NUMBER_WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen", "twenty", "twenty-one",
    "twenty-two", "twenty-three", "twenty-four", "twenty-five", "twenty-six",
    "twenty-seven", "twenty-eight", "twenty-nine", "thirty",
]  # fmt: skip


def _counts() -> dict[str, int]:
    return {
        "scenes": len(introspect.scene_types()),
        "overlays": len(introspect.overlay_docs()),
        "modes": len(introspect.display_modes()),
        "generators": len(generators.REGISTRY),
        "effects": len(effects.REGISTRY),
    }


# (book-relative path, the sentence, with {registry} where a number is spelled).
_CLAIMS: tuple[tuple[str, str], ...] = (
    (
        "reference/03-vocabulary.md",
        "There are {scenes} kinds of scene and {overlays} overlays.",
    ),
    (
        "reference/03-vocabulary.md",
        "The {generators} sources and {effects} effects are Appendix E",
    ),
    (
        "reference/04-display-pipeline.md",
        "draws its frames from one of {generators} procedural sources.",
    ),
    (
        "reference/04-display-pipeline.md",
        "The {effects} effects and their parameters are Appendix E.",
    ),
    ("reference/04-display-pipeline.md", "## The {modes} Display Modes"),
    ("guide/07-overlays-color.md", "## The {modes} Display Modes"),
)


def _normalize(text: str) -> str:
    """Collapse whitespace, so a claim still matches after a re-wrap."""
    return re.sub(r"\s+", " ", text)


class ProseCountsTest(unittest.TestCase):
    def test_spelled_counts_match_the_registries(self):
        counts = _counts()
        words = {
            key: _NUMBER_WORDS[n] if n < len(_NUMBER_WORDS) else str(n) for key, n in counts.items()
        }
        for rel, template in _CLAIMS:
            with self.subTest(claim=f"{rel}: {template}"):
                path = _DOCS / rel
                haystack = _normalize(path.read_text(encoding="utf-8"))
                # Title Case in a heading, lower case in a sentence.
                expected = template.format(**words)
                if template.startswith("## "):
                    expected = template.format(**{k: v.capitalize() for k, v in words.items()})
                # Not assertIn: its failure message prints the haystack, and
                # the haystack is an entire chapter.
                if _normalize(expected) not in haystack:
                    self.fail(
                        f"{rel} does not contain:\n"
                        f"    {expected}\n"
                        f"Registries now: {counts}. Either a count changed and "
                        f"the prose needs the new number spelled out, or the "
                        f"sentence was rewritten and this claim needs updating "
                        f"or removing."
                    )


if __name__ == "__main__":
    unittest.main()
