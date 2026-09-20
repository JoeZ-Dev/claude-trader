"""
Production wiring for claude-connector (specs.md section 27). The only
container with the `claude` CLI's OAuth credentials mounted in (a
read-only bind mount of the host's ~/.claude/.credentials.json -- the
minimal file proven sufficient in the stage-0 investigation, specs.md
section 25; nothing else from the host's ~/.claude is needed or mounted).

Environment:
  CLAUDE_BINARY_PATH     path to the `claude` executable inside this
                        container                    (default "claude",
                                                       resolved via PATH)
  CLAUDE_TIMEOUT_SECONDS  how long a single `claude -p` call is allowed
                        to run before being killed and reported as a
                        timeout failure                    (default 30)

Run:  uvicorn main:app --host 0.0.0.0 --port 7879
"""
from __future__ import annotations

import os

from app import create_app

CLAUDE_BINARY_PATH = os.environ.get("CLAUDE_BINARY_PATH", "claude")
CLAUDE_TIMEOUT_SECONDS = float(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "30"))

app = create_app(binary=CLAUDE_BINARY_PATH, timeout_seconds=CLAUDE_TIMEOUT_SECONDS)
