"""Точка входа: `python -m asiko [проект.asiko] [--demo] [--self-test [папка]]`."""
from __future__ import annotations

import sys


def main() -> int:
    argv = list(sys.argv)
    if "--self-test" in argv:
        from .selftest import main as selftest_main

        i = argv.index("--self-test")
        out = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("-") else None
        return selftest_main(out)
    if "--version" in argv:
        from . import __version__

        print(f"ASIKO {__version__}")
        return 0
    from .ui.app import run_gui

    return run_gui(argv)


if __name__ == "__main__":
    sys.exit(main())
