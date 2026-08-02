"""Launcher entry point for packaging winSpark as an executable.

Equivalent to `python -m winspark.ui`, but a plain script PyInstaller can point
at. Build with:  pyinstaller winspark.spec
"""

import sys

from winspark.ui.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
