# Moved

The example configs now live in [`c64cast/examples/`](../../c64cast/examples/)
— inside the package, so they ship with an install and are addressed by name
rather than by path:

```bash
c64cast --list-examples                  # every demo + a one-line summary
c64cast --config example:hello           # run one
c64cast --print-example hello > my.toml  # copy one out to edit
```
