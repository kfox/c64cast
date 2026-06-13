#!/usr/bin/env python
"""Compatibility entry point. The implementation now lives in c64cast/.

Prefer:    python -m c64cast [args...]
"""
import sys

from c64cast.cli import main

sys.exit(main())
