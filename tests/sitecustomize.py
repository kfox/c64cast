"""Arm the test suite's filesystem sandbox at interpreter startup.

`site` imports a module named exactly `sitecustomize` if it can find one on
`sys.path`, which is the only hook that runs early enough to catch a file
access from a test module's own import, and the only one that reaches
`unittest_parallel`'s worker processes — they inherit `PYTHONPATH`, not the
parent's audit hooks. Hence the fixed, unlovely name and the placement in
`tests/`: every entry point already runs with `PYTHONPATH=tests`, because
that is also how the modules import `_fakes`.

Nothing in the package imports this. A production install never has `tests`
on its path, so it is not importable there at all.
"""

import _fs_sandbox

_fs_sandbox.redirect_local_state()
_fs_sandbox.neutralize_local_chargen()
_fs_sandbox.arm()
