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

**Backfill vs. live bar width (known, intentional, temporary tradeoff):**
Every function in this module except `session_vwap` implicitly assumes
uniform bar width — `ema`/`macd`'s decay factor is applied per bar, not
per elapsed second; `relative_volume` compares one bar's volume to a
rolling average of others'; `detect_levels`'s swing-point window and
`evaluate_hold`'s `required_bars` both count bars, not time. That
assumption held by construction through the first backfill implementation
(section 5), the whole live bar series came from one 10s aggregator.
Backfill breaks it: `schwab-connector/price_history.py` prepends Schwab
price-history candles, no finer than 1 minute and with zero-volume
minutes skipped entirely (gaps observed live, backfilling QCLS on
2026-09-16, ranging from 60s to over 900s) — genuinely irregular, not
just "coarser than 10s." A "9-period EMA" or "3-bar hold" computed across
that boundary means a different amount of real time depending on which
bars happen to be in the window, which is a real correctness bug, not
cosmetic.

The fix adopted for now, confined to `monitor-app/state.py`
(`live_cadence_tail`): `ema`, `macd`, `relative_volume`, and
hold-confirmation only ever see the contiguous LIVE tail of the bar
list — found by walking backward from the most recent bar and stopping at
the first gap wider than 15s (comfortably between live's 10s cadence and
backfill's 60s floor) — never the backfilled bars ahead of it.
`session_vwap` (genuinely granularity-agnostic — a cumulative sum, not a
window) and `detect_levels` (a whole-session swing scan, not a
decay-weighted average — the softer, more forgivable case of this same
assumption) deliberately keep seeing the full backfilled+live series.
Practical effect: EMA/MACD/relative_volume/hold-confirmation warm up from
scratch on live data alone after every fresh watch, same as the
already-accepted EMA "first value seeds on itself" warm-up transient —
not a new limitation, just the existing one now correctly scoped away
from misleading coarse data instead of contaminated by it.

This is explicitly a stopgap, not the intended end state. The correct,
durable fix is to make these functions genuinely time-aware — decay by
elapsed seconds rather than by bar count, compare volume *rates*
(volume/duration) rather than raw per-bar volume, and require a minimum
elapsed *time* on the correct side of a level rather than a bar count.
That also fixes the irregular gaps *within* the backfilled portion itself
(this stopgap doesn't touch those, since detect_levels still sees them
as-is), not just the live-transition boundary. It was deferred rather
than built immediately because it means reworking `core/`'s public
function signatures (`ema`/`macd` currently take plain `values:
list[float]`, with no timestamps) and its authoritative test suite — a
real redesign, not a quick patch. Candidate for a future phase, alongside
3.5 below, not assumed by the current one.

### 4. Data source

Charles Schwab's API — streaming quotes, aggregated into 10s bars, via the
`schwab-py` library.

**Revised credential model (as of this writing, superseding the original
plan below):** Schwab caps individual/retail developers at ONE app
registration. A separate, Market-Data-only app is not achievable under
this account. `momentum_monitor` therefore authenticates through the
existing ToS_Companion app registration, which has both "Market Data
Production" and "Accounts and Trading Production" — meaning the resulting
token has genuine trading capability. This is a real, accepted tradeoff,
not an oversight, mitigated by:
- A dedicated Schwab account, funded with $1, with no margin enabled -
  bounds any worst-case outcome, from any cause, to that amount.
- `momentum_monitor` containing ZERO order-placement or account-endpoint
  code paths. This is now the primary, actively-maintained protection
  rather than a nice-to-have - any future phase that would add order
  submission (the eventual execution phase, if ever built) requires
  explicitly revisiting this section first, not just writing the code.
- Schwab's own app-level "Order Limit" setting as an additional,
  independent safety layer (exact semantics not yet fully confirmed).

Callback URL: `https://companion-auth.p3l.co/callback` (previously
`127.0.0.1`, changed because 7-day token renewal requires repeated
interactive logins, and this externally-routed URL is already approved
on the existing app - avoiding a multi-week re-approval wait). Must be
handled by a container routed through the existing joelab infrastructure
for that domain - confirm with Claude Code exactly how that routing
connects to `schwab-connector` before assuming it's automatic.

**Original plan (kept for context, not current):** a separate,
Market-Data-Production-only app registration was the original design,
intended to make the token structurally incapable of trading. Confirmed
infeasible given Schwab's one-app-per-retail-developer limit.

**Token lifetime constraint (platform-enforced, unaffected by which app is
used):** Schwab refresh tokens are valid for 7 days, after which a fresh
interactive login is required regardless of client implementation.
"Survives a container restart" is the correct, achievable, testable claim.
"Never requires re-authentication" is not achievable by any client and
must not be implied by definition-of-done language.

**Token refresh architecture:** `companion-auth` vends access-token-only
responses (no `refresh_token`) - by design, keeping the refresh token off
`schwab-connector` entirely. This means `schwab-py`'s own internal
auto-refresh (via authlib, which needs a `refresh_token` in the token
dict it was constructed with) cannot function here - confirmed by reading
schwab-py's source, not assumed. `schwab-connector` therefore manages
renewal explicitly: proactively re-fetch a fresh access token from
`companion-auth` and rebuild the `schwab-py` client (via
`client_from_access_functions`) shortly before each ~30-minute access
token would expire (matching schwab-py's own 5-minute internal leeway
convention), reconnecting the stream as part of that cycle. This makes
stream reconnection a routine, expected event roughly every ~30 minutes
during live operation, not a rare failure case - the reconnect path needs
to be solid for exactly this reason. Deliberately NOT using direct
`AsyncClient`/`AsyncOAuth2Client` manipulation (an alternative that avoids
full reconnects) - that would reach past schwab-py's supported interface,
which contradicts the original reason for choosing a vetted library over
hand-rolling this layer.

**Reconnect-storm incident (2026-09-16, root-caused and fixed):** for a
full hour, `schwab-connector` reconnected roughly every ~15-75ms instead
of every ~30 minutes — 46,695 reconnects, confirmed via
`docker compose logs`, hammering `companion-auth`'s `/access_token`
endpoint at ~13 requests/second sustained the whole time. Root cause:
`ReconnectingStreamSource.ticks()` (reconnect.py) calls
`token_source.refresh_async()` unconditionally at the top of every loop
iteration and trusts that a fresh refresh yields a non-stale
`seconds_until_stale()` budget — true under normal operation (a real
~30-minute token lifetime), but nothing enforced it. Because
`companion-auth` was, during that window, serving an already-near-expiry
cached token on every request (root cause on that side not confirmed —
`companion-auth` is a separate repo/service, out of this one's reach —
but directly observed post-recovery: five rapid `/access_token` calls
against the healthy `companion-auth` all returned the identical cached
token, confirming it caches rather than re-hitting Schwab per-request, so
this was very likely a stuck/stale cache entry on that side, not
`schwab-connector` exhausting Schwab's real OAuth endpoint), every
`refresh_async()` call kept producing an already-stale budget, so
`_consume_until_stale` returned immediately — zero ticks, no `await`
anywhere in that path — and the outer loop refreshed and reconnected
again immediately, forever. This was worse than wasteful: reproduced
directly in a regression test (`tests/test_reconnect.py`,
`test_backs_off_when_a_fresh_refresh_is_immediately_stale_again`), a
tight zero-await loop like this doesn't just hammer the auth helper, it
starves THIS PROCESS's own asyncio event loop, since nothing in the path
ever yields control back to it. That's the confirmed explanation for why
`schwab-connector` showed `(unhealthy)` in `docker compose ps` during the
incident — its own `/health` handler (FastAPI, same process) shares the
event loop the reconnect loop was starving.

Two fixes, both committed: (1) `ticks()` now tracks whether a cycle
yielded any ticks at all; if not, it `await`s the same
`auth_retry_seconds` backoff the auth-error path already used, before
looping back — this alone fixes both the hammering and the starvation,
since awaiting anything hands control back to the loop. (2)
`AccessTokenSource.refresh()`'s default HTTP call
(`httpx.get`) is synchronous, and was previously called directly from
async code at both reconnect call sites — meaning even in NORMAL
operation, a slow `companion-auth` response would block this whole
process for the call's duration, not just during a storm. Added
`refresh_async()` (runs `refresh()` in a thread executor via
`asyncio.to_thread`, proven non-blocking by
`test_refresh_async_does_not_block_the_event_loop` in
`tests/test_token_source.py`), and switched both `reconnect.py` and
`main.py`'s one-shot backfill token fetch to use it instead of the
synchronous `refresh()`. `refresh()` itself is unchanged and still used
directly by anything that isn't running on this process's event loop.

**Price-history date-range quirk (platform-enforced, confirmed live):**
`GET /marketdata/v1/pricehistory` (wrapped by schwab-py's
`get_price_history`), when called with `periodType=day&period=1` and no
explicit `startDate`/`endDate`, returns the PREVIOUS completed trading
day's candles, not the current in-progress session. This is not a guess —
it was caught live, backfilling QCLS on 2026-09-16: the request returned
9/15's full session while the market was mid-session on 9/16, silently
reproducing the exact cold-start VWAP bug the backfill in section 5 exists
to fix, just pointed at the wrong day instead of no day. It matches
schwab-py's own docstring for `get_price_history`'s `end_datetime`
parameter ("Default is previous trading day") — that default apparently
still applies server-side even when a period/periodType pair is given
instead of an explicit range; period-based and range-based requests are
NOT independent, mutually-exclusive modes the way the parameter names
suggest. The fix is to always pass an explicit `start_datetime`/
`end_datetime` range (today's exchange-local midnight through now) when
the intent is "today's session so far," and never rely on
`period_type`/`period` alone for that. See
`momentum_monitor/schwab-connector/price_history.py`
(`fetch_today_bars`), which does this and carries a regression test
(`tests/test_price_history.py`) pinning the explicit-range behavior. Do
not simplify this back to `period_type=DAY, period=1` alone — it looks
more correct/idiomatic on a casual read of schwab-py's API, and silently
reintroduces this exact bug.

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
  - `POST /watch {"symbol": "..."}` — for a symbol with no bars already
    on disk, backfills the current trading day's bars (Schwab
    price-history endpoint, 1-minute granularity — the endpoint's finest
    resolution, extended hours included) before starting live-stream
    aggregation, so a symbol added mid-session still gets a session VWAP
    anchored at market open (or the first extended-hours bar) instead of
    at whenever it happened to be watched. A symbol that already has
    bars (a restart, or a symbol already backfilled) skips this — both
    to avoid a wasted refetch and because re-inserting old bars behind
    already-stored newer ones would trip the store's monotonic-append
    dedup and silently drop the newer bars instead of the redundant old
    ones. A backfill failure (e.g. companion-auth unreachable) is
    non-fatal: live streaming still starts. See
    `momentum_monitor/schwab-connector/price_history.py`.
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
3.6. **Time-aware core indicators.** Rework `ema`/`macd`/`relative_volume`/
   `evaluate_hold`/`detect_levels`'s swing-point window (section 3) to
   decay/compare/require by elapsed real time rather than by bar count,
   replacing the `live_cadence_tail` stopgap (section 3, "Backfill vs.
   live bar width") that currently just excludes backfilled bars from the
   window-based functions instead of correctly weighting them. Also fixes
   the irregular gaps *within* the backfilled portion itself (Schwab
   skips zero-volume minutes), which the stopgap doesn't address. Touches
   `core/`'s public function signatures and its authoritative test suite
   — a real redesign, not a quick patch, which is why it's a separate
   phase rather than bundled into the backfill work that motivated it.
4. Virtual trade journal — logs what the system would have done
   (entry/stop/target) without placing anything, for end-of-day review
   against the user's own judgment.
5. Anything beyond this point (more autonomy, live execution) requires
   its own explicit design discussion and is not assumed by this roadmap.

Do not build ahead of the current phase without an explicit instruction
to move to the next one.
