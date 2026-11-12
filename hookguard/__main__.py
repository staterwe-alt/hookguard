"""Executable entrypoint for python -m hookguard."""

import sys
from hookguard.cli import main

if __name__ == "__main__":
    sys.exit(main())
