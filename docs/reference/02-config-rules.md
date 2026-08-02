---
number: 1
---

# The Configuration Language

> [!NOTE]
> This chapter is an outline. Its prose lands with the chapters 1 to 3 change;
> the tables it will refer to are already complete in Appendix A.

## Files and Where They Are Found

Which file is read, in what order, and what `--config example:NAME` resolves to.

## The Precedence Ladder

Each layer of the ladder stated in the introduction, worked through: what it is
for, what belongs in it, and how to see which layer supplied a given value.

## Machine Settings

`~/.config/c64cast/settings.toml`: what belongs there, what is refused, and
what `--save-settings` writes.

## Scenes and Playlists

`[[scenes]]`, `[playlist]`, and how a run is assembled from them.

## The Ensemble Cascade

Master and per-system files, and the one extra layer an ensemble inserts.

## Validation

What is checked when, what an unknown key does, and what `--doctor` reports.
