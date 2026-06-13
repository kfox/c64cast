# assets/programs/

Native C64 programs for the `launcher` scene. The Ultimate 64 loads and runs
them directly via its own firmware runners, then the program owns the whole
machine (VIC, SID, CIAs) — c64cast stops painting and only watches for player
input to keep the scene alive.

Two file types, selected automatically by extension:

| Extension | Firmware runner            | Notes                                        |
|-----------|----------------------------|----------------------------------------------|
| `.prg`    | `POST /v1/runners:run_prg` | Loads to its load address and RUNs it.       |
| `.crt`    | `POST /v1/runners:run_crt` | Resets the machine with the cartridge active.|

## Idle timeout vs. player input

The launcher scene's `duration_s` is an **idle timeout**, not a fixed runtime.
It counts down from launch and resets whenever a player provides input, so a
game stays up while someone is playing and advances once the controls go quiet.
For a self-running demo, set a long `duration_s` (and optionally `input_source =
"none"`) so it plays for the whole window. See the `[[scenes]]` launcher block in
[c64cast.example.toml](../../config/c64cast.example.toml) and
[config/examples/scene-launcher.toml](../../config/examples/scene-launcher.toml).

Input is read off the hardware and deliberately excludes the modifier keys
c64cast itself scans (Commodore / SHIFT / CTRL — those drive pause/skip/style).
`input_source` picks what counts: `cia` (joystick bits at `$DC00/$DC01`, the
default), `kernal` (`$00C5/$00C6`, only live while the kernal IRQ runs), `auto`
(both), or `none`.

## Limitations

- **Single program only.** No disk (`.d64`/`.d81`), tape (`.t64`/`.tap`), or
  multi-disk games — there's no disk mounting or disk-swap. Use a single-file
  `.prg` or `.crt`.
- **Idle detection is best-effort.** A program that runs its own keyboard-matrix
  scan drives `$DC00` as output, so CIA reads can race it (false "activity" → the
  scene may not advance until `max_duration_s`, or a keypress may be missed).
  `kernal` mode only works while the program leaves the kernal IRQ intact.
- Cartridge-type support depends on the U64 firmware. PAL/NTSC is not switched.
  The local preview/recording windows show nothing (no host-side pixel writes).

## Sources

- **CSDb**: https://csdb.dk/ — demoscene releases, many shipped as `.prg`/`.crt`.
- **GameBase64** and other archives — homebrew and freely distributable games.

## Licensing caveat

Most commercial C64 software is still under copyright. These files are tracked in
`.gitignore`; confirm redistribution rights before bundling any program with a
config you share.
