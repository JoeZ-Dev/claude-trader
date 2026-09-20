#!/bin/sh
# Fake `claude` binary for tests: ignores its arguments (a real `claude -p
# "<prompt>"` invocation) and prints a fixed, real, verifiable line to
# stdout, exit 0 -- exercises the REAL asyncio subprocess code path in
# claude_cli.py end to end, no mocking of asyncio internals.
echo "FAKE_CLAUDE_OK: $2"
