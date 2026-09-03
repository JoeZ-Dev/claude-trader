# Agent Protocol — claude-trader

Working discipline for any AI agent (Claude Code or otherwise) making changes
in this repository. Applies to all subprojects within this repo (the EOD
swing bot and `momentum_monitor/`) equally.

## Core workflow: RED → GREEN → REFACTOR → COMMIT

1. **RED** — write a failing test that expresses the behavior you're about
   to add, before writing the implementation.
2. **GREEN** — write the minimum code needed to make that test pass.
3. **REFACTOR** — clean up without changing behavior, tests still passing.
4. **COMMIT** — one commit per cycle. Commit messages should make the cycle
   legible (what behavior was added, not just "fix stuff").

No untested logic enters the codebase. If you find yourself writing
implementation code with no corresponding test, stop and write the test
first.

## Contract-first boundary design

Anywhere data crosses a boundary — between modules, between a container and
another container, between this codebase and an external API — define the
explicit shape of that data before writing the code on either side. Prefer
a plain, explicit structure (a dataclass, a documented dict shape, a JSON
schema) over an implicit one both sides "just happen to agree on."

When a spec document (like `specs.md`) defines a contract, treat that
document as the source of truth. Copy the exact contract into code
comments or docstrings rather than paraphrasing it — paraphrasing invites
drift between what the spec says and what the code does.

## Reproducibility

Tests must be deterministic. No test should depend on network access, wall
clock time (without explicit injection/mocking), or live external services.
If a component under test would normally hit a live API (Schwab, an LLM
provider), the test exercises it against a fake/mock, not the real thing.

## Directory boundaries

This repo currently contains more than one distinct project. Do not import
across them, do not assume shared configuration, and do not modify one
while working on the other unless explicitly instructed. As of this
writing: `strategy/`, `backtest/`, `data/`, `live/` belong to the EOD swing
bot; `momentum_monitor/` is a separate, unrelated tool. If a new subproject
is added later, it gets the same treatment — its own directory, its own
boundary, no silent sharing.

## Scope discipline

Work only within the scope explicitly given for the current session. If a
task spec defines phases, do not begin a later phase because "it seemed
natural to keep going" — stop at the defined boundary and report back.

## When something is ambiguous

Do not silently resolve a structural ambiguity by picking whatever seems
reasonable and proceeding. State what's ambiguous, state your proposed
resolution, and wait for confirmation before building on top of that
decision. A short pause to ask is cheaper than an afternoon spent unwinding
work built on a wrong guess.

## Credentials

Never create, request, hardcode, or commit API keys, tokens, or secrets.
Never write to any file or directory outside this repository's own working
tree (enforced separately via `.claude/settings.json`, but the principle
holds regardless of what any specific permission configuration allows).
