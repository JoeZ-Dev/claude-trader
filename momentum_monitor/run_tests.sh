#!/usr/bin/env bash
# Run every momentum_monitor test suite.
#
# Each service is its own container with a flat module layout (schwab-connector
# and monitor-app both have an `app.py` / `main.py` / `tests/test_app.py`), and
# the directory names are hyphenated so they cannot be Python packages. Running
# them in one pytest process therefore collides on module basenames. So each
# suite runs as its own pytest invocation, which also mirrors how the
# containers are built and tested independently.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-$HERE/.venv/bin/python}"

fail=0
for suite in core schwab-connector monitor-app; do
    echo "=== $suite ==="
    "$PY" -m pytest "$HERE/$suite/tests/" -q || fail=1
done

exit "$fail"
