#!/usr/bin/env python3
"""Entry point for the app bundle and for local runs."""
import os, sys

# A --windowed bundle has no stdio at all.  Give anything that writes to it a
# sink so a stray print in this or any library cannot take the app down.
class _Null:
    def write(self, *_a):
        return 0

    def flush(self):
        pass

    def isatty(self):
        return False


if sys.stdout is None:
    sys.stdout = _Null()
if sys.stderr is None:
    sys.stderr = _Null()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.server import serve

if __name__ == "__main__":
    # A native window by default; --browser keeps the old behaviour, and
    # --no-browser starts the server alone, which is what the tests drive.
    port, browser, native = 0, True, True
    for a in sys.argv[1:]:
        if a == "--no-browser":
            browser, native = False, False
        elif a == "--browser":
            native, browser = False, True
        elif a == "--native":
            native = True
        elif a.isdigit():
            port = int(a)
    serve(port=port, open_browser=browser, native=native)
