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

**Swing-point touches require real volume (found live, fixed):**
`detect_levels`'s swing-high/swing-low detection (`levels.py`,
`_swing_points`) excludes any bar with `volume == 0` from being the
CENTER of a touch, even if its high/low ties the local window's extreme.
Confirmed live against real QCLS data: a resistance level showed
`touch_count=20` with `total_touch_volume` exactly `0` — real trades
essentially never print zero shares, so that signature specifically means
a long quiet stretch's synthetic, forward-filled bars
(`schwab-connector/aggregator.py`'s `_fill_gap_until`: a quiet 10s bucket
emits a flat `open==high==low==close==prior-close` bar with
`volume=0.0`) were being counted as repeated real price tests, not that
the level was genuinely tested 20 times. Do not remove the `volume == 0`
guard as a "simplification" — it looks redundant on a casual read (the
window comparison still works without it) and silently reintroduces this
exact bug. Neighboring zero-volume bars still count toward a REAL bar's
own window comparison; only candidacy as the touch itself is restricted.

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
real redesign, not a quick patch. Candidate for a future phase (roadmap
item 3.6 below — 3.5, the other item originally listed alongside it, is
now built, see below), not assumed by the current one.

**Multi-scenario setup evaluation (phase 3.5, built 2026-09-17) —
`core/setup_types.py`.** Rather than surfacing only the nearest
above/below level (`select_levels`, phase 1's simpler design, still used
for the existing resistance/support blocks below), `evaluate_setups()`
computes four DISTINCT candidate setup types in parallel and lets the
caller compare them side by side — a direct evolution of ToS_Companion's
`candidate_generator.py` three-setup-type design, rebuilt on this
repo's corrected level detection instead of its buggy nearest-price
picking.

- **Resistance breakout** — the existing `detect_levels` +
  `evaluate_hold`, reused completely unchanged (swing_window=3, the
  `detect_levels` default).
- **Micro-breakout** — the SAME `detect_levels` function, called a
  second time with `swing_window=1` (`MICRO_SWING_WINDOW`) instead of 3.
  No new detection logic. 1 is half of the main window's default (3),
  floored to an integer — a window that small only needs a single bar
  beaten on each side, so it catches short-term micro-structure swings
  the main window is too coarse to see, at the cost of more noise.
- **VWAP pullback-reclaim** — trend context (current price at/above
  session VWAP, a simple instantaneous check, not a multi-bar trend
  model), gated by a pullback proximity check (`VWAP_PULLBACK_
  THRESHOLD_PCT = 0.5%` of VWAP), then `evaluate_hold` treating VWAP
  itself as the level to hold/reclaim closes above, same 3-bar
  confirmation as everywhere else.
- **Round-number reclaim** — `nearest_round_number_above()` (refactored
  out of `levels.py`'s existing `_round_number_bonus` scoring, which
  keeps its own nondirectional "nearest either side" version for
  proximity scoring) treated as the level for `evaluate_hold`. The one
  type watchable even with ZERO prior price touches at that level — an
  untested round number is still a psychologically real level to retail
  traders, unlike a swing level which requires an actual prior touch to
  exist at all.

**Round-number grid, TIERED by price (fixed 2026-09-17 — see
`levels.py`'s `_round_number_increment`).** The original version used a
single fixed $0.50 increment everywhere. That's wrong at both ends of
this tool's actual price range: a $0.50 jump is meaningless noise for a
$150 stock, and it's a huge, arbitrary jump for a $1 one — confirmed
live against RETO trading at $0.5989, where the fixed grid's nearest
level above was $1.00 (a 40-cent jump) versus the tiered grid's $0.60
(about a tenth of a cent away, the actually-meaningful next dime).
Tiers: **under
$2 → $0.10** (dimes), **$2 up to $10 → $0.25** (quarters), **$10 and up
→ $0.50** (half-dollars) — chosen to roughly cover the user's stated
$0.50–$15 trading range, not empirically fit past that range. The two
breakpoints ($2, $10) were deliberately chosen because they're a shared
multiple of the increments on both sides of each boundary (2.0 is a
multiple of both $0.10 and $0.25; 10.0 is a multiple of both $0.25 and
$0.50), so the grid has no gap or overlap exactly at a tier boundary —
confirmed by test (`test_nearest_round_number_above_has_no_discontinuity
_at_tier_boundaries`). Applies to both the round-number reclaim
candidate above and the existing proximity bonus in level-strength
scoring (section 3, `Level.round_number_bonus`) — one canonical grid
definition, still, same as before this fix.

**Known limitation: round-number reclaim structurally biases "closest"
toward itself, not necessarily toward "best."** Round-number reclaim is
the only one of the four setup types with NO gating condition — it
always produces a candidate, because there is always a round number
somewhere above any price. The other three require real detected
structure to exist at all (a swing-high cluster for resistance/micro-
breakout, an actual VWAP pullback in progress for VWAP reclaim) and are
simply ABSENT when that structure doesn't exist near price. This means
whenever price isn't near real structure, round-number reclaim will
systematically win "closest by dollar distance" by default — not
because it's a better opportunity, but because it's the only candidate
computable at all at that moment. "Closest" is a distance ranking among
whatever candidates happen to exist, not a claim that the closest one is
the best setup; a future reader must not conflate the two. Not fixed in
this pass — flagged here so it's a known, documented tradeoff of the
current comparison method rather than a silent bias someone has to
rediscover.

**Comparison metric: raw dollar distance to trigger, deliberately NOT
percentage and NOT volatility-relative.** A percentage or ATR-relative
metric would already be doing exactly the kind of implicit normalizing
this document's scoring principle (above) rejects for level-strength
components — it would silently favor triggers on more volatile names
over genuinely nearer ones, exactly the sort of collapsed, opaque
comparison this tool exists to avoid. `evaluate_setups()` returns
candidates pre-sorted ascending by this distance; the first one is
"closest," surfaced with full visual weight in the UI (section 5), the
rest as expandable chips.

**Scope for this pass (deliberate, not an oversight): bullish/
breakout-ABOVE direction only.** A symmetric breakdown-below version of
each type is a natural future extension, not built now — kept this pass
a manageable size. All four trigger prices are therefore always
`>= current_price` by construction; a type that isn't watchable right
now (no level above price, no real VWAP pullback in progress) is simply
absent from the result, never a null/zero placeholder entry.

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

**SCHWAB_API_KEY / SCHWAB_APP_SECRET are vestigial in this architecture
(traced, then verified live against the REST path specifically):**
`client_from_access_functions` passes both into authlib's `OAuth2Client`
as `client_id`/`client_secret`. Traced through authlib's actual source:
`client_id`/`client_secret` are used only for (a) building the OAuth
authorize URL, and (b) `client_secret_basic` auth on TOKEN-ENDPOINT calls
(`fetch_token`/`refresh_token`/`revoke_token`/`introspect_token`).
Ordinary resource-server calls — REST (`get_price_history`) and streaming
alike — authenticate via the bearer access token alone
(`self.token_auth`, attached by authlib's `request()`), never
`client_id`/`client_secret` — confirmed by reading that method, not
assumed. Since `schwab-connector` never lets its own `OAuth2Client`
refresh itself in place (see "Token refresh architecture" above — it's
rebuilt fresh from a new access token instead), the one code path where
these values would matter is never exercised here at all. Verified live
(2026-09-17) against the path this actually needs proving on — REST, not
streaming, since they're different call paths and a stream working
doesn't establish a REST call does: triggered a fresh backfill (the
price-history REST endpoint) for NVDA, never previously watched, with
`SCHWAB_API_KEY` confirmed empty throughout. The request returned `200
OK` and backfilled 775 real bars. Do not reintroduce these as "required"
without re-tracing this — and if `schwab-connector`'s token handling is
ever reworked to let schwab-py refresh in place instead of being rebuilt
externally, revisit this specifically, since that's the code path where
they'd start mattering.

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

**Stream events weren't tagged by symbol (found live 2026-09-17, fixed):**
each watched symbol gets its own independent `ReconnectingStreamSource`
instance (up to `MAX_SYMBOLS`, section 5), but all of them share one
process-wide `on_event=log_event` callback (`main.py`), and none of
`reconnect.py`'s `self._on_event(...)` calls passed which symbol the
event was about — `ticks(self, symbol)` had `symbol` in scope the whole
time, it just never got threaded through. Found while diagnosing a real
incident: 3 of 4 watched symbols went stale (no new bars for several
minutes) after a burst of container restarts; the logs showed exactly
one `stream_error` ("STREAM CONNECTION NOT FOUND — Please login again")
followed by a `stale_immediately_after_refresh` backoff, but there was
no way to tell WHICH of the 3 affected symbols it belonged to from the
log line alone, slowing down the diagnosis. Fixed by adding
`symbol=symbol` to every `on_event` call in `ticks()` and
`_consume_until_stale()` (`test_every_event_carries_its_own_symbol`
asserts every event kind this module emits carries it). `events.py`
needed no change — `format_event` was already generic over kwargs, so
`event=stream_error symbol='DAIC' error=...` just falls out of it.

This fix is what made the NEXT bug findable at all — see below. (Superseded
2026-09-17: once `schwab-connector` moved to ONE shared connection for all
symbols — see below — per-event symbol-tagging stopped being the right
model, since there's only one connection to log about now, and was
reverted along with that change. It served its purpose: without it, the
next two bugs below would have been much slower to find.)

**`BarAggregator.flush()` only forward-filled a quiet stream ONCE, ever
(found live 2026-09-17, fixed) — the real root cause of the stale-feeds
incident that prompted the symbol-tagging fix above.** Symptom: a
watched symbol's bars would burst-update after being re-added, then
freeze completely — `last_bar_ts` stuck for 5+ minutes at a time — with
`/health` reporting `connected: true` and NO stream errors in the logs
at all (confirmed only AFTER the symbol-tagging fix above made it
possible to watch one specific symbol's events in isolation and see
that literally nothing was firing for it, not even a `proactive_refresh`
— genuine silence, not a masked error). Root cause, in `aggregator.py`:
`flush(now_ts)` began with `if self._cur_start is None: return` —
`_finalize_current()` always sets `self._cur_start = None` when it
closes a bucket, so the FIRST `flush()` call after a bucket goes quiet
correctly forward-fills up to that moment, but `self._cur_start` stays
`None` afterward, and there is nothing that reopens it except a real
tick. In production, `Connector._flush_loop` (`app.py`) calls `flush()`
on a fixed 2-second timer forever, independent of whether ticks are
arriving — so on a stream that goes quiet for longer than one flush
interval (not a failure — real, thin-volume names do this), the series
forward-filled exactly once and then froze at whatever moment that one
flush happened to catch up to, even though the flush loop kept running
correctly every 2 seconds after that, because every one of those later
calls hit the same early-return and did nothing. Confirmed directly:
DAIC printed real, heavy volume (1,000–6,000+ shares per 10s bucket)
right up to its last bar, then produced literally zero further bars for
20+ minutes on a connection that never errored — not a quiet market, a
frozen aggregator. Reproduced in a unit test first
(`test_flush_keeps_forward_filling_across_repeated_calls_with_no_ticks`)
before touching the fix, confirming two consecutive `flush()` calls with
no ticks between them, exactly `Connector._flush_loop`'s real call
pattern. Fix: gap-filling (`_fill_gap_until`) now runs on every `flush()`
call unconditionally, not gated on `self._cur_start is not None` —
bucket-closing (`_finalize_current`) still only happens when there's an
actually-open bucket to close, but the forward-fill itself no longer
depends on one existing. This is a genuine correctness bug that
predates today, present since aggregator.py was first written in phase
1 — it just took multi-symbol concurrent live load (phase 2) with
symbol-tagged logging to actually catch it happening and prove why.

**Per-symbol tick heartbeat (added 2026-09-17) — `Connector` logs one
`event=tick_heartbeat symbol=... ticks_in_last_60s=N` line per symbol
roughly every 60s.** Exists because `connected: true` and no stream
errors logged are NOT proof real ticks are actually flowing — this was
the next diagnostic needed after the aggregator fix above still left
several symbols showing forward-filled flat bars that directly
contradicted real, independently-observed market volume. `Connector`
counts real ticks fed to each symbol's `BarAggregator` and reports the
count (then resets to 0) on the same flush-loop cadence as everything
else. Genuinely useful, permanent observability, not a one-off debug
hack — it's what surfaced the real root cause immediately below.

**ONE shared stream connection for all watched symbols (found + fixed
2026-09-17) — the real, final root cause of tonight's stale-feeds
incident, superseding the phase-2 "4 independent WebSocket sessions"
architecture note.** The tick heartbeat above showed real ticks arriving
at a small fraction of what actively-trading stocks should produce, and
— the key signal — which ONE of the 4 watched symbols got the (still
modest) bulk of ticks ROTATED minute to minute:
```
60s window 1: WETO=6   AEMD=0   RETO=1   DAIC=0
60s window 2: WETO=0   AEMD=0   RETO=0   DAIC=22
60s window 3: WETO=0   AEMD=43  RETO=0   DAIC=1
```
Root cause: each watched symbol had its OWN independent
`ReconnectingStreamSource` → own `StreamClient` → own WebSocket login,
all four against the SAME Schwab account/token concurrently — exactly
the risk phase 2's own docs already flagged ("4 concurrent symbols means
4 independent WebSocket sessions... needs to be watched under real
load, not assumed") and which this incident confirmed actually
materializing: Schwab's streaming service appears to only fully service
one (or very few) of several concurrent sessions on the same account at
a time.

Fix: ONE shared `StreamClient` (one login, one ~30-minute reconnect
cycle) subscribed to the union of all currently-watched symbols, ticks
demuxed by symbol after arrival. Confirmed via direct `schwab-py`
inspection (`docker exec` into the running container) that this is
well-supported without needing a reconnect to change the subscribed set:
`level_one_equity_subs` (initial), `level_one_equity_add` (add more,
live), `level_one_equity_unsubs` (remove one, live) all operate on the
same connection. `stream.py`'s `message_to_ticks()` (the pure,
already-tested Schwab-payload → ticks mapping) turned out to already be
multi-symbol-shaped — it always returned one `(symbol, tick)` pair per
message content entry; the single-symbol restriction lived entirely in
`SchwabStreamSource`'s own message filter, which needed to go, not in
the parsing logic, which needed zero changes.

Three layers changed together (`stream.py`, `reconnect.py`, `app.py`),
each keeping a clear contract with the one above/below it:
- `stream.py`: `SchwabStreamSource.ticks(symbols)` takes the symbol SET
  to subscribe, yields `(symbol, tick)` pairs for ALL of them (no more
  per-instance `self._symbol` filter). New `add_symbols`/`remove_symbols`
  for live updates. `ReplayStreamSource` (test/offline fixture player)
  takes a `watched_symbols` GETTER too and broadcasts each replayed tick
  to every symbol currently in that set — a single fixture can't
  represent several independently-moving real symbols, so broadcasting
  is the deliberate test-tool choice, not a limitation that matters in
  production.
- `reconnect.py`: `ReconnectingStreamSource.ticks()` drops the `symbol`
  parameter entirely and reads `watched_symbols()` FRESH at the top of
  every (re)connect — a getter, not a frozen list, so a symbol added or
  removed between reconnects "just works" on the next connect with no
  separate pending-changes bookkeeping. New `add_symbol`/`remove_symbol`
  forward to the currently-live inner source for an immediate,
  no-reconnect-needed change; a no-op (not an error) when nothing is
  connected right now, since the next connect picks it up anyway.
- `app.py`: `Connector` replaces its old `_sources`/`_tasks` dicts (one
  task per symbol) with `_aggs: dict[str, BarAggregator]` (per-symbol bar
  state, unchanged concept) plus ONE `_shared_source` and ONE
  `_consume_task` for the whole app's lifetime, started lazily on the
  first `watch()` call and never torn down again (even at zero watched
  symbols) to avoid relogin churn from watch/unwatch cycling. `watch()`
  backfills that symbol's history first (same guarantee as before), then
  either starts the shared consumer (first-ever symbol) or calls
  `add_symbol` on the already-live one. `unwatch()` calls `remove_symbol`.
  ONE `_flush_loop` now iterates every symbol's aggregator each cycle
  (naturally simpler than the old one-loop-per-symbol design, and the
  tick-heartbeat bookkeeping above moved here unchanged in spirit).

`BarAggregator` itself (10s bar accumulation) is completely unaffected —
this was entirely about how ticks get DELIVERED to each symbol's
aggregator, never about how they're aggregated once delivered.

**Confirmed live (2026-09-17), after deploy:** exactly one `access_token`
fetch and one connection attempt for all 4 watched symbols (not four,
confirmed via logs), zero collision errors since. Deploy hit an unrelated
recurrence of the `companion-auth` stale-token issue (a second restart of
that separate service was needed; see its own incident note above) —
once past that, two consecutive 60-second `tick_heartbeat` windows showed
comparable, non-rotating, non-zero real tick counts on every symbol
simultaneously:
```
window 1: WETO=4  AEMD=30  RETO=35  DAIC=36
window 2: WETO=5  AEMD=25  RETO=35  DAIC=25
```
Directly contrast with the baseline that motivated this fix (reproduced
above): one symbol getting the bulk of ticks while the other three got
0, rotating minute to minute. That rotation is gone.

**Poll replaced with push, both legs (fixed 2026-09-17) — the
self-inflicted latency stacked on top of the (now-fixed) throttling
ceiling above.** Once the shared-connection fix above proved Schwab's
own stream was healthy, a second, separate latency source remained,
architectural rather than a bug: `monitor-app` polled `schwab-connector`
(`GET /bars/{symbol}`) every `POLL_INTERVAL` (5s), and the browser polled
`monitor-app` (`GET /api/state`) every `POLL_MS` (4s) — two independent
timers stacked on top of each other, ~9s of combined latency on a good
day, unrelated to anything Schwab-side. Both legs are now genuine push,
Server-Sent Events specifically (not full websockets — both legs only
ever flow one direction, narrowly reversing the "no websockets" call
from phase 1, which was reasonable before there was evidence of a real
latency problem):

- **`schwab-connector` → `monitor-app`:** new `GET /events` route.
  `Connector` gets a best-effort pub/sub layer (`subscribe()`/`_notify()`,
  bounded per-subscriber queue, drop-oldest on overflow) fired from
  `_drain()` the instant a bar is stored — defensively, so a broken or
  slow subscriber can never crash or block bar storage (same standard as
  the tick-diagnostic crash lesson above: instrumentation/notification
  code must never be able to take down the production message-delivery
  path it observes). `monitor-app`'s `main.py` gets a matching
  `stream_events()` — one long-lived task holding the shared SSE
  connection, with its own purpose-built reconnect/backoff (not
  `ReconnectingStreamSource`, which is coupled to Schwab-token semantics
  this internal leg doesn't have).
- **`monitor-app` → browser:** new `GET /api/state/stream` route,
  same payload shape as `GET /api/state`, pushed on every state change
  instead of polled. The browser's `setInterval(refresh, 4000)` is gone,
  replaced with a native `EventSource('/api/state/stream')` — no manual
  reconnect logic needed, `EventSource` retries per spec, including
  across a `monitor-app` restart, and each reconnect's first message is
  always a fresh full snapshot, not a delta.

`GET /api/state` and the REST `GET /bars/{symbol}` fetch are both kept,
not removed — a one-shot catch-up (`Poller.catch_up`, still using the
same REST fetch) still runs once when a symbol is first watched, and
again for every watched symbol on every stream (re)connect and on poll
resume (`Poller.resync_all`), closing any gap between "what monitor-app
has" and "what schwab-connector has stored" the same ts-dedup guard
(`if not slot.bars or bar["ts"] > slot.bars[-1]["ts"]`) always protected,
regardless of which path (push or catch-up) a given bar arrives through
first. `poll_enabled`/`POST /api/polling` keeps its exact original
meaning — paused means monitor-app stops applying incoming updates,
`schwab-connector` keeps streaming and storing regardless either way —
just re-triggered by push instead of a timer tick; resuming calls
`resync_all()` so nothing pushed during a pause is silently lost. The
DOM-patch rendering (expand/collapse-state save/restore across a full
`#symbols` rebuild, from the phase-3.5 fix above) is unchanged in spirit
either way — a push-triggered render calls the exact same `render(data)`
a poll-triggered one always called, split out of the old `refresh()`
verbatim.

Tested at the unit level as a structural, sleep-free proof (per
AGENT_PROTOCOL.md's no-live-network/no-wall-clock-dependence rule): both
`Connector._notify` and `Poller.apply_bar_push`/`_broadcast_state` land
in a subscriber's queue the instant the triggering call returns, no
`asyncio.sleep` needed to observe it — the actual thing being fixed
(no timer in the path) is the thing the test proves. `fastapi.
testclient`'s `httpx` `ASGITransport` fully buffers a response (runs the
whole ASGI app call to completion) before returning anything, so it can
never observe partial output from a route that streams until client
disconnect — both new SSE routes' end-to-end tests drive the ASGI app
manually (a small `_drive_streaming_route` test helper, in both
`schwab-connector/tests/test_app.py` and `monitor-app/tests/test_app.py`)
instead of going through `TestClient.stream()`.

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
  - `POST /unwatch {"symbol": "..."}` — stops live-streaming a symbol
    (cancels its consume task, drops it from `watching`) without touching
    its stored bars — history stays on disk, only the live subscription
    stops. Idempotent: unwatching a symbol not currently watched is a
    no-op, 200 either way. Awaits the cancelled task's actual teardown
    before returning, not just scheduling the cancellation, since the
    caller may immediately watch a different symbol right after.
  - `GET /bars/{symbol}?since_ts={unix_seconds}` → array of bar objects
    per the shape in section 4.
  - `GET /health` → `{"status": "ok", "watching": [...], "connected": bool}`
  - `GET /events` → Server-Sent Events, one `event: bar\ndata: {"symbol":
    ..., "bar": {...}}` per bar stored, for every currently-watched
    symbol over one shared connection (added 2026-09-17, poll -> push —
    see section 4). `GET /bars/{symbol}` stays as the one-shot catch-up
    fetch a fresh subscriber uses to backfill before/around its first
    push, not removed.
- **`momentum_monitor/claude-connector/`** — the only container with the
  `claude` CLI's auth mounted in. Shells out to `claude -p` for
  event-triggered narration. Not built until phase 3 (see roadmap below)
  — currently a placeholder directory with a README only.
- **`momentum_monitor/monitor-app/`** — the FastAPI web app. Holds no
  credentials. Consumes `schwab-connector`'s pushed bars (`GET /events`,
  poll -> push fixed 2026-09-17 — see section 4), runs them through
  `momentum_monitor/core/`, serves a web view. The only container with a
  port published to the host (`8012`).

  **Multi-symbol (phase 2, built) — up to `MAX_SYMBOLS` (4) concurrently.**
  `WATCH_SYMBOL` is only the STARTING symbol (optional — unset means idle,
  no symbol watched until one is added). `Poller` (app.py) tracks a `dict`
  of up to 4 `_SymbolSlot`s keyed by symbol — each slot holds its own bar
  history, computed state, and journal position, fully independent of the
  others. `POST /api/watch {"symbol": "..."}` (urlencoded form) ADDS a
  symbol to the watched set, filling the next empty slot — it does NOT
  replace whatever else is watched (that was the phase-1 ticker box's
  behavior; phase 2 deliberately changes it, since concurrent multi-symbol
  is now the actual point). Rejections are explicit JSON, never a silent
  failure or a silent slot replacement of an UNRELATED symbol: an invalid
  ticker or a duplicate already being watched each get their own reason,
  HTTP 409. Adding a 5th symbol while already at the 4-symbol maximum is
  **not** one of these rejections (changed 2026-09-17, at the user's
  request — the original build session had this as a 409 instead, see
  the git history around `test_post_watch_at_capacity_evicts_the_oldest_
  symbol_not_a_rejection` for the before/after): it evicts the
  oldest-added symbol (FIFO — `_slots` is insertion-ordered, the same
  fact `Poller.symbols` relies on) via the exact same code path as an
  explicit `/api/unwatch` (`remove_symbol`), so the evicted symbol's open
  virtual-journal position is force-closed and schwab-connector is told
  to stop streaming it — no orphaned position, no leaked stream. This is
  HTTP 200, `"ok": true`, with `"reason"` carrying a human-readable note
  of what got dropped (e.g. `"dropped AEHL (oldest) to make room for
  S5"`) — not silent, just not rejected. `POST /api/unwatch {"symbol":
  "..."}` removes one specific
  symbol, force-closing its own open virtual-journal position (see section
  6) without touching any other slot. A poll already in flight for a
  symbol that gets removed (and possibly re-added) mid-fetch has its
  result discarded — `_poll_once` re-checks the slot's object identity,
  not just its key, after the fetch's `await` returns, so "removed" and
  "removed then re-added" are both caught. `schwab-connector` itself
  needed zero structural changes for this at the `monitor-app` boundary
  — `POST /watch`/`POST /unwatch` per symbol was already the right shape.
  Verified live (2026-09-16) under real 4-symbol concurrent load: a
  natural reconnect did not disturb the other three symbols' bars or
  journal state. (What DID need to change, on `schwab-connector`'s own
  internal side, was how those 4 concurrent symbols share the underlying
  Schwab connection — see section 5's `schwab-connector` entry, "ONE
  shared stream connection.")

  **`GET /api/state` shape change (breaking, deliberate, no back-compat
  shim — nothing else in this repo depended on the old single-object
  form).** Old (phase 1): one flat object for the single watched symbol.
  New (phase 2): `{"symbols": {SYM: {...same per-symbol shape as phase
  1's whole response, plus a "journal": {"open": {...}|null}}, ...},
  "recent_closed": [...], "poll_enabled": bool, "max_symbols": int}`.
  `recent_closed` stays intentionally cross-symbol (it already was in
  phase 1) — audited specifically to confirm it should stay that way, not
  get scoped per-symbol.

  **Page refresh mechanism (redesigned from a bug, not a style choice;
  superseded again 2026-09-17 — see section 4's poll -> push entry):**
  the page originally used `<meta http-equiv="refresh" content="5">` — a
  full page reload every 5 seconds. That was never the design (the
  original intent was always in-place JS updates); the full reload was
  the actual cause of visible flicker/redraw, not a matter of taste. It's
  gone, replaced with an inline `<script>`: `setInterval(refresh, 4000)`
  called `GET /api/state` and rebuilt the `#symbols` container's innerHTML
  in place from the current set of watched symbols (still no meta-refresh,
  no full-page reload — a coarser-grained in-place update than phase 1's
  per-element patching, chosen because the set of symbols itself can
  change size between polls). That `setInterval` timer is itself gone now
  too, replaced with a native `EventSource('/api/state/stream')` — the
  DOM-patch logic it drove (the innerHTML rebuild, described below) is
  unchanged, only pulled out into its own `render(data)` function so it
  can be called from either the push path or the still-present explicit
  post-action `fetch('/api/state')`. The Python side (`app.py`'s `_page`) still
  computes the same real first-paint HTML from current `state`/`journal`
  on every server request — a fresh load shows real data immediately, and
  it keeps server-side rendering meaningfully testable without a browser
  — while the JS mirrors the same rendering logic for subsequent in-place
  updates. The two renderers are deliberately duplicated, not shared:
  "single file, no framework, no build step" rules out a shared
  template, so this is a small, contained, explicitly-commented tradeoff,
  not an oversight. The visual redesign (dark card-based layout, a real
  type scale, meaningful color: price vs VWAP, price vs EMA9, MACD
  histogram sign, open/closed P&L sign) stays inside the same
  non-negotiable constraint as core's own scoring (section 3): nothing
  gets collapsed into a single composite number — level strength
  components and hold-confirmation's consecutive-bars/failed-attempts
  detail are exactly as visible as before, just better laid out.

  **Multi-panel grid (phase 2 Stage B, built; grid mechanics fixed
  2026-09-17 — see below).** `#symbols` is a CSS grid, one card per
  watched symbol, each showing exactly what phase 1's single card showed
  (price, indicators, levels, virtual position) plus its own `remove`
  button scoped to that panel's own symbol (`data-symbol`, wired via
  event delegation on `#symbols` so it survives the container's innerHTML
  being replaced every poll). An add-symbol form (text input + "Add")
  above the grid POSTs `/api/watch` and shows the server's own response
  reason inline — as an error on a 409 (a duplicate, an invalid ticker)
  or as a muted note on a 200 that evicted the oldest symbol to make
  room — rather than failing or evicting silently; a live `N / 4 symbols
  watched` counter sits next to it.

  **Multi-scenario setups in the grid (phase 3.5, built).** `build_state`
  (`state.py`) adds a `"setups"` key — `setup_types.evaluate_setups()`'s
  output, already sorted ascending by dollar distance (section 3) — using
  the exact same `bars`/`live_bars` split every other bar-count-windowed
  computation on this page already uses, not a second split invented for
  this. Each card shows the closest candidate with the same visual weight
  as the resistance/support tables below it (type, trigger price, dollar
  distance, its own hold-state, its own factors — never collapsed into a
  score), and the remaining candidates as compact chips (type + dollar
  distance) that expand in place on click to reveal that type's own
  factors — a local DOM toggle (`chip.nextElementSibling.hidden`), no
  fetch, delegated on `#symbols` the same way the remove control is.
  Expanded state SURVIVES the next poll (fixed 2026-09-17 — originally
  documented here as "an accepted tradeoff, not an oversight," which
  turned out to be wrong in practice: found live, a real user watching
  the page had an expanded section silently collapse on them every
  ~4s, which reads as broken, not as an acceptable tradeoff). Every
  `.setup-chip` (both the three other setup-type chips and the
  resistance/support chips) carries a `data-key` — `{symbol}:setup:
  {setup_type}` or `{symbol}:level:{resistance|support}` — unique
  across a full `#symbols` rebuild. `refresh()` now records which
  `data-key`s are currently expanded (their sibling `.setup-detail` not
  `hidden`) BEFORE replacing `#symbols.innerHTML`, then re-applies
  `hidden = false` to the matching fresh elements AFTER — values still
  come from the live poll (correct, current data), only the open/
  closed state is preserved across the rebuild, not the stale content.
  Proved with a real headless-browser test (Playwright, installed
  2026-09-17 specifically to close this verification gap — see below):
  open a section, wait past a real ~4s poll interval, assert it's still
  open; this is a materially different test than "does a click toggle
  work" and is the one that would have caught the original bug before
  it shipped. One generic row renderer
  (`_setup_hold_and_factor_rows_html` / `setupHoldAndFactorRows` in the
  JS mirror) handles all four types' `factors` dicts rather than four
  hand-written table layouts, since the "don't collapse into a score"
  principle only requires each factor to stay visible, not a bespoke
  layout per type.

  **Resistance/support collapsed behind the same chip pattern (fixed
  2026-09-17).** The raw resistance/support tables used to render always-
  open, directly duplicating whatever the closest-setup callout above
  them already shows in full whenever that closest type happens to be
  resistance breakout. They're now collapsed behind the exact same
  setup-chip/setup-detail toggle the other three setup types use
  (`_level_block_html`/`levelBlockHtml`, reusing the SAME generic click
  handler — no new JS wiring needed for this). Meaningfully cuts default
  panel height without losing anything; still one click away.

  **Genuine 4-column grid + tighter density (fixed 2026-09-17 — found
  live: a real 4th watched symbol, DAIC, was confirmed present and
  healthy in `/api/state` but not visible on the page without scrolling
  past the fold).** Root cause: `.grid` used `grid-template-columns:
  repeat(auto-fit, minmax(22rem, 1fr))` — at the page's then-`max-width`
  of 76rem (minus padding, ~73rem of actual content width), four 22rem
  columns plus three 1rem gaps need 91rem, so auto-fit silently wrapped
  to 3 columns at completely ordinary desktop widths. Not a data bug —
  DAIC was always in the response — a layout bug that LOOKED like a
  missing panel. Fixed by targeting the real column count directly
  (`repeat(4, 1fr)`) instead of leaving it to auto-fit's own arithmetic,
  with breakpoints down to fewer columns only when 4 genuinely can't fit
  at a legible width anymore:
  - `body` `max-width`: `76rem` → `84rem` (more room to work with on
    normal desktop monitors, still comfortably narrower than a 1366px-
    wide laptop's viewport).
  - `.grid` `gap`: `1rem` → `.75rem`; `.card` `padding`: `1rem 1.2rem` →
    `.75rem .9rem` — both trimmed to reclaim width/height for content,
    not decoration.
  - Below **68rem** viewport width, 4 columns of ~15rem (the minimum
    this page's dense content stays legible at) no longer fit — drops to
    2 columns. Below **38rem** (phone width), drops to 1.
  - Secondary/label text tightened for vertical density: `table.detail`
    cell padding `.3rem .5rem` → `.2rem .4rem`, font-size `.9rem` →
    `.82rem`, `line-height: 1.25` added. Heading margins (`h1,h2,h3`)
    `.5rem` → `.35rem`. `.hero-price` `2.4rem` → `1.9rem`, `.hero-symbol`
    `1.4rem` → `1.15rem` (still the headline number, just no longer
    sized for a single-symbol page multiplied by 4). `.setup-chip`/
    `.badge` font-size `.78rem` → `.72rem`, `.setup-chips` margin
    `.4rem 0 .8rem` → `.3rem 0 .5rem`.
  These, combined with the resistance/support collapse above, are what
  actually fixed the "mostly scrolling" complaint — the collapse removes
  vertical content, these values remove the padding/font overhead
  multiplied by 4 panels' worth of it.

  **Pause/resume — one flag, unchanged meaning, now gating push instead
  of a timer (originally "two layers", see below).** A "Pause updates"
  button next to the ticker box calls `POST /api/polling {"enabled":
  bool}`, which pauses `monitor-app`'s own applying of incoming updates
  from `schwab-connector` — the flag and route name are unchanged since
  phase 2, only what they gate changed with the poll -> push fix (section
  4): originally `Poller.run()`'s background poll loop hitting `GET
  /bars/{symbol}` on a timer, now `Poller.apply_bar_push()`'s handling of
  each bar arriving over `GET /events`. `GET /api/state`(`/stream`)'s
  `poll_enabled` field is still the resulting server truth, still
  treated as authoritative by the page's JS (re-syncing the toggle
  button's label on every state update) rather than a client-only
  preference — correct across multiple tabs/devices, not just the one
  that clicked the button. Pausing never touches `schwab-connector`'s own
  live Schwab stream or its stored bars either way; resuming calls
  `resync_all()`, so anything pushed (and dropped) while paused is caught
  up via one REST fetch per watched symbol, not lost. (Historical: this
  used to be described as "two layers" because the browser's own
  `setInterval` was a second, independent thing the same button also
  stopped — that layer doesn't exist anymore now that the browser side is
  push-based too; an `EventSource` connection has nothing to start/stop
  client-side, it just goes quiet because the server stops broadcasting
  while paused.) Verified live (2026-09-16, pre-push-fix): paused,
  `bar_count` genuinely stopped advancing for 12 real seconds against the
  running stack; resumed, it advanced again immediately.
- **`momentum_monitor/docker-compose.yml`** — orchestrates all three.

### 6. Virtual trade journal — momentum_monitor phase 4

Logs what the system would have done (entry, trailing stop) without
placing anything, for later review against the user's own judgment.
Lives in `momentum_monitor/monitor-app/` (`journal_logic.py` for the pure
decision functions, `journal_store.py` for SQLite persistence, wired into
`Poller` in `app.py`) — not `core/`, because it depends on Poller-level
state (which symbol is watched, the accumulated bar list), not pure
market analysis, even though `journal_logic.py` keeps the same "no I/O"
discipline `core/` uses for the same reason `core/` does (see section 3).

**Entry.** Fires exactly once per `hold.confirmed` False→True transition
on the nearest-above resistance level already computed and displayed on
the page (`state["levels"]["resistance"]["hold"]["confirmed"]`, from
`core/levels.py`'s `evaluate_hold`) — no new entry-signal logic invented.
`entry_price` is the close of the bar the transition is observed at (in
practice: the latest bar in the poll cycle where the transition is first
seen — the finest granularity available without re-running
`evaluate_hold` per-bar inside a single poll, which would itself be
inventing new entry logic). At most one open virtual position PER SYMBOL
(phase 2: up to 4 symbols can each have their own independently open
position at once, not one global position for whichever symbol happens
to be watched); if a position is already open for a symbol, a continued
or repeated `True` reading for that same symbol does not fire a
duplicate. `journal_logic.py`/`journal_store.py` needed no changes for
multi-symbol — audited specifically for a single-global-position
assumption and found none: every lookup was already scoped by symbol
(`open_position_for`) or by the specific row id
(`update_trailing`/`close_position`). The single-position assumption that
did exist lived in `Poller`'s own state, fixed by giving each watched
symbol its own `_SymbolSlot` (section 5).

**Exit — trailing stop only, no fixed target, by design.** A fixed R:R
target was explicitly rejected for this project: it capped winners in the
EOD swing bot and contributed to that strategy's edge not holding up
under proper testing. There is no target anywhere in `journal_logic.py`,
by design, not by omission.
- `TRAIL_PCT` (env var, default `0.05` / 5%, see `main.py`) — a starting
  point to tune against real logged data, not a validated number.
- `high_water_mark` starts at `entry_price` and ratchets up from each new
  bar's HIGH (never its close) — it never moves down.
- `stop_level = high_water_mark * (1 - TRAIL_PCT)`, recomputed every
  ratchet.
- A bar's LOW crossing below the (freshly-ratcheted) `stop_level` exits
  immediately — no confirmation delay. This mirrors the SAME asymmetry
  section 3 already establishes as non-negotiable for hold-confirmation
  generally (entries need sustained confirmation, stops fire fast, no
  exceptions) — not a new rule invented for this journal specifically.
  When a single bar's high raises the stop AND its low would breach that
  new, higher stop, the exit still fires: OHLC bars don't record whether
  the high or low happened first, so the worse-case-for-the-position
  ordering is assumed. `exit_price` on a stop is the `stop_level` itself
  (a virtual/simulated-fill modeling choice — assume the stop fills at
  the stop price — not a claim about real fill behavior).

**Removing a watched symbol (an edge case that didn't exist when phase 4
was first scoped, added once the watch/unwatch text box did — section
5).** When `Poller.remove_symbol` drops a symbol with an open virtual
position, it force-closes that position at the symbol's last known
close, `exit_reason="symbol_switched"` (kept as the same reason string
phase 1's ticker-switch used, since removal is the phase-2 equivalent
event) — distinct from `"trailing_stop"` so later review doesn't conflate
"the trade stopped out" with "the symbol was removed." A position is
never left open with no further price updates, which could never
resolve. Removing another symbol, or resuming an open position on
re-add (or a full restart), never touches a DIFFERENT symbol's own open
position — proven by test, including a dedicated multi-symbol test
driving two symbols through real `hold_confirmed` transitions
concurrently and confirming one's trailing-stop exit leaves the other's
`id`/`entry_price`/`high_water_mark`/`stop_level` completely untouched,
and verified live (2026-09-17): a real entry+exit fired on one of 4
concurrently-watched real symbols with the other three's journal state
confirmed unchanged via direct DB inspection throughout. Re-adding (or
restarting into) a symbol with an already-open position resumes tracking
it from `journal_store` rather than losing or duplicating it — proven by
test (two `JournalStore` instances over the same SQLite file), the same
rigor already applied to bars/tokens surviving a restart.

`symbol_switched` closed-trade rows are visually muted end to end in
the closed-trades table (`_journal_closed_rows_html`/`journalClosedRows`,
`row-housekeeping` CSS class) — including overriding the pos/neg P&L
coloring a real `trailing_stop` exit gets, even though `realized_pnl_pct`
is a real, computed number for a `symbol_switched` row too. The point
isn't that the number is wrong, it's that it was never a trading
decision the strategy made — muting it stops anyone from reading it as
a win/loss at a glance. Any future win-rate/expectancy summary (see
"eventually compute win rate/expectancy" below) MUST filter to
`trailing_stop` (and eventually `target_hit`, if a target is ever added
— it isn't currently, see above) exits only; a `symbol_switched` row is
never a trading outcome and must never be counted as strategy
performance, no matter how tempting it is to just average
`realized_pnl_pct` across every closed row.

**Storage: SQLite, not JSONL.** A different access pattern from
schwab-connector's bars (append-only, replayed sequentially start to
finish, one file per symbol) — trade records need to be QUERIED and
reviewed (find the open one for a symbol, list recent closed ones,
eventually compute win rate/expectancy), which SQLite fits better. Don't
default to JSONL just because that's what bars used — different access
pattern, different storage choice, on purpose.

Schema (`trades` table): `id`, `symbol`, `entry_ts`, `entry_price`,
`high_water_mark` (updated live while open), `stop_level` (updated live
while open), `exit_ts`, `exit_price`, `exit_reason` (nullable while
open), `realized_pnl_pct` (nullable while open).

**Page/API.** `GET /api/state`'s JSON gains a `"journal"` key (open
position + live unrealized P&L%, plus up to 10 recent closed trades, all
symbols, most recent first); the HTML page gets a matching plain-table
section, same style as the existing levels tables — no new framework.

**Deleting closed-trade rows (added 2026-09-17).** Trade history is
real, permanent SQLite data — journal noise from testing was piling up
with no way to clear it. Two endpoints, both irreversible deletes, both
gated behind a `confirm()` dialog client-side before the page ever calls
them (never a silent delete, same standard this project holds
everywhere else — see the momentum_monitor phase-2 eviction work):
- `POST /api/journal/delete {"id": "..."}` — deletes ONE closed row by
  id (`JournalStore.delete_closed`). Scoped to closed rows only
  (`exit_ts IS NOT NULL`) — deliberately CANNOT delete an open position,
  since that would silently desync it from `Poller`'s own in-memory
  `_SymbolSlot.journal_position`, which has no way to learn the row
  vanished underneath it. A small `delete` button, same styling as the
  existing per-panel `remove` control, sits on each closed-trades row.
- `POST /api/journal/clear_symbol_switched` — deletes EVERY
  `symbol_switched` closed row in one call (`JournalStore.
  delete_symbol_switched`), returning the count removed. The specific,
  real cleanup this was built for: most of the accumulated history by
  volume was `symbol_switched` housekeeping noise from testing multi-
  symbol eviction/removal, not real `trailing_stop` outcomes. A single
  "clear symbol_switched rows" button sits next to the "Recent closed
  trades" heading.
Both routes are pure HTTP-wiring around already-unit-tested SQLite
methods (`test_journal_store.py`) — no new deletion logic invented at
the app layer.

**Bulk-clear had no visible feedback (found live, fixed 2026-09-17).**
Reported as "the bulk clear button failed" — root-caused with a real
headless-browser click (Playwright), not by guessing: the endpoint
itself was and is correct (confirmed directly, both via `curl` and via
Playwright's own network capture of the real click: `POST
/api/journal/clear_symbol_switched` → `200 {"ok": true, "deleted": 0}`).
The actual gap was that a 0-row delete — a real, correct outcome
whenever there's simply nothing currently matching `symbol_switched`
left to clear — produced NO visible change on the page at all, which
reads identically to the button silently failing. Fixed by adding a
`#clear-status` span next to the button that the click handler now
populates with "cleared N row(s)" or "nothing to clear" from the
response body, mirroring the existing add-symbol form's status-message
pattern.

**Headless browser (Playwright + Chromium) installed 2026-09-17, into
this project's own `.venv`.** Every UI bug up to this point had to be
verified by manual click-and-report, or by extracting the deployed
page's actual JS and executing it against a hand-built DOM shim in
Node — workable but never a test of the REAL rendered page, and unable
to catch a bug like the toggle-state one above, which only manifests
across a real poll cycle a script can't fake. `playwright install
chromium` downloads and runs cleanly in this environment with no
system-level dependencies needed (confirmed — no `sudo`/`apt` required,
launches and renders correctly). This is a one-time environment fix,
not a per-bug workaround: real interaction tests (click, wait through a
real timer, assert on the resulting DOM) are now something this project
can actually run, not just reason about.

### 7. Roadmap / phases

1. **(built)** One symbol, live Schwab data through the tested core,
   a basic web page showing correct numbers. No trades, no multi-symbol,
   no LLM.
2. **(built, 2026-09-17)** Multi-symbol, up to 4 concurrent. See section 5
   for the full design (`_SymbolSlot`, the `/api/watch`/`/api/unwatch`
   add/remove semantics, the `/api/state` shape change) and section 6 for
   the per-symbol journal scoping. `schwab-connector` needed no structural
   changes — confirmed by reading it, not assumed, before writing any
   code. Proven live under real concurrent 4-symbol load, including a
   natural stream reconnect on one symbol not disturbing the other three,
   and a real virtual-position entry+exit on one symbol with the other
   three's journal state confirmed unchanged via direct DB inspection.
3. Event-triggered LLM narration via `claude-connector`, firing only on
   meaningful state changes (level hold-confirmed, volume threshold
   crossed, MACD cross, retest, sharp reversal) — never polled.
3.5. **(built, 2026-09-17) Multi-scenario setup evaluation.** See section
   3 (`core/setup_types.py`) for the four setup types and the
   dollar-distance comparison metric, and section 5 for the grid UI.
   Bullish/breakout-ABOVE direction only for this pass, deliberately — a
   symmetric breakdown-below version of each type is a natural future
   extension, not built now.
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
4. **(built)** Virtual trade journal — logs what the system would have
   done (entry, trailing stop) without placing anything, for end-of-day
   review against the user's own judgment. See section 6 for the full
   design — notably, no fixed target: a trailing stop only, by deliberate
   choice, not the "entry/stop/target" originally sketched here.
5. Anything beyond this point (more autonomy, live execution) requires
   its own explicit design discussion and is not assumed by this roadmap.

Do not build ahead of the current phase without an explicit instruction
to move to the next one.
