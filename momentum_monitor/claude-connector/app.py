"""
claude-connector -- the only container with the `claude` CLI's OAuth
credentials mounted in (specs.md sections 5 and 25's stage-0
investigation). Its ONE job is to be the isolated, credentialed shell-out
executor for `claude -p`: monitor-app holds no Claude credentials at all
and never invokes the binary directly, the same credential-isolation
discipline schwab-connector already established for Schwab auth
(specs.md section 5: "monitor-app ... Holds no credentials").

Deliberately thin: no narration-trigger detection, no prompt design, no
safety-gate logic lives here -- all of that is monitor-app's job (specs.md
section 27), reusing detection/state that already exists there. This
service does exactly one thing: given a prompt, run `claude -p <prompt>`
and report back what really happened, success or failure, never silently.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from claude_cli import ClaudeInvocationError, run_claude_prompt

DEFAULT_TIMEOUT_SECONDS = 30.0


class NarrateRequest(BaseModel):
    prompt: str


def create_app(*, binary: str = "claude",
               timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
               run_prompt_fn=run_claude_prompt) -> FastAPI:
    """`run_prompt_fn` is injectable (defaults to the real claude_cli.
    run_claude_prompt) purely for test seams that want to substitute a
    fake async callable directly -- every test in this project's suite
    instead points `binary` at a real, tiny fake `claude` shell script
    and exercises the real subprocess path, per this project's "test
    real behavior, not mocks" precedent; this parameter exists for
    parity with every other injectable-dependency seam in this codebase
    (fetch_bars, announce_watch, ...), not because any current test uses
    it directly."""
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    app = FastAPI(title="claude-connector", lifespan=lifespan)

    @app.post("/narrate")
    async def narrate(req: NarrateRequest):
        # Never lets ClaudeInvocationError (or anything else) become an
        # unhandled 500 -- a caller (monitor-app's narration gate) must
        # always get back clean, parseable JSON describing exactly what
        # happened, per specs.md section 27's "any invocation failure ...
        # must be caught and surfaced clearly, never silently swallowed."
        try:
            text = await run_prompt_fn(req.prompt, binary=binary,
                                       timeout_seconds=timeout_seconds)
        except ClaudeInvocationError as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=502)
        return {"ok": True, "text": text}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    return app
