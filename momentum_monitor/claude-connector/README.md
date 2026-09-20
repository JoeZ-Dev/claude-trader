# claude-connector

Built in phase 3 stage 1 (`specs.md` sections 25 and 27). The only
container with the `claude` CLI's OAuth credentials mounted in
(read-only — a bind mount of the host's `~/.claude/.credentials.json`,
the one file phase 3 stage 0's investigation proved sufficient; nothing
else from the host's `~/.claude` is needed or mounted).

Deliberately thin: no narration-trigger detection, no prompt design, no
safety-gate logic lives here — all of that is `monitor-app`'s job
(reusing state that already exists there). This service does exactly one
thing: given a prompt, run `claude -p <prompt>` as a real subprocess
(`claude_cli.py`) and report back what really happened, success or
failure, never silently.

`POST /narrate {"prompt": "..."}` → `{"ok": true, "text": "..."}` on
success, or `{"ok": false, "error": "..."}` (HTTP 502) for anything that
means this call did NOT produce real narration text: the binary missing,
a non-zero exit, a timeout, or empty output despite a zero exit.

`GET /health` → `{"status": "ok"}`.
