#!/bin/sh
# Simulates a hung/unresponsive `claude -p` call -- must be killed by
# claude_cli.py's own timeout, never left to hang the caller forever.
sleep 30
echo "should never be reached"
