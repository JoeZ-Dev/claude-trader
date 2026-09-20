"""
claude_cli.py -- the actual `claude -p` shell-out, in its own thin
module (mirrors schwab-connector's stream.py/price_history.py split: the
external-process-spawning seam kept separate from app.py's route wiring,
so tests exercise the real subprocess path against tiny fake `claude`
scripts, never a mock of asyncio internals).

Proven viable in the phase-3 stage-0 investigation (specs.md section 25):
`claude -p "<prompt>"` works non-interactively from a fresh, unattended
container with only the host's OAuth credentials file bind-mounted
read-only -- no interactive login inside the container. That investigation
also flagged, as a known and explicitly ACCEPTED risk (not solved here),
that access-token refresh under this mount was never exercised: this
module's job is to make sure that if refresh ever silently fails, the
resulting `claude -p` failure (non-zero exit, hang, or garbage output)
is caught here and raised as a clear, typed error -- never swallowed,
never misread as success.
"""
from __future__ import annotations

import asyncio
import os
import signal


class ClaudeInvocationError(Exception):
    """Raised by run_claude_prompt for anything that means this call did
    NOT produce real narration text: the binary doesn't exist, a
    non-zero exit (an auth/refresh failure would show up here), a
    timeout (the process is killed, never left running), or empty
    output despite a zero exit. Callers (claude-connector's own /narrate
    route, and monitor-app's narration gate) must catch this and surface
    it clearly -- never silently swallow it."""


async def run_claude_prompt(prompt: str, *, binary: str = "claude",
                            timeout_seconds: float = 30.0) -> str:
    """Runs `<binary> -p <prompt>` as a real subprocess (asyncio, never
    blocking the event loop -- same "don't block on a slow external
    call" discipline schwab-connector's reconnect.py fix already
    established) and returns its real stdout, stripped.

    Raises ClaudeInvocationError (never lets an OSError/TimeoutError
    leak past this function) for: the binary not existing, a non-zero
    exit (stderr included in the message), a timeout (the process is
    killed and awaited before raising, never left as a zombie), or a
    zero exit with empty stdout (a real, if unusual, malformed-output
    case -- exit 0 alone is not proof of real narration text)."""
    try:
        # start_new_session=True (its own process group) is not just
        # hygiene -- found live while testing the timeout path itself
        # (a fake `claude` that spawns a real child, e.g. a shell
        # script's own `sleep`): killing only the direct child leaves a
        # grandchild holding the stdout/stderr pipes open, which hangs
        # proc.wait() below for the FULL original duration regardless of
        # the kill, defeating the timeout entirely. `claude` itself may
        # similarly spawn helper subprocesses, so this is a real
        # correctness fix, not a test-only workaround.
        proc = await asyncio.create_subprocess_exec(
            binary, "-p", prompt,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ClaudeInvocationError(f"claude binary not found ({binary!r}): {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            proc.kill()  # process (or its whole group) already gone
        await proc.wait()
        raise ClaudeInvocationError(f"claude -p timed out after {timeout_seconds}s")

    if proc.returncode != 0:
        raise ClaudeInvocationError(
            f"claude -p exited {proc.returncode}: {stderr.decode(errors='replace').strip()}")

    text = stdout.decode(errors="replace").strip()
    if not text:
        raise ClaudeInvocationError("claude -p produced empty output")
    return text
