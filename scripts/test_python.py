#!/usr/bin/env python3
"""Run isolated unit suites without touching the active desktop."""

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    suites = [ROOT / "computer-use-linux/scripts"]
    suites += sorted((ROOT / "contrib").iterdir())
    suites += [ROOT / "scripts"]
    for suite in suites:
        tests = suite / "tests"
        if not tests.is_dir():
            continue
        env = os.environ.copy()
        env["PYTHONPATH"] = str(suite / "src") if (suite / "src").is_dir() else str(suite)
        subprocess.run(
            [sys.executable, "-m", "unittest", "discover", "-s", str(tests), "-p", "test_*.py"],
            cwd=ROOT, env=env, check=True,
        )


if __name__ == "__main__":
    main()
