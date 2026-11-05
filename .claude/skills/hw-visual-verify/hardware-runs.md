# Hardware during a review or a ship run

The suite fakes every backend, so prove a finding there first. When the
caller's prompt says hardware use is authorized for this run, an agent can use
the rig without asking again to settle what the fakes cannot: a register
restore, a teardown, what the VIC draws, a timing claim. The prompt names the
target and whether a capture device is attached. The rules:

- **Take the lock.** Run every command that touches the machine, the capture
  device or the audio input under `scripts/diags/hw_lock.py`, as the outermost
  command. Keep each locked command short.
- **Use the committed tools.** `scripts/diags/u64_probe.py`, `hdmi_capture.py`
  and `run_and_capture.py` cover most needs, and the
  [`hw-visual-verify`](SKILL.md) skill covers
  captures. Improve those tools rather than writing throwaway scripts.
- **Keep the target out of the repo.** It comes from the prompt. Never write a
  host, IP address or device name into a committed file.
- **Run the way users do.** Audio is on by default, and so is the rig run:
  don't pass `--no-audio`. Several defects only appear when audio shares the
  DMA link with render. Whoever is near the speakers mutes them at their end.
- **Prefer a run that ends on its own** over a background run you have to stop.
  `run_and_capture.py` resets the machine on exit. Never kill a run mid-DMA:
  that can wedge the U64 until it is power-cycled. Stop a run in the same step
  as the observation it was for.
- **No REST polling during a stream.** Don't poll the U64's REST API in a loop
  while c64cast is streaming to it.
- **Leave the machine silent and reset.** A clean c64cast exit already resets
  it. After anything else, run `scripts/diags/u64_probe.py --reset-only --url
  http://HOST` and check that it printed an HTTP status, not `FAILED`.
- **Hand back what needs a person.** A check that needs someone's own eyes or
  ears, such as judging audio or a monitor with no capture, goes back to the
  caller as an open question.

Without that authorization, a claim only hardware can settle goes back to the
caller as an open question.
