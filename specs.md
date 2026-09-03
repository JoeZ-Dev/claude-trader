# claude-trader — Specifications

This document is the canonical source of truth for design decisions in this
repository. Where a prompt or conversation describes something differently
than this document, this document wins — update it rather than letting
drift accumulate between what was said and what's written down.

Sections are added per subproject as they're built. The EOD swing bot
predates this document and is documented in its own `README.md` for now.

---

## momentum_monitor/ — Real-time discretionary trading support tool

### 1. Purpose

Not an autonomous trading system. A tool that watches a small number of
candidate stocks in real time and surfaces an objective technical read —
level quality, hold-confirmation state, volume confirmation — as a
countermeasure to premature, emotionally-driven entries. The user makes
every trading decision; this tool's job is giving them a calmer, more
consistent read of the chart than they can reliably produce themselves
under time pressure.

### 2. Non-goals (explicit, not implicit)

- Does not place, arm, or manage real or paper orders through any broker.
- Does not autonomously decide which stocks to watch — the user adds
  candidates manually.
- Does not treat catalyst/news credibility as a hard gate. Signal strength
  is primary; catalyst quality and market backdrop are contextual
  modifiers with sector-dependent weight, not pass/fail filters. See
  design rationale in `momentum_monitor/core/` docstrings.

### 3. Core analysis logic — `momentum_monitor/core/` (monitor_core)

Pure, framework-free Python. No I/O, no network, no UI dependency. This
boundary exists deliberately: analysis logic tangled into a UI/network
layer (as happened in the ToS_Companion reference project) becomes
untestable and untrustworthy. It does not happen here.

Two non-negotiable principles:
- Hold-confirmation (requiring N consecutive bar closes on the correct
  side of a level before treating a break as real) applies to ENTRY
  evaluation only. Stop-loss evaluation must remain immediate and
  unconditional — this asymmetry is intentional, not an oversight.
- Level strength scoring exposes its components (touch count, volume
  concentration, round-number proximity) rather than collapsing them into
  one opaque number. Any change to scoring must preserve this visibility.

Covered by unit tests in `momentum_monitor/core/tests/` — see that
directory for the current, authoritative test suite.

### 4. Data source

Charles Schwab's Market Data API — streaming quotes, aggregated into 10s
bars, via the `schwab-py` library (chosen over hand-rolling OAuth/streaming:
schwab-py's own documented rationale explicitly warns against rolling your
own OAuth callback flow as "unnecessarily complex and dangerous," which
matches this project's own empirical experience — the hand-rolled
equivalent in the reference project had failing auth-refresh and
stream-reconnect tests). Deliberately scoped to a SEPARATE Schwab developer
app registration from any other project in this environment, requesting
ONLY "Market Data Production" — explicitly not "Accounts and Trading
Production". The resulting credential must be structurally incapable of
placing orders or reading account/position data, not merely unused for
those purposes.

**Token lifetime constraint (platform-enforced, not a code quality issue):**
Schwab refresh tokens are valid for 7 days, after which a fresh interactive
login is required regardless of client implementation. "Survives a
container restart" is the correct, achievable, testable claim. "Never
requires re-authentication" is not achievable by any client and must not
be implied by definition-of-done language.

Bar shape (the contract between `schwab-connector` and everything else):
```
{
  "ts": int,          # unix seconds
  "open": float,
  "high": float,
  "low": float,
  "close": float,
  "volume": float,
  "is_extended": bool # true for premarket/after-hours bars
}
```

### 5. Container architecture

Three containers, one per credential boundary, via docker-compose. All
directories below live under `momentum_monitor/` at the repo root — this
subproject is one directory, per AGENT_PROTOCOL.md's directory-boundary
principle, with no code living loose at repo root:

- **`momentum_monitor/core/`** — the analysis logic from section 3.
  (If found instead at repo-root `monitor_core/`, that's stale from an
  earlier extraction step and should be moved here, not imported
  across from its old location.)
- **`momentum_monitor/schwab-connector/`** — the only container holding
  the Schwab OAuth token. Owns the stream subscription and bar
  aggregation. Bars persist to an append-only JSONL store (chosen over
  the originally-specified SQLite table for simplicity of an
  append-mostly, single-symbol log; includes non-monotonic-bar dedup so
  a container restart doesn't duplicate entries). Internal API only
  (not published to host):
  - `POST /watch {"symbol": "..."}`
  - `GET /bars/{symbol}?since_ts={unix_seconds}` → array of bar objects
    per the shape in section 4.
  - `GET /health` → `{"status": "ok", "watching": [...], "connected": bool}`
- **`momentum_monitor/claude-connector/`** — the only container with the
  `claude` CLI's auth mounted in. Shells out to `claude -p` for
  event-triggered narration. Not built until phase 3 (see roadmap below)
  — currently a placeholder directory with a README only.
- **`momentum_monitor/monitor-app/`** — the FastAPI web app. Holds no
  credentials. Polls `schwab-connector` for bars, runs them through
  `momentum_monitor/core/`, serves a web view. The only container with a
  port published to the host (`8012`).
- **`momentum_monitor/docker-compose.yml`** — orchestrates all three.

### 6. Roadmap / phases

1. **(current)** One symbol, live Schwab data through the tested core,
   a basic web page showing correct numbers. No trades, no multi-symbol,
   no LLM.
2. Multi-symbol (4-6 concurrent), same architecture extended.
3. Event-triggered LLM narration via `claude-connector`, firing only on
   meaningful state changes (level hold-confirmed, volume threshold
   crossed, MACD cross, retest, sharp reversal) — never polled.
3.5. **Multi-scenario setup evaluation.** Rather than tracking only the
   nearest above/below levels (phase 1's simpler version), evaluate
   several distinct candidate setup TYPES in parallel — e.g. a
   resistance breakout, a shorter-term micro-breakout, a VWAP
   pullback-reclaim, and a round-number reclaim (the last of which can
   be watched even before price has actually tested it — untested
   round numbers are still psychologically real levels, unlike swing
   levels which require an actual prior touch). This is a direct
   evolution of ToS_Companion's `candidate_generator.py` three-setup-type
   design, rebuilt on `monitor_core`'s corrected level detection instead
   of its buggy nearest-price picking. Surface whichever candidate is
   closest to a real setup, but per the "no collapsed grades" principle
   established for the LLM Coach: show the factors that make it the
   best candidate (proximity, level strength, volume confirmation) —
   don't reduce the comparison to an opaque score.
4. Virtual trade journal — logs what the system would have done
   (entry/stop/target) without placing anything, for end-of-day review
   against the user's own judgment.
5. Anything beyond this point (more autonomy, live execution) requires
   its own explicit design discussion and is not assumed by this roadmap.

Do not build ahead of the current phase without an explicit instruction
to move to the next one.
