# Appliance-image packaging

Non-Python files installed onto the Orange Pi Zero 2W OS image (see
[docs/architecture.md](../docs/architecture.md) → "the image" — the build
script that chroot-installs these is a separate, not-yet-landed change). They
are inert here: nothing in the Python package reads this directory, and
`make check` never touches it.

- **`systemd/`** — a service + timer pair that runs
  `c64cast --check-for-updates --write-state` once a day, so the console's
  update banner and `motd/`'s login line have something to read without
  either of them ever querying PyPI. Never installs anything — see
  `c64cast --upgrade` for the one command that does, and it always asks
  first. The unit has to run as the same user as `c64cast.service`, since
  what it writes and what the console reads are both `<data root>` files
  resolved from that account's environment.
- **`motd/`** — an `/etc/update-motd.d/` script that prints what the timer
  above last recorded, via `c64cast --motd-line`
  (`c64cast/app/update_state.py`): the pending upgrade, or — for a box that
  has not reached PyPI in a month — that it cannot say whether what it runs
  is current. Reads a local file only; never touches the
  network itself, so it can never hang an SSH login on a slow or absent
  connection.
