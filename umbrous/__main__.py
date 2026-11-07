"""Allows `python3 -m umbrous ...`. bin/umbrous uses this entry point."""

import sys

from umbrous.cli import main

sys.exit(main())
