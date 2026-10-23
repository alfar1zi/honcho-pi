#!/usr/bin/env python3
"""Allow `python -m omp_memory`."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
