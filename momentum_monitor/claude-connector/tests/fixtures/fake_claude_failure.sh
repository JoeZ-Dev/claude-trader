#!/bin/sh
# Simulates a real `claude -p` failure -- non-zero exit with a real
# stderr message (e.g. an auth/refresh problem, specs.md's known,
# accepted, untested OAuth-refresh risk).
echo "error: authentication failed (token refresh unsuccessful)" >&2
exit 1
