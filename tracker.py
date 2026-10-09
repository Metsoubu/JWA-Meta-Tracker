"""JWA Meta Tracker - run `python tracker.py --help` for commands.

Most people only need START.bat (dashboard) and UPDATE_NOW.bat (manual update).
"""
import sys

if sys.version_info < (3, 10):
    sys.stderr.write("JWA Meta Tracker needs Python 3.10 or newer. Please install it from python.org.\n")
    sys.exit(3)

from jwa_tracker.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
