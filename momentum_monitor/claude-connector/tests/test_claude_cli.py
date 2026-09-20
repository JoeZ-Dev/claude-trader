import asyncio
import os
import sys
import time

_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _APP_DIR)

import pytest

from claude_cli import ClaudeInvocationError, run_claude_prompt

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_SUCCESS = os.path.join(_FIXTURES, "fake_claude_success.sh")
_FAILURE = os.path.join(_FIXTURES, "fake_claude_failure.sh")
_SLOW = os.path.join(_FIXTURES, "fake_claude_slow.sh")
_EMPTY = os.path.join(_FIXTURES, "fake_claude_empty.sh")


def test_run_claude_prompt_returns_real_stdout_on_success():
    text = asyncio.run(run_claude_prompt("hello world", binary=_SUCCESS))
    # the fake script echoes back argv[2] (the prompt, since argv[1] is
    # always "-p") -- proves the real prompt actually reached the real
    # subprocess, not a canned response.
    assert text == "FAKE_CLAUDE_OK: hello world"


def test_run_claude_prompt_raises_on_non_zero_exit_with_stderr_in_the_message():
    with pytest.raises(ClaudeInvocationError) as exc_info:
        asyncio.run(run_claude_prompt("anything", binary=_FAILURE))
    assert "authentication failed" in str(exc_info.value)


def test_run_claude_prompt_raises_on_timeout_and_kills_the_process():
    with pytest.raises(ClaudeInvocationError) as exc_info:
        asyncio.run(run_claude_prompt("anything", binary=_SLOW, timeout_seconds=0.3))
    assert "timed out" in str(exc_info.value).lower()


def test_run_claude_prompt_timeout_returns_promptly_not_after_the_full_sleep():
    # Found live while building this: the fake script's `sleep 30`
    # (spawned as a child of the "claude" shell script itself) inherits
    # the stdout/stderr pipes. Killing only the direct process leaves
    # that grandchild holding the pipes open, which hangs proc.wait()
    # for the FULL 30s regardless of the kill -- defeating the timeout
    # entirely. Fixed via a new process group + group kill; this test
    # locks that fix in by asserting the call actually returns close to
    # timeout_seconds, not ~30s later.
    start = time.time()
    with pytest.raises(ClaudeInvocationError):
        asyncio.run(run_claude_prompt("anything", binary=_SLOW, timeout_seconds=0.3))
    elapsed = time.time() - start
    assert elapsed < 5.0, f"took {elapsed:.1f}s -- the process-group kill fix regressed"


def test_run_claude_prompt_raises_on_empty_output_even_with_exit_zero():
    # exit 0 alone is not "success" -- no real narration text came back.
    with pytest.raises(ClaudeInvocationError) as exc_info:
        asyncio.run(run_claude_prompt("anything", binary=_EMPTY))
    assert "empty" in str(exc_info.value).lower()


def test_run_claude_prompt_raises_clearly_when_binary_does_not_exist():
    with pytest.raises(ClaudeInvocationError) as exc_info:
        asyncio.run(run_claude_prompt("anything", binary="/no/such/claude/binary"))
    assert "not found" in str(exc_info.value).lower()


def test_run_claude_prompt_passes_the_prompt_as_the_dash_p_argument():
    # Regression guard: -p must be a real, separate argv element (not
    # concatenated into one string), matching the CLI's actual
    # non-interactive invocation form proven in the stage-0 investigation.
    text = asyncio.run(run_claude_prompt("a distinctive test phrase", binary=_SUCCESS))
    assert "a distinctive test phrase" in text
