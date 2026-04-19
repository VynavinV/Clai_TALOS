#!/usr/bin/env python3
"""Compatibility wrapper for the moved recalc script."""

from pathlib import Path
import runpy


def main() -> None:
    target = Path(__file__).resolve().parent.parent / "src" / "scripts" / "recalc.py"
    runpy.run_path(str(target), run_name="__main__")


if __name__ == "__main__":
    main()
    