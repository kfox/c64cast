"""The cut a transport splice takes on an audio sink.

A splice (seek, A/B loop wrap, resume) retires the audio pushed before it and
anchors the picture where the first audio pushed after it is heard. Both
sinks take that cut in two steps: ``cut()``, which bumps the sink's flush
epoch and reads the anchor, and ``flush(cut=...)``, which does the rest
(the sampler's ring rewrite, the DAC's pause stomp). The video source takes
the cut under the same lock that sets its pending seek, so every push made
after the seek is applied carries the new epoch, however soon the demux
thread gets there; see
docs/architecture/audio.md#cut--flush-silence_outputfalse-cutnone--transport-resync.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FlushCut:
    """``epoch`` is the flush epoch the cut started, or None when the sink
    had nothing to cut (a sampler not yet gated, a DAC under its REU pump).
    ``anchor_s`` is the sink's ``position_seconds()`` at which the first
    post-cut sample is heard. ``ring_pos`` is the sampler's ring byte that
    sample is written at, and 0 on the DAC."""

    epoch: int | None
    anchor_s: float
    ring_pos: int = 0
