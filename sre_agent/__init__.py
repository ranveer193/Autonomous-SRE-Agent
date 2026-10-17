import sys

# Windows consoles default to cp1252 and crash on emoji / arrows in log lines. Fix once for every entry point.
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
