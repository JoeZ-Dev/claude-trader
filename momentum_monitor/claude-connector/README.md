# claude-connector (placeholder — phase 3)

Not built yet. Per `specs.md` section 5 and the phase roadmap, this will be
the only container with the `claude` CLI's auth mounted in, shelling out to
`claude -p` for **event-triggered** narration — firing only on meaningful
state changes (level hold-confirmed, volume threshold crossed, MACD cross,
retest, sharp reversal), never on a poll.

Phase 1 deliberately contains no LLM calls of any kind. This directory
exists now only to hold the boundary.
