#!/usr/bin/env python3
"""Compatibility entry point for the public setup workflow."""

from __future__ import annotations

import sys
from pathlib import Path


SETUP_SCRIPTS = Path(__file__).resolve().parents[2] / "setup" / "scripts"
sys.path.insert(0, str(SETUP_SCRIPTS))

from bootstrap_repo import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
