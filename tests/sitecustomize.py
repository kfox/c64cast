"""Arm the test suite's three startup sandboxes — filesystem, background
thread and per-test timeout — at interpreter startup.

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
import _thread_sandbox
import _timeout_sandbox

_fs_sandbox.redirect_local_state()
_fs_sandbox.neutralize_local_chargen()
_fs_sandbox.arm()
_thread_sandbox.arm()
# Last, so its wrapper is the outermost one and the cap covers the test's
# cleanups — where `_thread_sandbox`'s stray check runs, and where a join that
# never returns is as good a hang as one inside the test body.
_timeout_sandbox.arm()
