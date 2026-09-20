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

**Reconnect-storm incident, root cause now confirmed (2026-09-17) — the
`companion-auth` side of the mystery above.** The paragraph above left
`companion-auth`'s stale-cache behavior as "very likely," not confirmed,
since it's a separate repo/service out of this one's reach. Root-caused
tonight, from `companion-auth`'s own code plus its token file's mtime
(not a live occurrence — the earlier incident had already passed):
`companion-auth`'s `tokens.py` only re-hits Schwab once its cached
token is within `REFRESH_SKEW_SECONDS` of expiry; this repo's own
`token_source.py`'s `AccessTokenSource` (the "matching schwab-py's own
5-minute internal leeway convention" value referenced above) treats a
token as stale at `LEEWAY_SECONDS = 300`. `companion-auth`'s
`REFRESH_SKEW_SECONDS` sat at 60 — **below**, not above, this repo's
300 — creating a real ~240-second dead zone every ~30-minute cycle
where `token_source.refresh_async()` asks for a token it already
considers too-stale-to-use, and gets the identical not-yet-refreshed
one back, because `companion-auth`'s own narrower threshold doesn't
consider it due for a real Schwab round-trip yet. Confirmed precisely
via `companion-auth/data/tokens.json`'s mtime landing exactly on the
one request (of five, 60s apart) that actually changed — the other
four never touched the file.

This is the SAME failure signature as the storm above, and the
`ticks()` backoff fix from that incident is exactly what kept this
recurrence from becoming a second storm — it degraded to a slow,
rate-limited retry loop (one attempt per `auth_retry_seconds`, not a
tight zero-await loop) instead. Two things now exist specifically so
this class of incident stops needing after-the-fact mtime archaeology
to diagnose, both in `companion-auth` (a separate repo, not this one —
noted here because the *contract* this repo's `LEEWAY_SECONDS`
participates in belongs in both places' documentation, not just one):
(1) `tokens.py` now logs `access_token_cache_hit` /
`access_token_refreshing` / `access_token_refreshed` /
`access_token_refresh_failed` distinctly (previously: zero
application-level logging at all — `app.py` didn't even call
`logging.basicConfig()`, so none of this would have reached `docker
logs` regardless of what was logged); (2) `REFRESH_SKEW_SECONDS` raised
from 60 to 420 — 120 seconds of real margin above this repo's 300,
deliberately not an exact tie, with both a code comment and a
regression-guarding test in `companion-auth`'s own suite
(`test_refresh_skew_has_real_margin_over_known_consumer_leeway`)
tying it explicitly to this repo's `LEEWAY_SECONDS=300`. The general
principle, not just today's numbers: `companion-auth`'s cache-hit skew
must always stay comfortably above the LARGEST leeway any consumer
uses (this repo's 300s is the only known one right now) — if
`LEEWAY_SECONDS` here is ever changed, `companion-auth`'s
`REFRESH_SKEW_SECONDS` needs its margin re-checked by hand against the
new value, since the two repos have no shared import to enforce this
automatically.

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
  event-triggered narration. **Built, phase 3 stage 1, 2026-09-20 — see
  sections 25 and 27** (a `README`-only placeholder before that). A
  read-only bind mount of the host's `~/.claude/.credentials.json` (the
  ONE file the stage-0 investigation proved sufficient) is its only
  credential; `monitor-app` never shells out to `claude` itself and
  holds none of this.
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

**Entry, generalized to all four setup types + volume-gated (2026-09-17
— see below for the original, narrower phase-4 design this replaces).**
Fires on ANY of the four setup types' (resistance breakout, micro
breakout, VWAP pullback-reclaim, round-number reclaim — section 3.5)
OWN `hold.confirmed` False→True transition, tracked independently PER
TYPE (a set of confirmed setup-type strings, not one collapsed
boolean) — "closest" (`state["setups"][0]`, the one shown with the most
visual weight on the page) is `setup_types.evaluate_setups()`'s own
comparison metric for what to display/watch, not a requirement for a
confirmation to count as a real entry signal; a type sitting confirmed
from earlier does not block a DIFFERENT type confirming fresh later. If
more than one type transitions in the same tick, the closest wins (the
setups list is pre-sorted ascending by distance) — a deterministic
tie-break, not arbitrary. Also requires `relative_volume` (session-level,
`core/indicators.py`) to clear `VOLUME_CONFIRM_THRESHOLD` (env var,
default `1.5`) AT THE SAME MOMENT as the confirmation — a starting
point to tune against real logged data, same treatment as `TRAIL_PCT`,
not a validated number. Reasoning for `1.5`: high enough to filter the
specific false-breakout pattern volume confirmation exists to catch (a
low-conviction drift through a level on unremarkable volume), not so
high it requires an extreme spike that would filter out most real
breakouts too — `1.5` means the confirming bar's volume is 50% above
its own trailing 20-bar average, a moderate bar, not an extreme one. A
type that confirms on volume below threshold does not fire, and does
NOT get re-checked on a later tick while it stays confirmed with the
same unremarkable volume — the gate applies at the moment of
confirmation, not as a standing condition re-evaluated every tick.
Exits are deliberately NEVER volume-gated — same asymmetry
core/'s hold-confirmation has always used (entries need sustained
confirmation, now also real volume; stops fire fast and unconditionally,
no exceptions), applied here too, not a new rule. `entry_price` is the
close of the bar the transition is observed at (in practice: the latest
bar in the poll cycle where the transition is first seen — the finest
granularity available without re-running `evaluate_hold` per-bar inside
a single poll, which would itself be inventing new entry logic). At most
one open virtual position PER SYMBOL (phase 2: up to 4 symbols can each
have their own independently open position at once, not one global
position for whichever symbol happens to be watched); if a position is
already open for a symbol, a continued or repeated confirmed reading
for that same symbol does not fire a duplicate. `journal_logic.py`/
`journal_store.py` needed no changes for multi-symbol — audited
specifically for a single-global-position assumption and found none:
every lookup was already scoped by symbol (`open_position_for`) or by
the specific row id (`update_trailing`/`close_position`). The single-
position assumption that did exist lived in `Poller`'s own state, fixed
by giving each watched symbol its own `_SymbolSlot` (section 5).

`setup_type` (which of the four fired) and `factors` (a dict: the
type-specific detail from `SetupCandidate.factors`, plus `distance`,
`trigger_price`, and `relative_volume` at that moment) are captured on
the trade record at the instant of entry, not re-derived later from
whatever happens to be displayed — see "Schema" below.

**Original phase-4 design, superseded above (kept for history, not
current behavior):** entry fired exactly once per `hold.confirmed`
False→True transition on the nearest-above resistance level only
(`state["levels"]["resistance"]["hold"]["confirmed"]`) — written before
phase 3.5's multi-scenario `setups` evaluation existed, and never
generalized to the other three types until now. No volume condition
existed at all.

**A real bug found generalizing this (2026-09-17), fixed alongside it:**
`Poller._update_journal` dispatched a `JournalTick`'s `opened`/
`updated`/`closed` via `if`/`elif`/`elif` — meaning if a tick carried
BOTH a `closed` (a stop-out, mid-batch) AND an `opened` (a fresh entry
on a newly-confirmed type, later in the SAME batch of new bars), only
the `opened` branch ran and the close was silently never persisted to
`journal_store` — the closed position's row stayed open in SQLite
forever, orphaned, while `Poller`'s in-memory state had already moved
on to the new position. This shape existed in the ORIGINAL narrower
design too (`journal_logic.advance_journal` always computed `opened`
independently of `closed`), but the original resistance-only fixtures
never happened to produce it — generalizing entry to include
`round_number_reclaim` (which, being "always present," per section 3.5,
readily re-confirms on the very next round-number grid point right
after a stop-out) made it a real, reachable sequence, not a
hypothetical one. Fixed: `closed` is now applied unconditionally
whenever present, independent of whether `opened` is ALSO set on the
same tick (`opened`/`updated` stay mutually exclusive by construction,
per `journal_logic.py`'s own guarantee — only `closed`+`opened`
together needed the fix). Regression-tested directly
(`test_advance_journal_can_both_close_and_reopen_within_one_batch`,
`journal_logic.py`'s own test suite).

**Exit — trailing stop only, no fixed target, by design.** A fixed R:R
target was explicitly rejected for this project: it capped winners in the
EOD swing bot and contributed to that strategy's edge not holding up
under proper testing. There is no target anywhere in `journal_logic.py`,
by design, not by omission. (Section 21 later adds a purely
INFORMATIONAL reference-target display next to the open position —
never consulted by `journal_logic.py`, never a second exit mechanism —
this decision stands unchanged.)
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
open), `realized_pnl_pct` (nullable while open), `setup_type` (added
2026-09-17 — which of the four setup types fired; nullable, a position
opened before this existed has none), `factors` (added 2026-09-17 —
JSON-encoded dict of the factors behind that entry at the moment it
happened, see "Entry" above; nullable, same reason).

**Page/API.** `GET /api/state`'s JSON gains a `"journal"` key (open
position + live unrealized P&L%, plus up to 10 recent closed trades, all
symbols, most recent first); the HTML page gets a matching plain-table
section, same style as the existing levels tables — no new framework.
`entry_ts`/`exit_ts` (added 2026-09-17 — existed in the schema since
this section's first version, never shown until now) render human-
readable in the closed-trades table, America/New_York (the same
exchange-local timezone `state.py` already anchors session VWAP to —
see section 3 — not a new timezone convention invented for this), never
raw epoch.

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

**Journal reset point (2026-09-18, following the generalized/volume-
gated entry logic above).** Every row in `trades` as of this date —
including the then-open AEMD and AIFF positions — was opened under the
SUPERSEDED entry rule (resistance-breakout-only, no volume condition):
not meaningfully comparable to anything entered after this point, and
carrying it forward would silently mix two different strategies'
outcomes in the same table. Backed up, not deleted — `docker run --rm
-v .../monitor-app/data:/data alpine cp journal.db journal.db.pre-
generalized-entry-reset-20260918-034027Z.bak` (exact copy verified:
same row count, 18 total / 2 open, before the table was cleared) — kept
alongside the live file in the same `/data` volume, since keeping it
costs nothing and it's real history, just not history from the current
rules. `DELETE FROM trades` then emptied the live table entirely (open
positions included); `monitor-app` was stopped first and the deletion
done via a throwaway container against the same volume, specifically to
avoid a live process's in-memory `_SymbolSlot.journal_position` racing
against the DB changing underneath it. Confirmed clean on restart:
`GET /api/state` showed `"recent_closed": []` and `"journal": {"open":
null}` for all 4 re-watched symbols. Any row in the `.bak` file predates
generalized, volume-gated entries — read it as a record of the OLD
resistance-only rule, not as comparable performance data for the
current one.

### 7. Known gaps, identified 2026-09-18, not yet built

Recorded here as an explicit backlog, not fixed by this section's
existence — each is a real, named gap in either what gets recorded
about a trade or what the strategy itself accounts for, not yet closed.

**Data collection gaps:**
- ~~Catalyst/context note at add-time.~~ **Built 2026-09-18 — see
  section 9.** The original recorder design explicitly called this
  "impossible to reconstruct later" — it did not survive into the
  current web UI's plain "add symbol" box. This was the highest-priority
  gap: no trade record captured why a symbol was worth watching, only
  what happened technically.
- ~~Reverse-split history flag.~~ **Built 2026-09-18 — see section 10.**
  Proposed early, never built until now. Public, checkable data; would
  have been directly relevant on BIAF, QCLS, and RETO.
- ~~Continuation-vs-fresh-day flag (Day 1 gap vs. Day 2+ runner).~~
  **Built 2026-09-18 — see section 13.** Also proposed early, also
  never built until now — an informational flag, deliberately never a
  gate, reusing the daily-bars data section 12's volume gate already
  fetches rather than a second historical pull.
- ~~Market backdrop (broad-market direction that day).~~ **Built
  2026-09-19 — see section 23.** SPY's day change, global informational
  display, deliberately NOT wired into sizing (a real, explicitly
  deferred decision, not an oversight — see that section).
- ~~Human review/labeling.~~ **Built 2026-09-19 — see section 24.** The
  last of this list's data-collection gaps — closed trades only,
  `clean_signal`/`lucky`/`bad_signal`, plain mutable fields (no history
  table needed), extending the existing closed-trades table.

**Strategy gaps:**
- ~~Position sizing does not exist.~~ **Built 2026-09-18 — see section
  11.** The journal now tracks a real share count, real dollar P&L, and
  a compounding virtual account balance — not just entry/exit price and
  percentage.
- No portfolio-level risk cap across the 4 concurrent symbol slots.
  **Deliberately DEPRIORITIZED 2026-09-19 — see section 26.** A "loss"
  costs nothing real in this paper-trading context, so a limit protects
  against a risk that doesn't actually exist here; understanding whether
  the strategy LOGIC is trustworthy matters more right now, which is
  what section 26's evaluation view builds toward instead. Not built,
  not abandoned — still the next strategy gap once evaluation itself is
  trusted.
- ~~`TRAIL_PCT` was one global value despite volatility varying hugely
  across candidates.~~ **Partially addressed 2026-09-18 — see section
  13.** Section 8's live-tunable mechanism was the first step; section
  13 goes further for the early/pattern-forming part of a trade
  specifically (a swing-low-anchored stop, tighter and more structure-
  aware than a flat percentage) — still not per-symbol or volatility-
  adjusted, and the flat percentage still governs once a trade is
  established, so this gap isn't fully closed, just narrowed.
- ~~Breakdown-below variants of the four phase-3.5 setup types.~~
  **Built 2026-09-19 — see section 22.**

### 8. Live-tunable strategy parameters

**Principle, decided 2026-09-18.** Strategy parameters must be
live-tunable, not baked into code/env config. `TRAIL_PCT` living in
`.env` had the exact same failure shape as every stale-config incident
already hit in this project (see section 4's reconnect-storm and
companion-auth entries) — a value silently drifting out of sync with
intent, discovered late. Worse: real tuning based on accumulating trade
outcomes requires changing these often, and a redeploy cycle per change
discourages that. Every trade snapshots the actual parameter values in
effect at its own entry — never a live reference to "whatever's
current" — or it becomes impossible to later attribute an outcome to
the value that produced it.

**The build.** `TRAIL_PCT` and `VOLUME_CONFIRM_THRESHOLD` moved out of
env/code constants into `journal_store.py`'s SQLite file: a
`strategy_params` table (`key`, `value`, `updated_at`), seeded from the
env-derived values ONLY on the first-ever run against a given
`journal.db` (`JournalStore.__init__`'s `default_params` — an
already-tuned value already on disk is never reset back to the env
default on a later restart, since `main.py` passes the same defaults in
every single startup, tuned or not). `Poller._update_journal` reads
`journal_store.get_param(...)` on EVERY journal decision from then on,
never the env var directly — a change takes effect on the very next
decision, no redeploy. Every change is appended to a separate
`strategy_params_history` table (`key`, `old_value`, `new_value`,
`changed_at`) — never an in-place overwrite with no trail.

**Locked at entry, not re-read live, once a position is open.** This is
the subtle half of the requirement: `journal_logic.OpenPosition` gained
a `trail_pct` field, set ONCE from the live value at the moment of
entry and never touched again. `apply_bar_to_open_position` (the
ratchet + stop-breach check that runs on every bar for an open
position) reads `position.trail_pct` exclusively — never a freshly
looked-up global — so a parameter change made while a position is open
can never move that position's own stop math; only a brand-new entry
picks up the new value. `volume_threshold_used` is captured the same
way at entry (exits were never volume-gated to begin with, so it has no
ongoing use after entry — captured purely for the trade record).
`trades` gained matching `trail_pct_used`/`volume_threshold_used`
columns, migrated in place via the same `_ADDED_COLUMNS` mechanism the
setup_type/factors columns used (specs.md's own "assumed fresh, wasn't"
lesson, hit multiple times already — migrated explicitly again here,
not assumed away).

**API, deliberately minimal this pass.** `GET /api/strategy_params` →
`{"params": {key: {"value", "updated_at"}}, "history": [...]}`.
`POST /api/strategy_params` with one or more `{key: value}` pairs in
one call; each validated independently against a `(lower, upper]`
bound per key (`trail_pct`: `(0, 0.5]`; `volume_confirm_threshold`:
`(0, 20]` — an unrecognized key is rejected outright, not silently
accepted with no bounds check) — a rejected key changes nothing for
that key and the response reports exactly which keys succeeded vs.
were rejected, not an all-or-nothing failure over one bad value among
several. No settings UI this pass beyond a read-only current-values
line on the page itself (`#strategy-params`, updated live on every
push same as everything else) — the adjustment mechanism is API-only
(`curl`) for now; a proper settings UI is a natural, separate
follow-up once this core mechanism is proven.

**Verified live (2026-09-18), against a real running instance, both
directions.** Deployed to production monitor-app (no open positions at
the time, confirmed first — a safe restart, per this project's standing
discipline). Changed `trail_pct` via a real `POST /api/strategy_params`
call against the running production container: `0.05 -> 0.08`, took
effect immediately (`GET /api/strategy_params` reflected it with no
restart), and the change appeared correctly in `history` with the real
old/new values and a timestamp matching the call's actual wall-clock
time. Production has no live entries firing at this hour (market
closed), so the "a subsequent trade uses the new value" half was
verified fixture-driven against a SEPARATE, isolated instance of the
same unmodified production code (schwab-connector in `STREAM_SOURCE=
replay` mode + monitor-app, both real running processes, real HTTP API,
real SQLite file — same methodology as section 6's earlier live proof,
not a unit test): `POST /api/strategy_params {"trail_pct": 0.12}`
first, then `POST /api/watch {"symbol": "AEHL"}` — the resulting entry's
`stop_level` (8.008) was exactly `9.1 * (1 - 0.12)`, not `9.1 * (1 -
0.05)` (8.645, what the seed default would have produced), confirmed
both via the live API and a direct query against the real `trades` row
(`trail_pct_used=0.12`, `volume_threshold_used=0.5`,
`setup_type='round_number_reclaim'`). Then, with that position still
open, `POST /api/strategy_params {"trail_pct": 0.30}` again — the
open position's own `journal.open.stop_level` and the DB row's
`trail_pct_used` both stayed unchanged at the 0.12 value locked in at
its own entry, confirming the "not affected by subsequent parameter
changes" half live, not just at the unit-test level.

### 9. Catalyst/context notes at watch-time

**The gap, closed 2026-09-18.** Section 7's highest-priority item: no
trade record captured WHY a symbol was worth watching, only what
happened technically afterward. The original recorder design had
explicitly called this "impossible to reconstruct later," and it never
survived into the current web UI's plain "add symbol" box.

**Storage.** New `watch_notes` table (`journal_store.py`): `id`,
`symbol`, `note`, `created_at`. A NEW row every time a symbol is added
with a note or its note is explicitly updated — never a single mutable
field per symbol, since the reason for watching something can genuinely
differ across separate occasions and the history of past reasons has
value too (same append-only spirit as `strategy_params_history`,
section 8). `current_note_for(symbol)` returns the most recent row;
`watch_note_history(symbol)` returns all of them.

**Capture at add-time.** `POST /api/watch` gained an optional `note`
field in the SAME request (the existing add-symbol form on the page
gained a matching text input) — recording why is part of the same
action as adding the symbol, not a second step. Empty/omitted is valid
and normal, never blocks or slows the add; only an over-length note
(> `MAX_WATCH_NOTE_LENGTH` = 500 characters) is rejected, cleanly (409,
same convention as every other validation in this app), never silently
truncated. A rejected note also rejects the whole add — the symbol is
not watched with a note it was never given.

**Updating an already-watched symbol's note.** A separate small
endpoint, `POST /api/watch_note {"symbol", "note"}`, updates the note
for a currently-watched symbol without removing/re-adding it — context
often becomes clearer a minute or two after the initial add. Rejects an
unwatched symbol (nothing to attach the note to) or an over-length note
the same way. Same length limit, validated in `JournalStore.
add_watch_note` itself (`InvalidWatchNoteError`) so both callers (the
add-time path and this one) can never disagree about it.

**Display.** The current (most recent) note shows prominently on each
watched symbol's panel, right under the symbol/price header — in BOTH
the warming-up and normal-data states, since the note doesn't depend on
`core`'s analysis being ready. Plainly rendered as "no note recorded"
when empty, never just omitted, so a missing note is never confused
with "hasn't loaded yet." No dedicated edit-in-place UI widget this
pass (matching section 8's "adjustment mechanism can be API-only for
now" precedent) — updating an existing note is `POST /api/watch_note`
via `curl`, a UI control is a natural, separate follow-up.

**Snapshotted onto the trade at entry — the critical half, same
principle as `trail_pct_used` (section 8).** `journal_logic.
OpenPosition` gained a `watch_note` field, set ONCE from
`current_note_for(symbol)` at the exact moment `advance_journal` fires
a fresh entry, and never touched again — NOT a live reference to
`watch_notes`, which could change (an explicit update, or a re-watch
with different context) before anyone reviews this specific trade.
`trades` gained a matching `watch_note` column, migrated in place via
the same `_ADDED_COLUMNS` mechanism every prior schema addition this
session used (checked explicitly against a non-empty table, never
assumed fresh).

**Verified live (2026-09-18), against the real running production
container.** No open positions at deploy time (a safe restart). `POST
/api/watch_note {"symbol": "AEMD", "note": "watching for a low-float
reclaim above VWAP after the afternoon halt"}` against the real running
container — confirmed on the real rendered page and via `GET
/api/state` immediately. Updated it again (`"update: reclaim confirmed,
volume picking up"`) — confirmed the page and API reflected the new
text, with AEMD's `bar_count` (5246 and climbing) and watch status
completely undisturbed. Market hours were closed by this point, so the
trade-snapshot half was confirmed the same way as sections 6 and 8's:
a separate, isolated instance of the same unmodified code,
`STREAM_SOURCE=replay`. `POST /api/watch {"symbol": "AEHL", "note":
"original reason at entry"}` — the fixture cascaded through 3 real
entry/exit cycles (a genuine, not contrived, real-time replay outcome,
same round_number_reclaim cascade behavior documented in section 6);
all 3 closed trades' `watch_note` columns read `"original reason at
entry"`. Only THEN was `POST /api/watch_note` called with `"a
completely different later reason"` — confirmed live via the API that
the CURRENT note genuinely changed, while a direct query against the
real `trades` rows showed all 3 already-closed trades still reading
their original snapshot, unaffected — the exact requirement, confirmed
against real data, not simulated.

### 10. Reverse-split history flag

**The gap, closed 2026-09-18.** Section 7's next-priority data
collection gap: no way to flag that a candidate symbol has a history of
reverse splits — public, checkable information that would have been
directly relevant on BIAF, QCLS, and RETO, all real symbols this
project watched.

**Data source, decided explicitly (not silently assumed).** Checked
first, not assumed: Schwab's API (via `schwab-py`, already in use) has
no split-history data anywhere — `Client.Instrument`'s `FUNDAMENTAL`
projection covers PE ratios, market cap, volume averages, dividend
data, etc., but nothing about corporate actions. So this data has to
come from somewhere else, which is a real design decision, not an
implementation detail — put to the human explicitly rather than picked
silently, per this repo's own "when something is ambiguous" rule
(AGENT_PROTOCOL.md). Three options were on the table: (1) a manually
curated list, entered as splits are spotted; (2) a new external
corporate-actions API, which would mean a new dependency and possibly a
new credential to manage; (3) a heuristic scan of Schwab's own
long-lookback daily price history for anomalous single-day jumps, which
would be self-contained but imprecise and blind to anything outside the
lookback window or already smoothed by split-adjusted data. **Decided:
option (1), a manually curated list** — no new external dependency or
credential, and precise (an entered split is a known fact, not a
guess), at the cost of only covering what's actually been entered.

**Storage.** New `reverse_splits` table (`journal_store.py`): `id`,
`symbol`, `split_date`, `ratio`, `note`, `recorded_at`. A NEW row per
split event, never one mutable field per symbol — the exact low-float
names this flag targets (BIAF, QCLS, RETO) are also the names most
likely to split more than once in their lifetime, so the history of
past splits has to survive a later one being added, same append-only
spirit as `watch_notes`/`strategy_params_history`. `split_date` is
validated as ISO 8601 (`YYYY-MM-DD`) specifically because
`reverse_splits_for` sorts on it lexicographically DESC — a non-ISO
date would silently corrupt that ordering rather than just looking odd,
so it's rejected outright (`InvalidReverseSplitError`) instead. `ratio`
is free text (e.g. `"1:10"`) — real-world reverse splits get described
in more than one notation, and this flag's job is informing a human,
not feeding a calculation, so no calculator, no parsing, no bounds
check).

**Deliberately NOT snapshotted onto `trades`, unlike `watch_note` and
`trail_pct_used`.** Those two are locked onto `OpenPosition` at entry
because they're LIVE values that could drift out from under an
already-open position before anyone reviews the trade. A reverse split
is different in kind: it's an immutable historical fact about a
symbol's past, not a value anyone tunes or updates in place.
`reverse_splits_for(symbol)` answers "what splits happened before this
symbol's current price" correctly at any later point in time without
needing an entry-time lock-in — adding this snapshot field would have
been building a mechanism this gap doesn't actually need, not filling
one it does.

**Checkable before watching, not only after.** The flag's real value is
informing the decision to watch a symbol in the first place, not just
annotating it afterward — so `POST /api/reverse_splits` and
`GET /api/reverse_splits?symbol=...` both work for a symbol that isn't
currently watched at all, unlike `POST /api/watch_note` (which requires
an active watch, since it has nothing to attach an update to
otherwise).

**Display.** A warning line (`.reverse-split-flag`, styled with the
same warning color as the page's existing `.banner`) on a watched
symbol's panel, right under the watch-note line, in BOTH the
warming-up and normal-data states — the flag doesn't depend on
`core`'s analysis being ready, same reasoning as the watch-note's own
placement (section 9). Unlike the watch-note, which is always rendered
(even as "no note recorded") specifically because an empty note could
otherwise be confused with "hasn't loaded yet" — `reverse_splits_for`
has no such ambiguity: it resolves instantly and definitively, so an
empty result is genuinely nothing to show. Rendering "no known reverse
splits" on every panel, when most tickers never split, would be pure
noise for what's meant to read as a warning banner, not routine status.
Multiple splits for one symbol render together, most-recent-first, each
with its optional note. No dedicated add-a-split UI widget this pass —
API-only, same "natural, separate follow-up" precedent sections 8 and 9
both used for their own settings/edit UI.

**Verified live (2026-09-18), against a real running instance —
production itself was not restarted for this.** Checked first: at
verification time, production monitor-app had two real open virtual
positions (AEMD, AIFF) — this project's own standing discipline
(confirmed explicitly in sections 8 and 9) is to confirm NO open
positions before any restart, so production was left running
untouched. Verified instead against a separate, real, isolated
instance of the same unmodified code: `uvicorn main:app` against a
fresh `journal.db`, no `schwab-connector` needed (this feature has no
dependency on bar/stream data at all). `POST /api/reverse_splits
{"symbol": "QCLS", "split_date": "2023-01-10", "ratio": "1:4", "note":
"low-float reverse split, pre-runner"}` against a symbol NOT yet
watched, confirmed via `GET /api/reverse_splits?symbol=QCLS` before
anything else touched QCLS. A second split added the same way
(`"2024-05-02", "1:10"`, no note) — confirmed both survived as separate
rows. `POST /api/reverse_splits` with a non-ISO date
(`"05/02/2024"`) returned `409` with the real validation message and
changed nothing. Then `POST /api/watch {"symbol": "QCLS"}` (still
warming up, no bars) — the real rendered page already showed `⚠
Reverse-split history: 1:10 on 2024-05-02; 1:4 on 2023-01-10 (low-float
reverse split, pre-runner)`, most-recent-first, confirming the flag
renders before `core`'s analysis is ready, not just after. Finally, a
direct query against the real SQLite file (not the API) confirmed both
rows exactly as entered — real data, not simulated, same methodology
as sections 6, 8, and 9's own live proofs.

### 11. Position sizing with compounding virtual equity

**The gap, closed 2026-09-18.** Section 7's remaining strategy gap: the
journal tracked entry/exit price and percentage P&L only — no share
count, no dollar risk, no account-size concept, so a winning or losing
trade's real dollar significance (and whether a subsequent trade's size
should reflect prior wins/losses at all) was unanswerable from the data.

**Two new strategy_params, same mechanism as section 8, not a new
one.** `base_equity` (default 2000, the dollar "reset target") and
`risk_pct_per_trade` (default 0.01, the same 1%-of-account convention
the EOD swing bot used) joined `trail_pct`/`volume_confirm_threshold`
in the EXISTING `strategy_params` table — live-tunable via the same
`GET`/`POST /api/strategy_params`, the same `(lower, upper]` bounds
validation, the same append-only `strategy_params_history` trail. No
new table needed for the params themselves; building a second
parallel mechanism next to an already-proven one would have been
solving a problem that doesn't exist.

**`current_equity` is different in kind — a running value, not a
simple param — and needed its own build.** Unlike `trail_pct`, nobody
directly *sets* `current_equity` to whatever they want as the normal
case; it's supposed to move on its own, compounding off real trade
outcomes, with two DELIBERATE escape hatches for when a human needs to
intervene. That distinction drove the whole design:

1. **Trade close (automatic) — the ONLY path that moves it on its
   own.** On every REAL trade exit, `journal_store.close_position`
   computes `realized_pnl_dollars = shares * (exit_price -
   entry_price)` from the position's OWN locked-in `shares` (below),
   and — if that position ever had a real, computed share count —
   `app.py`'s `_update_journal` calls `apply_realized_pnl(trade_id,
   pnl_dollars)`, which reads `current_equity` fresh, adds the real
   dollar P&L, writes the new value, and appends an `equity_history`
   row with `reason = "trade_close:trade_id=<id>:pnl=<+/-X.XX>"`.
   Deliberately excluded from this path: a `symbol_switched`
   housekeeping force-close (`Poller.remove_symbol`, unwatching a
   symbol with an open position) — that's ALREADY treated everywhere
   else in this codebase as "watchlist housekeeping, not a trading
   outcome" (muted display, excluded from win/loss coloring, section
   6), and letting it silently move the virtual account balance based
   on whatever price happened to be current at the moment of an
   unrelated slot-management action would contradict that existing
   rule, not extend it. `remove_symbol` still calls `close_position`
   (the row still gets an honest `realized_pnl_dollars`, for the
   record), it just never reaches `apply_realized_pnl`.
2. **Reset (explicit action).** `POST /api/equity/reset` sets
   `current_equity` to the LIVE `base_equity` param — not a frozen
   2000 — logged as `"manual_reset"`, distinct from a trade-driven
   change.
3. **Manual override (explicit action, separate from reset).** `POST
   /api/equity/override {"value": X}` sets `current_equity` directly to
   any positive value, for correcting a mistake or deliberately
   starting from a different number, WITHOUT changing what a FUTURE
   reset targets (`base_equity` itself is untouched) — logged as
   `"manual_override"`, distinct from `"manual_reset"`. A non-positive
   value is rejected (`InvalidEquityOverrideError`, 409), changing
   nothing.

Storage: `equity_state` (a single row, `id` fixed at 1, the live value)
and `equity_history` (id, old_value, new_value, `reason`, changed_at) —
append-only like `strategy_params_history`, but with a `reason` field
in place of a bare key, since three DIFFERENT kinds of action produce a
change here and must stay distinguishable later, not just numbers with
no indication of which path produced them. Every write to
`equity_state` and its matching `equity_history` row happens inside one
function (`JournalStore._write_equity`), called with an `old_value`
read fresh, immediately beforehand, by the caller — structural, not
conventional, protection against two updates in the same batch
clobbering or double-counting each other (see "the concurrency proof"
below). Seeded from `base_equity`'s own seed value on the FIRST-EVER
run against a given `journal.db` only, same never-reset-on-a-later-
restart precedent as `strategy_params` itself.

**Explicit, load-bearing rule: `current_equity` is NEVER adjusted for
unrealized/open positions.** Only realized closes (path 1 above) move
it. Sizing a new entry while other positions remain open uses whatever
`current_equity` was as of the last REALIZED close, full stop — an
open position's paper gain or loss is not "spent" or "protected"
before it actually closes.

**Entry-time sizing.** `journal_logic.advance_journal` gained two
required parameters, `current_equity`/`risk_pct_per_trade` — required,
not defaulted, same treatment as `trail_pct`: sizing math has no
meaningful zero-effort default the way an optional `watch_note` does.
Both are read fresh by `app.py`'s `_update_journal` on EVERY journal
decision (never cached), and used ONLY when a brand-new entry fires:
`risk_amount = current_equity * risk_pct_per_trade`; `risk_per_share =
entry_price * trail_pct` (the dollar distance from entry to the
initial stop — algebraically the same distance `initial_stop_level`
itself computes); `shares = floor(risk_amount / risk_per_share)` —
rounded DOWN, never up, since overshooting the risk budget on a
rounding technicality would defeat the purpose of sizing by risk at
all. `risk_amount_used = shares * risk_per_share` is the REAL dollar
amount that rounded share count risks, recorded instead of the
theoretical `risk_amount` target, which it can differ slightly from.
All four — `shares`, `account_size_used` (the `current_equity` reading
itself), `risk_pct_used`, `risk_amount_used` — are LOCKED onto
`OpenPosition` at the moment of entry, same snapshot-at-entry
discipline as `trail_pct_used` (section 8): an already-open position's
sizing is never recomputed on ratchet, and a LATER trade's sizing
(reading a `current_equity` this one's own close may since have moved)
can never retroactively change what a given trade's own record says it
used.

**The zero-share edge case is real, not hypothetical, and stays
visible.** An expensive stock, a tight stop, or a small `current_equity`
can legitimately round `shares` down to 0. The trade still enters and
still logs — this journal's job is learning what signals look like,
not just what's executable — but `shares=0` is never silently
indistinguishable from a real position: both the open-position block
and the closed-trades table render a `zero-size` flag next to it
wherever it appears. Distinct, on purpose, from `shares=NULL` (a
pre-migration position whose sizing was never computed at all, or a
`symbol_switched` row's honest-but-`None` dollar figure when shares
were never known) — 0 is a real, meaningful, computed value; `NULL`
means "never computed." `close_position` reflects this exactly:
`realized_pnl_dollars` is `0.0` (a real, well-defined, still-logged
change) for a genuine zero-share trade, and `None` (never a fabricated
number) only when `shares` itself was never known.

**Real dollar P&L, now displayed.** With a real share count existing,
both the open-position block (`unrealized P&L $`, alongside the
existing `%`) and the closed-trades table (`P&L $`, a new column next
to the existing `%`) show it — more honest for review than percentage
alone once size is known. `current_equity` itself shows prominently
above the existing strategy-params line (`#current-equity`, refreshed
live on every push same as everything else), and `base_equity`/
`risk_pct_per_trade` show alongside `trail_pct`/
`volume_confirm_threshold` on that same existing line, automatically —
no separate rendering code needed, since it already iterates every key
`strategy_params` reports. API-only for the actual reset/override
actions this pass (`POST /api/equity/reset`, `POST
/api/equity/override`) — same minimal-first-pass precedent sections 8
and 9 both used; a settings UI is a natural, separate follow-up.

**Migration.** `trades` gained `shares` (INTEGER), `account_size_used`,
`risk_pct_used`, `risk_amount_used`, and `realized_pnl_dollars` (all
REAL), migrated in place via the same `_ADDED_COLUMNS` mechanism every
prior schema addition this session used — an existing open position or
closed trade from before this feature has `NULL` in all five, never an
invented value. `equity_state`/`equity_history` are new tables
(`CREATE TABLE IF NOT EXISTS`), proven explicitly against a database
file that predates them entirely, same discipline as every prior
schema addition.

**The concurrency proof — specifically testing the same class of bug
already found once in this project.** Part A of setup-type
generalization (specs.md section 6's build) found a real bug where
`app.py`'s `if`/`elif`/`elif` across opened/updated/closed silently
dropped a `close_position()` write whenever a stop-out was immediately
followed, within the same batch, by a fresh entry. `current_equity`'s
own update path is a NEW place the same class of bug — a same-batch
write silently lost or double-counted — could recur, so it was tested
for directly, not just assumed fixed by the structural guard above. A
test drives TWO DIFFERENT symbols' real trade closes through the SAME
`resync_all()` call (this codebase's actual "same processing batch"
shape: `resync_all()` awaits `catch_up()` for every watched symbol in
turn, all before the caller sees a response) and confirms
`current_equity` reflects the SUM of both realized P&L amounts,
applied sequentially — the second close's own `equity_history` row
shows `old_value` equal to what the FIRST close had already moved
`current_equity` to, never the original starting value twice over, and
never just one of the two losses alone. Passes at both the storage
primitive itself (`JournalStore.apply_realized_pnl` called twice, back
to back) and at the full Poller-wiring level (two real symbols, real
`core/` hold-confirmation transitions, real SQLite file).

**Incident (2026-09-18, found and fixed same-day): a one-close lag in
same-batch reopen sizing.** The FIRST live verification pass (below)
reported `account_size_used` of 2000.0, 2000.0, 1992.845, 2047.52 for
the 3 closed + 1 open `AEHL` trades, and was initially written up as
correct. It wasn't: caught by independent arithmetic cross-check
against the `equity_history` chain (2000.0 → 1992.845 → 2047.52 →
2097.295) — given the single-position-per-symbol guard forces strict
entry→close→entry ordering, every entry AFTER the first should have
shown the equity level as of ITS OWN entry, not the PRIOR entry's
level. A one-close lag, not random error — exactly the shape of a
caching/staleness bug, not a one-off arithmetic mistake.

**Root cause, confirmed with fresh instrumented evidence, not
inferred.** `app.py`'s `_update_journal` read `current_equity` ONCE,
before calling `advance_journal` — but that SAME call can both close
the existing position and size a fresh reopen in one shot (a stop-out
immediately followed by `round_number_reclaim` re-confirming, the
exact shape `test_advance_journal_can_both_close_and_reopen_within_
one_batch` already covered for entry mechanics, but never for
sizing). The close's real dollar P&L only reached `current_equity` in
storage AFTER `advance_journal` returned (`_update_journal`'s own
`tick.closed` handling, which runs after `tick.opened`'s sizing was
already computed and frozen). Confirmed live, not just by re-reading
the code: temporary instrumentation logged `account_size_used_baked_in`
against `live_equity_right_now` at the exact instant of each same-batch
reopen, re-run against the same real replay fixture —
`account_size_used_baked_in=2000.0` vs. `live_equity_right_now=
1992.845` for the second entry, `1992.845` vs. `2047.52` for the third,
`2047.52` vs. `2097.295` for the fourth — caught in the act, three for
three, then reverted (this was a diagnostic, not a fix).

**The fix.** `journal_logic.advance_journal` now computes an
`effective_equity` for sizing a same-batch reopen: `current_equity`
adjusted for the JUST-closed position's own realized P&L
(`closed_position.shares * (exit_event.exit_price -
closed_position.entry_price)`), using data already fully available in
that same call — no I/O needed, since it's the identical arithmetic
`journal_store.close_position` itself uses. A closing position whose
`shares` was never computed (a pre-migration position) contributes no
adjustment, same "`None` means skip, never invent a number" convention
used everywhere else in this feature. A new regression test
(`test_advance_journal_reopen_in_the_same_batch_sizes_off_post_close_
equity`) locks this in at the pure-logic level, independent of the
live replay.

**Verified live again (2026-09-18), against a fresh replay run, with
the fix in place — production still not restarted (same two open
positions as before).** Same real isolated `schwab-connector`
(`STREAM_SOURCE=replay`) + `monitor-app` pair, same
`fixtures/replay_sample.jsonl`. The replay produced 3 real closed
trades and left a 4th open, all against `AEHL`: `shares` of 54, 54,
57, then 53 (note: 57/53 differ from the pre-fix run's 55/52 — the fix
changes the ACTUAL sizing, not just its bookkeeping, since a
same-batch reopen's real risk budget changes too). Direct query
against the real `trades` rows confirmed every single one's
`account_size_used` matches `current_equity` as of THAT position's own
entry moment with NO lag: trade 2's `account_size_used` (1992.845)
equals `equity_history`'s row for trade 1's close (`new_value`);
trade 3's (2047.52) equals trade 2's close; trade 4's (2099.105) equals
trade 3's close. `current_equity` after all three closes was
`2099.105` — confirmed exactly equal to `2000 + (-7.155) + 54.675 +
51.585`, the real `realized_pnl_dollars` sum (the third trade's own
P&L also changed, 49.775 → 51.585, a direct consequence of it having
been sized off the CORRECT, larger post-close equity this time).
`equity_history` held exactly 3 rows, most-recent-first, each
`old_value`/`new_value` pair chaining correctly with zero gaps —
confirmed against real, non-contrived cascading data, not simulated,
and specifically checked for the exact defect just fixed, not just
re-run and eyeballed.

**Reset/override, verified live (2026-09-18) — unaffected by the
incident above (neither reads/writes mid-batch), re-confirmed
regardless.** `POST /api/strategy_params {"base_equity": 5000}`
followed by `POST /api/equity/reset` moved `current_equity` to exactly
`5000.0` (the LIVE param, not the 2000 seed), logged `"manual_reset"`;
`POST /api/equity/override {"value": 750}` moved it to `750.0`, logged
`"manual_override"`, distinct from the reset; `POST /api/equity/
override {"value": -5}` returned `409` and changed nothing, confirmed
via a follow-up `GET /api/equity` still reading `750.0`. The rendered
page (fetched live, not assumed from the template) showed `Current
equity: $750.00`, `base_equity=5000.0000` and
`risk_pct_per_trade=0.0100` on the strategy-params line, and each
closed row's real `shares`/`P&L $` columns alongside their existing
percentages — confirmed in the server-rendered markup itself, not
just the JSON API.

### 12. Pattern-anchored early stop + session-level volume gate

**The gap, closed 2026-09-18.** Section 6's original trailing-stop
design (a single flat `TRAIL_PCT` below the running high-water-mark for
a position's ENTIRE life) considered, and explicitly did not choose, an
alternative anchor: the most recent CONFIRMED higher-low, tracked via
real swing-point structure rather than a fixed percentage. That option
is built now, scoped specifically to a trade's early, pattern-forming
phase — not a replacement for the proven flat trail, which still
governs once a trade has shown real progress. Alongside it: a
session-level volume gate, a SEPARATE check from the existing bar-level
`VOLUME_CONFIRM_THRESHOLD` (section 6), requiring today's cumulative
session volume to clear a multiple of the symbol's typical daily
volume before an entry fires at all.

**Part 1 — two-phase exit.** `OpenPosition.exit_phase` (`journal_logic.
py`) is `"swing_low"` or `"trailing"`, defaulting to `"trailing"` on the
dataclass itself — deliberately the ORIGINAL, single-phase behavior,
not the new early phase: a position built without opting into this
feature (every pre-existing test fixture, and a resumed pre-migration
open position) must ratchet exactly as it always has, unaffected by
this feature's mere existence. Only `advance_journal`'s own real
entry-construction path sets `"swing_low"` for a genuinely new trade —
the same safe-default-vs.-real-opt-in split `trail_pct`/`shares`/etc.
already use.

- **Phase 1, `"swing_low"`.** Stop anchors to the LOWEST confirmed
  swing low since entry — reusing `core/levels.confirmed_swing_lows`
  (new, built on the existing `_swing_points` primitive `detect_levels`
  already used for resistance/support — not a new detection algorithm),
  applied to the position's own bar history since its entry, buffered
  down by `swing_low_buffer_pct`. Taking the MINIMUM across every
  confirmed low seen so far — not just the most recent — is what makes
  "the stop never moves up in this phase" true automatically: the set
  of confirmed lows only grows as more bars arrive, so its minimum can
  only fall or stay put. Before any swing low has confirmed yet (too
  early in the trade for `_swing_points`' own window-based confirmation
  delay — the exact same delay `detect_levels` already imposes, not a
  new one), the anchor falls back to the entry-trigger level actually
  broken to enter this trade (`OpenPosition.factors["trigger_price"]`),
  buffered the same way — an explicit, non-arbitrary interim value,
  never a raw crash or an invented number. `swing_low_buffer_pct`
  defaults to 0.005 (0.5%): enough to absorb a typical wick-through-
  the-exact-low without meaningfully widening the stop, small enough
  that it's still anchored to a REAL level, not a guess.
- **Phase 2, `"trailing"`.** The original, unchanged flat
  `trail_pct`-from-high-water-mark mechanism (section 6), unaffected.
- **Transition, one-way, checked precisely per-bar (not the swing-low
  anchor's own batch-level granularity — see below).** Once
  `high_water_mark` clears `entry_price * (1 +
  pattern_progress_threshold_pct)`, `exit_phase` becomes `"trailing"`
  and never reverts, even on a later pullback.
  `pattern_progress_threshold_pct` defaults to 0.03 (3%): comfortably
  past normal intrabar noise/spread on these volatile low-priced
  candidates, while still handing off early enough that most real
  winners actually reach phase 2 — the whole point of having a proven
  fallback mechanism at all. `high_water_mark` itself keeps ratcheting
  every bar UNCONDITIONALLY, regardless of phase — needed the instant
  phase 2 begins, and it's how "real progress" is measured in the first
  place.
- **Batch-level granularity for the swing-low anchor, by design, not
  oversight.** `Poller._update_journal` computes the anchor ONCE per
  poll cycle — the position's full bar history since entry, sliced from
  `slot.bars`, fed to `confirmed_swing_lows` — and passes that single
  value through the whole ratchet loop for that batch, the same
  granularity tradeoff this codebase already accepted for
  `round_number_reclaim`'s own entry timing (section 6: "the finest
  granularity available without re-running `evaluate_hold` per-bar
  inside a single poll cycle, which would be inventing new entry
  logic"). The PHASE TRANSITION check, by contrast, needs no swing-low
  data at all (just `entry_price`/`high_water_mark`/the threshold
  already on the position) and is checked exactly per-bar.
- **A real bug found and fixed while building this, not by inspection
  — `_phase1_anchor`'s clamp.** `entry_price` is the CONFIRMING bar's
  own close (section 6's existing "finest granularity" rule); but
  `evaluate_hold`'s "once confirmed, a single close back through
  doesn't retroactively un-confirm history" rule (`core/levels.py`)
  means the level that triggered confirmation can sit ABOVE the price
  the position actually enters at, if price pulled back between
  confirming and the entry bar itself. Confirmed directly against real
  `setup_types.evaluate_setups` output, not assumed: `round_number_
  reclaim` confirmed with `trigger_price=9.25` while the confirming
  bar's own close was `9.1`. An unclamped anchor there would price
  phase 1's "protective" stop ABOVE the entry itself — a
  near-guaranteed immediate stop-out, defeating the entire point of a
  two-phase exit. `_phase1_anchor(candidate_anchor, entry_price)` —
  `min(candidate_anchor, entry_price)` — is applied in BOTH the
  entry-construction and ratchet code paths, so this invariant ("phase
  1's stop is never above entry") holds regardless of which anchor
  source (the trigger-price fallback, or a genuinely confirmed swing
  low) produced the candidate.
- **Snapshot at entry, same "don't lose the reasoning" discipline as
  `trail_pct_used`/`watch_note`.** `trades` gains `exit_phase`,
  `swing_low_buffer_pct_used`, `pattern_progress_threshold_pct_used`
  (all locked in once, at entry, never re-read live for an open
  position — a parameter change never reaches it, same precedent as
  every prior live-tunable param) and `phase_transitioned_ts` (the ts
  of the bar the transition happened at, `None` while still in phase 1
  or for a position that never transitioned) — `JournalStore.
  update_trailing` now persists `exit_phase`/`phase_transitioned_ts`
  too, not just `high_water_mark`/`stop_level`, since the one-way
  transition happens mid-trade, on a ratchet, and must survive a
  restart the same way the rest of an open position's live state
  already does.
- **Migration is NOT the usual "`NULL` reads back as the dataclass
  default" story.** A pre-migration OPEN position, resumed after this
  feature ships, was ALREADY using the original flat-trail-only
  mechanism the whole time it's been open. `_row_to_position` explicitly
  reads a `NULL` `exit_phase` column back as `"trailing"` — which
  happens to equal the dataclass field's own default here, but for a
  DIFFERENT, deliberate reason (never retroactively drop an in-flight
  trade into a phase it was never actually in), not because that's
  merely the fallback value like every other nullable numeric column in
  this schema.

**Part 2 — session-level volume gate.** A new, additional entry
condition in `should_enter` (`journal_logic.py`): today's cumulative
session volume must clear `avg_daily_volume * session_volume_multiple`
— stacking with, never replacing, the existing bar-level
`relative_volume`/`volume_confirm_threshold` check. `session_volume_
multiple` defaults to 3.0, the user's own stated criterion, not a
guess. `state.build_state` exposes `session.cumulative_volume` (summed
from the SAME `session_bars_for_vwap` slice VWAP already uses, not a
separately-invented one).

- **"Typical daily volume" needed data this project didn't have.**
  Checked first, not assumed: neither the existing same-day intraday
  backfill (`schwab-connector/price_history.py`'s `fetch_today_bars`)
  nor anything else already fetched covers a multi-week daily lookback.
  A new `fetch_daily_history` (same module) requests DAILY candles over
  an explicit date range ending at today's own NY midnight —
  EXCLUSIVE of today's still-forming volume, and the same explicit-
  range discipline `fetch_today_bars` already established (`period_
  type=DAY` was confirmed, live, to silently return the wrong range —
  see section 4 — so this reuses the fix, not the trap). Exposed via a
  new, deliberately thin, on-demand REST pass-through — `GET
  /daily_bars/{symbol}` — no caching or `BarStore` involvement at the
  schwab-connector layer at all, same division of labor as `GET
  /bars/{symbol}`: schwab-connector fetches and serves raw Schwab data,
  monitor-app owns the strategy-level business logic (the average, the
  gating threshold) on top of it.
- **Fetched once per symbol, at watch-time, cached — never per-bar.**
  `Poller.add_symbol` calls the new `fetch_daily_bars` dependency
  (mirroring `fetch_bars`'s own injection pattern) once, right after
  creating the symbol's slot, and caches the average on `_SymbolSlot.
  avg_daily_volume` for that symbol's whole watch lifetime.
- **Missing data SKIPS the gate — an explicit, documented choice, not
  a silent one.** A fetch failure (network error, or schwab-connector's
  own `GET /daily_bars/{symbol}` returning 503/502 when unconfigured or
  itself failing) or an empty result (a symbol too new to have daily
  history yet) leaves `avg_daily_volume` as `None`, logged as a
  warning, never a crash. `should_enter` treats `avg_daily_volume=
  None` as "skip this gate entirely" rather than "block every entry" —
  deliberately chosen over the alternative (block when data is
  missing): this tool's own stated purpose is watching volatile,
  often very-recently-listed small caps, exactly the names most likely
  to lack multi-week daily history at all; blocking on missing data
  would make the tool non-functional for its own primary use case. The
  existing bar-level `relative_volume` gate still applies regardless,
  so a missing daily-history fetch never removes volume screening
  entirely, only this one additional, session-level layer of it.
- **Snapshotted at entry into `factors`** (`session_cumulative_volume`,
  `avg_daily_volume`, `session_volume_multiple_used`) — reusing the
  EXISTING JSON `factors` column (no new `trades` columns needed),
  same "why did this trade happen" discipline `relative_volume` already
  gets there. `avg_daily_volume=None` in a trade's own snapshot means
  the gate was SKIPPED for that specific trade, distinct from a real
  ratio that happened to pass — never silently indistinguishable.

**Verified live (2026-09-18) — against a real isolated pair, both
halves, checked against real DB rows and real API responses, not
simulated. Production was not restarted** (three real open positions —
AEMD, AIFF, DTSS — at verification time; per this project's own
standing discipline, confirmed every time this session, left running
untouched). Same real `schwab-connector` (`STREAM_SOURCE=replay`,
`fixtures/replay_sample.jsonl`) as every prior live proof this session.
No real Schwab credentials are available in this dev environment for
the daily-history REST call specifically — verified instead with a
small entry-point script, identical to the real `main.py` in every
other respect (real HTTP calls to schwab-connector for bars/watch/
events, real `journal_store.py`/SQLite, real `Poller`/`journal_logic`
code, unmodified), with `fetch_daily_bars` stubbed at the EXACT same
injection seam `main.py` itself uses for it — a controlled substitute
for the one genuinely-external boundary, the same principle already
established for `STREAM_SOURCE=replay` replacing the Schwab-stream
boundary itself.

Two full runs of the same real 32-bar cascading replay, from a fresh
`journal.db` each: with `avg_daily_volume=50,000,000` (session_
cumulative_volume would need to clear 150,000,000), the replay ran to
completion (`bar_count=32`) with the real hold-confirmed transitions
this fixture is known to produce — and a direct query against the real
`trades` table confirmed exactly ZERO rows, not merely `GET /api/state`
showing none. With `avg_daily_volume=50,000` (needs only 150,000,
comfortably cleared by the real, computed `session_cumulative_volume`
of 1,501,000), a real position opened — confirmed via a direct `trades`
row query showing the full real snapshot: `entry_price=7.86`,
`setup_type=vwap_reclaim`, `trigger_price=7.8572` (below `entry_price`,
so `_phase1_anchor` doesn't clamp here), `exit_phase='swing_low'`,
`swing_low_buffer_pct_used=0.005`, and `factors` holding the real
`session_cumulative_volume=1501000.0`/`avg_daily_volume=50000.0`/
`session_volume_multiple_used=3.0` together. `stop_level` (7.8179)
confirmed to equal `7.8572 * (1 - 0.005)` exactly. The rendered page
(fetched live) showed `stop phase: swing-low anchored (early)` on that
open position's own panel, and `swing_low_buffer_pct=0.0050`/
`pattern_progress_threshold_pct=0.0300`/`session_volume_multiple=
3.0000` on the strategy-params line. Live-tuning verified both
directions against that same running instance: `POST /api/
strategy_params {"session_volume_multiple": 10.0, "swing_low_buffer_
pct": 0.02}` took effect immediately (confirmed via `GET`, with the
real old/new values in `history`) — while the ALREADY-OPEN position's
own `stop_level` (7.8179) and the real `trades` row's `swing_low_
buffer_pct_used` (still `0.005`, not `0.02`) stayed completely
unchanged, confirming the "locked at entry, a live change never
reaches an open position" requirement live, not just at the unit-test
level.

The swing-low anchor actually REPRICING a stop mid-trade (a confirmed
low replacing the entry-trigger fallback) and the one-way phase
transition were verified at the wiring-test level instead of via a
second live run — `test_a_real_confirmed_swing_low_reprices_the_
phase1_stop` and `test_a_parameter_change_after_entry_does_not_
affect_the_open_positions_ratchet` (`test_journal_wiring.py`) — real
`Poller`/`journal_logic`/`JournalStore(tmp_path)` code, real `core/`
hold-confirmation and swing-point detection, real SQLite, driven
through `TestClient` rather than a second separate OS process; the
32-bar replay fixture's own real cascade doesn't hand-tunably produce
these two specific shapes (a controlled post-entry pullback, and a
controlled progress-threshold clearance) the way a purpose-built bar
sequence does, and building a live-process variant of the same proof
would exercise identical code to what these wiring tests already do
end-to-end. Building the swing-low wiring test itself is what
surfaced the `_phase1_anchor` clamp bug above — the first attempt used
a swing low ABOVE `entry_price`, silently clamped to `entry_price` by
the (correct) new logic, numerically indistinguishable from the
untouched fallback until the fixture was corrected to a genuine
below-entry pullback.

### 13. Continuation-vs-fresh-day flag

**The gap, closed 2026-09-18.** Section 7's remaining data-collection
gap: no way to tell, at a glance, whether a candidate is a genuine Day
1 (fresh) setup or already several days into a run — real examples
from this project's own candidates (RETO, QCLS, DLXY) moved 100%+ on
their actual runner days, and whether today's setup is the FIRST such
move or a later continuation of one is exactly the kind of context
that shaped the original journal design's intent but never survived
into a built feature.

**Reuse, not a second fetch — checked first, not assumed.** Section
12's session-level volume gate already fetches a symbol's daily-bar
history once, at watch-time, to compute `avg_daily_volume`. Before
building anything new, confirmed directly (not assumed) that only the
COMPUTED AVERAGE survived that call — the raw daily bars were fetched,
used, and discarded. `_SymbolSlot.daily_bars` now retains that raw
series (added to `add_symbol`'s existing fetch, not a second one), so
this flag is pure computation over data already in hand — zero new
network calls, confirmed live via a call-count assertion (below), not
just working code.

**Design: informational, never a gate — the opposite treatment from
section 12's volume requirement.** `journal_logic.should_enter`/
`advance_journal` take NO continuation-related parameter at all — not
"a parameter that happens to always pass," genuinely absent from the
function signatures, so there is no code path through which this flag
could ever block or influence an entry, structurally, not just by
convention. Same treatment as the reverse-split flag (section 10):
context for a human to weigh, never baked into strategy logic.

**Computation** (`core/indicators.continuation_days`, new, pure — no
I/O, reused the same way `confirmed_swing_lows` reuses `_swing_points`
rather than inventing a new detection primitive per feature). For each
of the most recent `continuation_lookback_days` trading days, compares
day-over-day % change (close vs. prior close, SIGNED — a big move
either direction, not just up) against `continuation_threshold_pct`
(a fraction, the same "_pct" convention every other strategy param in
this project already uses). Both join the EXISTING `strategy_params`
mechanism (section 8) — live-tunable, same validation/history
discipline as every other threshold here; NEVER snapshotted onto
`trades`, unlike every entry-time-locked value this project has built
so far, since the flag is meant to always reflect the CURRENT live
threshold whenever a symbol's panel is viewed, not a value frozen at
some past moment.

- `continuation_lookback_days` defaults to 7 (about a trading week):
  long enough that a runner day's immediate aftermath — the "Day 2,
  Day 3..." continuation window this flag exists to distinguish from a
  genuine Day 1 — is still caught, short enough that a move from weeks
  ago has stopped being relevant context for TODAY's setup.
- `continuation_threshold_pct` defaults to 0.5 (50%): the cited real
  examples (RETO, QCLS, DLXY) moved 100%+ on their actual runner days,
  but ordinary daily noise for a volatile small/micro cap can itself
  run into the 10-30% range on an unremarkable day — 50% sits
  meaningfully above that noise floor without requiring the most
  extreme outcomes only to register as "not a normal day."

**Three distinct states, not a binary flag.** `Poller.
_continuation_status_for` returns `"unknown"` (`daily_bars` is empty —
the fetch never happened, or failed), `"fresh"` (data exists, nothing
in the window qualified), or `"continuation"` (one or more days did) —
`"unknown"` is never conflated with `"fresh"` the way a bare boolean
would collapse them into the same false signal. Recomputed fresh on
every state read, not cached — cheap (a handful of comparisons over an
already-in-memory list) and means a live threshold change is reflected
immediately for every currently-watched symbol, with nothing to
invalidate.

**Display: the actual detail, not a bare flag, and ALWAYS rendered —
the opposite of the reverse-split flag's "only when non-empty"
treatment, and deliberately so.** "Day 1, fresh" is exactly as useful
to see at a glance as "continuation, ran +112% on 9/16" — the absence
of a flag is itself the answer, unlike a reverse split (rare, and
noisy to show as "none" on every ordinary panel). Shows every
qualifying day, most-recent-first inherited from bar order, each with
its real signed magnitude and date (`_fmt_date`, date-only — a daily
candle's time-of-day is noise a real entry/exit timestamp isn't).

**Verified live (2026-09-18) — against a real isolated pair, checked
against real API responses and a real call-count log, not simulated.
Production was not restarted** (three real open positions — AEMD,
AIFF, DTSS — at verification time; left running untouched, this
project's own standing discipline, confirmed every time this session).
Same real `schwab-connector` (`STREAM_SOURCE=replay`) as every prior
live proof, with a small entry-point script identical to `main.py` in
every other respect, `fetch_daily_bars` stubbed at the same seam as
section 12's own verification (no real Schwab credentials for daily
history in this dev environment) — with the stub logging its own call
count this time, specifically to prove reuse.

**A first verification pass used the stub's own loop index (0..7) as
each daily bar's `ts`, not a real date — caught, not shipped: the
rendered date came out `12/31` (`ts=3` is `1969-12-31 19:00:03 EST`,
the standard "small-integer-as-Unix-epoch" artifact), which is a
completely ordinary shortcut for testing the pure day-over-day
comparison logic (the loop index is never fine for a LIVE-verification
demonstration meant to show what a real user would actually see, and
was corrected before this section shipped, not after). Traced to
confirm it was ONLY this stub's own shortcut, not a bug in the shared
`schwab-connector/price_history.py` fetch path both this flag and
section 12's volume gate depend on: that module's real `candles_to_bars`
(`ts = int(dt) // 1000`, a plain epoch-ms-to-seconds conversion) is
pre-existing code, unchanged by this session, already covered by its
own test asserting a REAL 2025-09-03 epoch-ms value maps back to the
exact matching real date — and was itself verified live against
production Schwab data before this session even began (section 4: a
real NVDA backfill, 775 real bars). `avg_daily_volume`'s own math never
even reads `ts` at all, only `volume` — so section 12's already-
deployed calculation was never at risk either way. The verification
below was re-run using genuinely realistic dates (real September 2026
calendar dates, each converted to its own real epoch second) before
this section was marked done.

Two runs from fresh `journal.db` files, using real dates 2026-09-08
through 2026-09-17 (a real Tuesday-through-Thursday span, skipping the
weekend): with daily closes producing a real +112.1% day on 2026-09-11,
`GET /api/state` showed `{"status": "continuation", "days":
[{"ts": 1789133400, "pct_change": 1.121...}], ...}` (`1789133400` is
exactly `2026-09-11 09:30:00 America/New_York`, confirmed independently
via direct epoch-to-date conversion, not just trusted), the rendered
page showed `Continuation — +112.1% on 09/11`, and the fetch-call log
showed exactly `call_count=1` for the symbol — the SAME single fetch
that also populated `avg_daily_volume`. With ordinary-noise daily
closes over the same real date range (no day exceeding 50%), `GET
/api/state` showed `{"status": "fresh", "days": [], ...}`, the
rendered page showed `Day 1 (fresh) — no moves over 50% in the past 7
trading days`, and the fetch-call log again showed exactly
`call_count=1`. Both runs, against the SAME real cascading replay
fixture, produced the identical real trade count (5,
confirmed by direct query against each run's own `trades` table) —
live, direct proof that continuation status never affects entry
behavior, not merely that `should_enter`'s signature has no parameter
for it.

### 14. Time-aware core functions — phase 3.6, stage 1 (equivalence only)

Motivated by section 3's "Backfill vs. live bar width" and roadmap item
3.6 below: `ema`/`relative_volume`/`evaluate_hold`/`detect_levels`'s
swing-point window all currently decay/compare/require by BAR COUNT, an
assumption that only holds while every bar is the same width. Live bars
are exactly 10s apart (`schwab-connector`'s aggregator, `BUCKET_SECONDS
= 10`); backfilled/daily bars are coarser and irregular, and even the
backfilled portion has its own internal gaps (Schwab skips zero-volume
minutes). `state.py`'s `live_cadence_tail` stopgap works around this
today by excluding backfilled bars from these functions entirely, rather
than weighting them correctly.

**Stage 1's entire job, and nothing more:** build new, distinctly-named,
purely additive time-aware functions in `core/` and prove they behave
EXACTLY like the existing bar-count functions on real uniform-cadence
data. No existing call site (`journal_logic.py`, `setup_types.py`,
`state.py`) is touched — the old functions keep running unchanged, the
new ones exist alongside them, untouched by anything currently live.
Stage 2 (proving the new functions are actually BETTER on genuinely
mixed-cadence data) and stage 3 (migrating call sites) are separate,
later, explicitly-gated work — not started.

**New functions**, all in `momentum_monitor/core/`:
- `indicators.ema_time_aware(values, timestamps, period,
  reference_interval_seconds=10.0)`
- `indicators.relative_volume_time_aware(bars, lookback_seconds=200.0)`
- `levels.evaluate_hold_time_aware(bars, level_price, direction="above",
  required_seconds=30.0, reference_interval_seconds=10.0)` →
  `HoldStateTimeAware` (parallel to `HoldState`, `elapsed_seconds` in
  place of `consecutive_bars`)
- `levels.swing_points_time_aware(bars, window_seconds, kind)` (time-aware
  analog of the private `_swing_points`, the primitive `detect_levels`
  and `confirmed_swing_lows` both build on)

**`ema_time_aware` — derivation, shown in full (not just the code).**
The bar-count EMA's recursion is `out[i] = k*v[i] + (1-k)*out[i-1]`,
`k = 2/(period+1)`. `(1-k)` is the fraction of the OLD value retained
after one bar-width step. Generalizing "one step" to an arbitrary real
elapsed time `dt`: if retention compounds continuously at the same rate,
the fraction retained after `dt` seconds is `(1-k)**(dt /
reference_interval_seconds)` (`reference_interval_seconds` being the bar
width the original `k` was calibrated against — 10.0, the live cadence).
The effective per-step weight on the new value is therefore

```
k_eff(dt) = 1 - (1 - k) ** (dt / reference_interval_seconds)
out[i] = k_eff(dt) * v[i] + (1 - k_eff(dt)) * out[i-1]
```

At `dt == reference_interval_seconds` (every live bar under uniform
cadence): `k_eff = 1 - (1-k)**1 = 1 - (1-k) = k` — the ORIGINAL formula,
exactly, by direct substitution. By induction (the base case `out[0]`
seeds identically in both functions, and the inductive step above shows
each subsequent step's formula is identical when `dt == reference_interval
_seconds`), the two sequences are equal at every index, not just in
aggregate.

One real subtlety this exposed: floating-point `**` does not always
round-trip losslessly at exponent `1.0`. E.g. `period=5`: `k = 2/6 =
0.3333333333333333`, but `1 - (1 - k) ** 1.0` evaluates to
`0.33333333333333326` — off in the last bit. Left as-is, this would make
`ema_time_aware` merely APPROXIMATELY equal to `ema` on uniform data,
not exactly equal as required. The implementation special-cases `dt ==
reference_interval_seconds` to use `k` directly, skipping `**`
entirely — this is what makes the equivalence test's `==` (not
`pytest.approx`) pass bit-for-bit, and is also just the correct
optimization for the overwhelmingly common case (every live bar).

**`evaluate_hold_time_aware` — the bar-start-vs-bar-end boundary, made
explicit.** `required_bars=3` at the live 10s cadence is
`required_seconds=30.0` — three FULL bar-widths of confirmed time, not
merely the 20-second gap between the first and third bar's own start
timestamps. Concretely: `elapsed_seconds = (current_bar_ts -
streak_start_ts) + reference_interval_seconds` — the current bar's own
assumed width is added on top of the gap since the streak began, because
confirmation is evaluated as of the END of the current bar's interval,
not its start. Proof of exact equivalence on uniform cadence: for the
k-th consecutive on-side bar in a streak (1-indexed) under uniform 10s
spacing, `current_bar_ts - streak_start_ts = (k-1)*10`, so
`elapsed_seconds = (k-1)*10 + 10 = k*10`. The bar-count version confirms
when `k >= 3`; the time-aware version confirms when `k*10 >= 30`, i.e.
`k >= 3` — the identical threshold, by direct algebra, not merely
observed to match in practice.

**`relative_volume_time_aware` and `swing_points_time_aware` — explicit
bar-count-to-seconds conversions**, both at the live 10s cadence:
- `relative_volume`'s `lookback=20` bars → `lookback_seconds=200.0`
  (20 × 10s). Window selection: every prior bar with `b["ts"] -
  lookback_seconds <= w["ts"] < b["ts"]`, which for uniform 10s bars
  (`ts = j*10`) reduces to `j in [i-20, i-1]` — the identical 20 bars
  `bars[i-lookback:i]` selects. "Not enough history" is judged the same
  way: `b["ts"] - bars[0]["ts"] < lookback_seconds` reduces to `i < 20`
  on uniform data, matching the bar-count version's `i < lookback` check
  exactly.
- `detect_levels`'/`confirmed_swing_lows`'s `swing_window=3` bars (each
  side) → `window_seconds=30.0` (3 × 10s, each side). A candidate's
  bracket is every bar with `abs(b["ts"] - cand["ts"]) <= window_seconds`,
  which for uniform 10s bars reduces to `j in [i-3, i+3]` — the identical
  7-bar segment `bars[i-window:i+window+1]` selects. The zero-volume
  forward-fill exclusion (section 3) is preserved unchanged in
  `swing_points_time_aware` — that rule is about real vs. synthetic
  bars, orthogonal to bar-count vs. real-time windowing.

**Live evidence** (real, already-captured bars — not a hand-built
fixture — pulled from `schwab-connector`'s actual `data/bars/AEMD.jsonl`
store, tonight's regular-hours session, 2026-09-18 09:30:00–15:59:50
America/New_York, 2,340 bars, gap-set `{10}` confirmed strictly uniform):

- `ema_time_aware` vs `ema`, periods 9/12/26 (macd's fast/slow legs and
  its own signal period): `new == old` (Python list equality, exact)
  over all 2,340 points, for all three periods. Spot-checked values
  (period=9): i=0 both `6.36`; i=100 both `6.259372499466311`; i=1000
  both `6.072960438384809`; i=2339 (last bar, 15:59:50) both
  `6.41329308830684`.
- `relative_volume_time_aware` vs `relative_volume`, `lookback=20` ↔
  `lookback_seconds=200.0`: `new == old` over all 2,340 points. i=19
  (last warm-up bar) both `1.0`; i=20 (first real comparison, volume
  jumped to 3,586 against a quiet opening average) both `4.834187...`;
  i=1000 both `1.500586...`.
- `swing_points_time_aware` vs `_swing_points`, `window=3` ↔
  `window_seconds=30.0`, kind="low": identical index lists, 210 swing
  lows found by both, in the same order (first five: indices 4, 12, 18,
  20, 24 — e.g. index 4 is the 09:30:40 bar, low=6.00).
- `evaluate_hold_time_aware` vs `evaluate_hold`, `required_bars=3` ↔
  `required_seconds=30.0`, level_price=6.20 (the session's median
  close): three real bar sequences walked step by step —
  1. 09:30:40–09:31:40 (7 bars, all closes below level): both stay at
     `consecutive_bars=0` / `elapsed_seconds=0`, `confirmed=False`
     throughout — trivial agreement, included for contrast.
  2. 09:33:40–09:34:30: bar1 close=6.2199 (on side) → OLD
     `consecutive_bars=1`, NEW `elapsed_seconds=10`; bar2 close=6.2150
     (on side) → OLD `consecutive_bars=2`, NEW `elapsed_seconds=20`;
     bar3 close=6.1810 (off side) → both reset, neither ever confirmed.
     Both count this as one failed attempt.
  3. 09:35:00–09:35:50, the confirming case: bar1 close=6.3199 → OLD
     `consecutive_bars=1` NEW `elapsed_seconds=10`, both
     `confirmed=False`; bar2 close=6.3086 → OLD `2` / NEW `20`, both
     still `False`; bar3 close=6.2650 → OLD `3` / NEW `30` — **both flip
     to `confirmed=True` on this exact same bar**; bar4–6 stay
     confirmed on both sides (`4`/`40`, `5`/`50`, `6`/`60`). This is the
     concrete proof the 30-second boundary derived above fires at
     precisely the same real bar as the bar-count version, not
     approximately the same one.

**Deliberate break-then-fix, both required equivalence tests**
(`test_ema_time_aware_exactly_equals_bar_count_ema_on_uniform_cadence`,
`test_evaluate_hold_time_aware_exactly_equals_bar_count_version_on_
uniform_cadence`), to prove the tests actually catch a real regression
rather than merely passing today:
- `ema_time_aware`: changed the uniform-cadence special case from
  `k_eff = k` to `k_eff = k * 1.01`. Result: both the hand-computed test
  and the equivalence test failed immediately (`15.05 == 15.0` and full
  divergence from index 1 onward across all three periods, e.g.
  period=3: `[5.0, 5.101..., ...] != [5.0, 5.1, ...]`). Reverted; full
  suite green again.
- `evaluate_hold_time_aware`: changed `elapsed_seconds = (b["ts"] -
  streak_start_ts) + reference_interval_seconds` to drop the `+
  reference_interval_seconds` term (reintroducing exactly the bar-start-
  vs-bar-end ambiguity this stage was built to resolve). Result: both
  the hand-computed test (`elapsed_seconds=20` instead of `30`,
  `confirmed` stuck `False`) and the equivalence test
  (`failed_attempts` mismatched: `1` vs. the bar-count version's `0`,
  because the streak that should confirm at bar 3 never did) failed
  immediately. Reverted; full suite green again.

**Status:** stage 1 complete — additive functions built, exact
equivalence proven both algebraically and against real uniform-cadence
data, tests proven to catch a real regression. `core/tests/test_core.py`:
27 tests passing (19 pre-existing plus 8 new for this stage);
`core` suite overall (including the untouched `test_setup_types.py`): 40
tests passing; full project suite: 276 tests passing, unchanged in every
other module, confirming zero impact on any existing call site. Stage 2
(mixed-cadence improvement) and stage 3 (call-site migration) are
separate future work, not started.

### 15. Time-aware core functions — phase 3.6, stage 2 (genuine improvement)

Stage 1 (section 14) proved `ema_time_aware`/`relative_volume_time_aware`/
`evaluate_hold_time_aware`/`swing_points_time_aware` are bit-exact with
the bar-count versions on real uniform-cadence data. Stage 2's job is
different and stricter: prove the time-aware functions are genuinely
BETTER on the actual case that motivated this whole effort — real
backfilled bars (coarser, irregular) followed by live 10s bars, with a
real gap inside the backfilled portion itself. Still purely additive:
no existing call site is touched. Stage 3 (migration) remains separate,
not started.

**The precise claim, proven algebraically first.** A bar's contribution
to `ema_time_aware` must scale PROPORTIONALLY to its real width — not
treated as equal to a 10s bar (naive equal-weighting), and not excluded
entirely (today's `live_cadence_tail` stopgap). Concretely: a single
60-second step landing on close `V`, from a prior ema value `P`, must be
mathematically IDENTICAL to six consecutive hypothetical 10-second steps
that each feed in `V`. Proof: expanding the standard recursion
`out_n = k*V + (1-k)*out_{n-1}` for `n` steps that all feed the same `V`
gives, by the geometric-series identity `k*sum_{j=0}^{n-1}(1-k)^j =
1-(1-k)^n`:

```
out_n = V*(1 - (1-k)**n) + (1-k)**n * P
```

For n=6: `out_6 = V*(1-(1-k)**6) + (1-k)**6 * P`. `ema_time_aware`'s
single 60s step computes `k_eff(60) = 1-(1-k)**(60/10) = 1-(1-k)**6`, so
`out_A = k_eff(60)*V + (1-k_eff(60))*P = V*(1-(1-k)**6) + (1-k)**6*P` —
the IDENTICAL expression, term for term. `test_ema_time_aware_single_
60s_step_equals_six_10s_steps_to_same_value` confirms this bit-for-bit
(`out_a[1] == out_b[6]`, not merely `pytest.approx`), using real-shaped
numbers (period=9, P/V taken from the actual AIFF ema level and close
used in the live evidence below).

**Two real necessary fixes this stage found** (both are the actual
subject of this stage's break-then-fix tests below — this is not a
cosmetic addition, stage 1's versions of these two functions did not yet
satisfy the proportional-weighting claim on mixed cadence):

1. `relative_volume_time_aware` (stage 1) selected its window by real
   time but still averaged RAW per-bar volume within it — correct only
   because stage 1's tests were all uniform-cadence, where dividing both
   sides of the ratio by the same constant duration cancels out. Once
   widths actually vary, raw-volume averaging is wrong: a 60s bar
   naturally carries ~6x a 10s bar's volume at the SAME underlying rate,
   so treating them as equal-weight readings misreads a normal rate as
   unusually high or low depending on how many wide bars happen to be in
   the window. Fixed by comparing volume RATES (volume/duration) — both
   the window's aggregate rate (`sum(volume)/sum(duration)`) and the
   current bar's own rate — whenever the window's bars don't all share
   `reference_interval_seconds` width; the uniform-width case is kept as
   its own bit-exact raw-volume path (not merely relying on the rate
   formula's algebraic cancellation to hold under floating point),
   preserving every stage 1 equivalence test unchanged.
2. `evaluate_hold_time_aware` (stage 1) added a FIXED
   `reference_interval_seconds` for every bar's own width, correct only
   because every stage 1 test used uniform 10s bars where the assumption
   and reality happened to coincide. Fixed to use each bar's ACTUAL
   width — `bars[i+1]["ts"] - bars[i]["ts"]` when a next bar exists,
   falling back to `reference_interval_seconds` only for the newest bar
   in the list (no next bar exists yet to measure from). This still
   reduces to stage 1's exact formula on uniform data (the next bar is
   always exactly `reference_interval_seconds` away), so every stage 1
   test is unaffected.

**Real mixed-cadence data**, pulled from `schwab-connector`'s actual
`data/bars/AIFF.jsonl` store — AIFF's real first watched session,
2026-09-17 07:50:00–14:40:10 (236 bars: 228 backfilled 60s-cadence bars,
then a genuine transition to 8 live 10s bars), containing BOTH required
irregularities in one real symbol's real day:

**1. Backfill-to-live transition** (real boundary: bar 227, 14:38:00,
close 1.3051 → bar 228, 14:39:00, close 1.3091 → bar 229, 14:39:10,
close 1.32 — `live_cadence_tail`'s actual boundary, confirmed by running
the real production function, lands at bar 228, since that bar's gap to
*the next* bar is only 10s even though bar 228 is itself a 60s-wide
backfilled bar):

   - `ema` (period=9): OLD (cold-started on the 8-bar live tail alone)
     `1.309100` at bar 228, `1.311280` at bar 229, `1.307024` at bar
     230, `1.299559` at bar 231, `1.293787` at bar 232. NEW (full
     236-bar backfill+live history) `1.304932`, `1.307945`, `1.304356`,
     `1.297425`, `1.292080` at the same five bars — a real, persistent
     divergence, not a rounding artifact: OLD restarts from nothing the
     instant streaming begins (its first value is always exactly that
     bar's own close, by construction), discarding the entire day's
     real decayed trend; NEW carries it forward correctly.
   - `relative_volume` (lookback=20/200s): OLD returns exactly `1.0` for
     all 8 live bars — "not enough history," even though real volume
     history plainly exists, it's just on the wrong side of
     `live_cadence_tail`'s cutoff. NEW returns `0.025235`, `0.140512`,
     `0.061211`, `0.074476`, `0.096600` at the same five bars —
     correctly reflecting that these bars are quiet relative to the
     REAL recent volume surge in the backfilled data just before the
     transition (bar 227's 60s bar alone carried 260,425 shares). OLD's
     flat `1.0` here isn't merely less informative, it's actively
     misleading — it reads as "perfectly average volume" when the real
     comparison says these bars are unusually quiet.
   - `evaluate_hold` (level_price=1.30, direction="above"): OLD, seeing
     only the 8-bar live tail, never confirms (`consecutive_bars=0`,
     `confirmed=False`, `failed_attempts=2` — the tail's own brief
     pokes above 1.30 each break before reaching 3 bars). NEW, walking
     the full real history, confirms exactly at bar 228 (14:39:00) —
     traced by re-running with progressively more of the real day
     included until `confirmed` first flips true, landing precisely on
     that bar, not an unrelated earlier point in the day. This is the
     real, structural case the whole effort exists for: bars 227 and
     228 are two consecutive 60s bars both closing above 1.30 — a
     genuine 60+ real seconds held above the level — which
     `live_cadence_tail` mostly excludes and which bar-count logic,
     even for the one backfilled bar it does let through, can't
     recognize as anything more than "1 bar, need 2 more."

**2. A real internal backfill gap** (08:23:00, close 0.9445 →
08:29:00, close 0.927 — a genuine 360-second/6-minute gap, Schwab
skipping 5 zero-volume minutes, confirmed present in the real stored
data, not manufactured): `ema_time_aware` (period=9) retains
`(1-k)**36 = 3.245×10⁻⁴` of the pre-gap ema value across this single
step — compare a naive equal-weighting treatment (crediting the gap as
one ordinary step) which would retain `(1-k)**1 = 0.8`, roughly **2,465×
too much** memory of stale pre-gap data. The computation itself runs
cleanly on the irregular spacing (`0.927005` immediately after the
gap, no exception, no special-casing needed) — real proof the gap is
treated as genuinely elapsed time, not silently mishandled.

**Deliberate break-then-fix, both stage-2-specific tests**
(`test_relative_volume_time_aware_uses_rate_not_raw_volume_across_mixed_
widths`, `test_evaluate_hold_time_aware_uses_actual_bar_width_not_fixed_
reference_interval`), same standard as stage 1:
- `relative_volume_time_aware`: reverted the rate-based branch back to
  always averaging raw per-bar volume (stage 1's version). Result:
  the mixed-width test failed immediately with the exact predicted
  wrong value (`0.6874999999999999` instead of `1.0` — a normal-rate
  bar misread as 31% below average purely because of how many wide
  bars happened to be in the window). Reverted; full suite green again.
- `evaluate_hold_time_aware`: reverted the actual-next-bar-width
  calculation back to stage 1's fixed `reference_interval_seconds`.
  Result: the bar-width test failed immediately (`confirmed=True`
  instead of the correct `False` — over-confirming after only 23
  real seconds because the fixed assumption over-counted an
  interior bar's real 2-second width as a full 10 seconds). Reverted;
  full suite green again.

**Explicit scope note:** `swing_points_time_aware` (the fourth stage-1
function) is NOT addressed by this stage — the "prove genuine
improvement" instruction for this stage named only
`ema_time_aware`/`relative_volume_time_aware`/`evaluate_hold_time_aware`
explicitly; `detect_levels`' mixed-cadence swing-window behavior remains
unproven on irregular data and is deferred, not silently assumed fine.

**Status:** stage 2 complete. `core/tests/test_core.py`: 31 tests
passing (27 from stage 1 plus 4 new for this stage); `core` suite
overall: 44 tests passing; full project suite: 276 tests passing,
unchanged in every other module. Stage 3 (migrating call sites) remains
separate, later, explicitly-gated work — not started.

### 16. Time-aware core functions — phase 3.6, stage 2 completion (swing_points_time_aware)

Stage 2 (section 15) explicitly left `swing_points_time_aware` out of
scope. This closes that gap, same standard: real mixed-cadence data
(AIFF's actual first watched session, reused from section 15 — same
236-bar backfill+live capture, same real 360s internal gap, same real
backfill-to-live transition), no existing call site touched.

**A real bug found, not merely a design tradeoff.** `window_seconds=30`
(calibrated to live 10s cadence: 3 bars × 10s) is NARROWER than real
backfilled bar spacing (60s). Stage 1's implementation built each
candidate's bracket as `[b for b in bars if abs(b.ts - cand.ts) <=
window_seconds]` — on 60s-cadence data this degenerates to a ONE-element
list (just the candidate itself, since its nearest real neighbors are
60s away, outside a 30s window), and a single-element list trivially
"wins" as both its own max AND its own min. Run against the real AIFF
day, this flagged 228 of 236 bars as low-swing-point candidates and 228
of 236 as high-swing-point candidates — nearly every bar, meaningless
noise, versus the bar-count version's 47 and 39. Fixed by requiring a
candidate's bracket to contain a REAL bar strictly before AND strictly
after it within `window_seconds` — calendar room alone (the existing
"far enough from the ends of the whole list" check) was never sufficient
on its own, a genuine local bracket must exist. This doesn't touch any
stage 1 uniform-cadence test (on uniform 10s data with a 30s window,
every eligible candidate always has 3 real neighbors on each side, so
the new check is always trivially satisfied there — reconfirmed, all
stage 1 tests still pass bit-for-bit unchanged).

**Real result, post-fix, on the same real AIFF day:** `swing_points_
time_aware` finds exactly 1 low (index 231) and 1 high (index 229) —
both inside the 8-bar live tail, none in the 228-bar backfilled portion.
The bar-count version's 47/39 backfilled-region "finds" were never
backed by a genuine 30-real-second bracket; they were backed by however
many *bars* window=3 happened to reach, regardless of how much real time
that spanned — which leads to the two required checks below.

**1. Across the real internal gap** (08:23:00 → 08:29:00, the same 360s
gap from section 15): the bar-count version's window=3 segment real span
for candidates near the gap balloons far past its nominal ~360s (3 bars
× 60s × 2 sides) intent — measured directly on the real data: index 24
spans 720 real seconds, index 26 spans 900s, index 29/30 span 1,320s
(22 real minutes) for a parameter nominally meaning "3 bars each side."
The gap is silently absorbed into the window's real meaning without any
signal that it happened. `swing_points_time_aware`, by construction,
never has this problem — its bracket is always exactly `window_seconds`
of real time, gap or no gap; post-fix, it correctly finds nothing
confirmable at 30-second resolution across a stretch this sparse,
because there genuinely isn't 30 real seconds of bracketing data there.

**2. At the real backfill-to-live transition** (14:35:00–14:39:40, same
transition as section 15): the bar-count version's window=3 real span,
measured candidate by candidate straddling the boundary, shrinks
smoothly and asymmetrically as the candidate approaches and crosses it
— 360s (idx 224, pure backfill) → 310s (226) → 260s (227) → 210s (228)
→ 160s (229) → 110s (230) → 60s (231, pure live) — a 6× difference in
what "window=3" actually means in real time, purely a function of
proximity to the transition, never signaled anywhere in the output.
`swing_points_time_aware` has no equivalent asymmetry — its bracket
width is a fixed real-time quantity by definition, never a function of
which cadence regime a candidate happens to sit near.

**Zero-volume exclusion, reconfirmed on real data.** AIFF's real
backfilled portion contains ZERO zero-volume bars — Schwab's minute
candles omit quiet minutes entirely (creating gaps, not zero-volume
bars; the internal gap above IS this behavior), so the zero-volume
forward-fill scenario only actually occurs in live-cadence data (real
finding, not assumed: backfill and live-forward-fill are two genuinely
different mechanisms for handling a quiet period). Reconfirmed instead
against AEMD's real live-cadence session (section 14's data): 1,601 real
zero-volume bars are present; none appear among `swing_points_time_
aware`'s low or high results, confirmed by direct set intersection
against the real output, not assumed carried-over from stage 1.

**Deliberate break-then-fix**
(`test_swing_points_time_aware_requires_a_real_bracket_not_just_
calendar_room`): reverted the `has_before`/`has_after` check to
unconditional `True`. Result: the test failed immediately, reproducing
the exact original bug (`[1, 2, 3, 4, 5, 6, ...]` instead of `[]` on
60s-cadence data with a 30s window). Reverted; full suite green again,
`test_swing_points_time_aware_still_finds_real_swing_points_when_
window_actually_brackets` confirms the fix doesn't make the function
vacuously empty in general (widen the window to genuinely bracket the
same 60s-cadence data and the real V-shaped low is still found).

**Honest, load-bearing conclusion for stage 3 planning.** This is not
"no material difference" (a real bug was found and fixed) but the
larger design finding is more nuanced than section 15's other three
functions: `swing_points_time_aware` is now MORE correct than the
bar-count version (it never confirms a swing point without genuine
real-time bracketing evidence), but at `window_seconds=30` — the
value that exactly matches live cadence — it finds essentially nothing
useful on 60s-cadence backfilled data, where the bar-count version
happens to find plenty (just not reliably, per the asymmetries above).
`detect_levels` currently runs its bar-count window across the FULL
backfilled+live series deliberately (state.py: "detect_levels...
deliberately keep seeing the full backfilled+live series"). A
mechanical swap to `swing_points_time_aware` at `window_seconds=30`
would NOT be a drop-in improvement for that call site — it would need a
materially larger or cadence-adaptive window to find anything at all in
backfilled data. This is exactly the kind of finding stage 3 (migration)
needs before it starts, and is the reason stage 3 stays separately
gated rather than assumed.

**Status:** stage 2 now fully complete (all four stage-1 functions
covered). `core/tests/test_core.py`: 33 tests passing (31 from before
this stage plus 2 new); `core` suite overall: 46 tests passing; full
project suite: 276 tests passing, unchanged elsewhere. Stage 3 remains
separate, later, explicitly-gated work — not started.

### 17. Cadence-adaptive window for level detection — phase 3.6 completion

Section 16 fixed a real bug (degenerate single-bar brackets) but left an
explicit design gap: a single fixed `window_seconds=30`, calibrated to
live 10s cadence, is narrower than real 60s backfilled bar spacing, so
the fixed-window version — correctly, but uselessly — excludes the
entire backfilled portion of a day from ever confirming a swing point.
This section originally shipped a first fix for that gap; a follow-up
investigation (recorded here in full, not just the corrected result —
same "document the incident" discipline as sections 11/13) found that
first fix's own two identified edge cases were structural, not rare, and
replaced it with a version proven to close both. No existing call site
touched throughout.

**The first design, and why it wasn't good enough.** `window_seconds`
was replaced with `multiple` (default `3.0`) times a candidate's own
real observed width, via `_bar_duration` (the same helper
`relative_volume_time_aware`'s rate fix uses). This closed the original
bug and passed both the uniform-cadence equivalence proof (AEMD, 210/210
lows and 192/192 highs identical to the bar-count version) and a genuine
mixed-cadence improvement proof (AIFF, 44 real lows and 39 real highs
newly confirmed inside the 60s-cadence backfilled portion, versus 0 for
the fixed-window version). Two edge cases were found and checked against
ONE real instance each: a bar at a cadence speed-up (60s predecessor
gap, 10s successor gap) could become unbracketable since its own width
was scaled by the narrower successor gap; and a bar in a doubly-sparse
stretch could have its own inflated width bridge back across a
neighboring real gap. Both instances happened not to change an actual
swing-point verdict, and were reported as "narrow, non-outcome-changing"
exceptions.

**That was insufficient evidence, per this project's own standing
lesson** (the compounding-equity bug earlier this session: a benign
instance never proves a mechanism sound in general). A full structural
scan of the ENTIRE real captured history for all four mixed-cadence
symbols (AIFF, AEMD, DAIC, DTSS — ~51,000 bars, not just the one AIFF
day already sampled) found both edge cases were common, not rare:

- **54 real bars** where a narrow forward gap masked a genuinely wider
  real backward neighbor (the cadence-speed-up pattern) — not a one-off.
- **63 of 100** real large-gap (>90s) bars actually bridged back across
  their own gap — a MAJORITY of the time a real gap occurred, not an
  isolated fluke.

**Alternatives tested against the same real data, not reasoned about in
the abstract.** `min(backward_gap, forward_gap)` as the width: **zero
change** to either count (54/63-of-100, identical) — forward was already
the smaller value in essentially every real occurrence, so `min`
collapses to the existing (flawed) behavior. `max(backward_gap,
forward_gap)`, and an asymmetric design sizing each side directly off
its own single neighboring gap: both fully closed the narrow-window
blind spot (0 remaining) but made gap-bridging strictly WORSE — 100 of
100 real large gaps bridged, up from 63. This is a real, confirmed
tension: any design deriving ONE scalar per side from a SINGLE adjacent
gap cannot solve both problems at once, because that one gap value has
to serve two conflicting purposes — "how sparse is it here" (should
widen the window) and "is this specific neighbor even reachable" (should
narrow it) — and a real discontinuity is, numerically, indistinguishable
from genuinely coarse cadence using only that one number.

**The design that actually closes both, verified against the same real
data.** `swing_points_time_aware`'s bracket is now built by a real
TWO-DIRECTIONAL WALK (`_walk_real_neighbors`): each side walks outward
hop by hop, accumulating real elapsed time using each traversed pair's
own actual gap. A single hop larger than `max_hop_seconds` (default
`90.0` — chosen because it sits cleanly between real backfill's 60s
baseline cadence and the smallest real "skipped-minute" gap observed in
this data, 120s) is a hard stop: never crossed, never counted, rather
than treated as "far but still valid, scaled generously." The
accumulation target on a side is `multiple` times that side's own FIRST
(immediately adjacent) hop — so it still scales to whatever cadence
genuinely exists right next to the candidate — but `max_hop_seconds`
caps that first hop too, so a candidate sitting immediately next to a
real gap can never use the gap itself to inflate its own target. This no
longer uses `_bar_duration` at all (a genuinely different mechanism per
the explicit suggestion to evaluate a two-directional walk, not a
variant of the single-scalar design the "don't add a second width
primitive" instruction was originally about).

Re-scanned against the SAME full real history: **0 remaining narrow-
window blind spots, 0 of 100 remaining gap-bridges.** Both of the
originally-identified real instances, re-checked directly against the
final implementation: index 228 (the real transition bar) now correctly
finds its real 60s-cadence predecessor region (non-empty walk, 3 bars
collected); index 28 (the real gap-adjacent bar) now correctly finds
nothing on its "before" side (its 360s first hop exceeds
`max_hop_seconds`, never crossed).

**Re-ran both required proofs to confirm neither regressed.**
Equivalence (AEMD, real uniform data): 210/210 lows and 192/192 highs,
still identical — the walk's target on each side (3.0 × the adjacent 10s
hop = 30.0, reached via exactly 3 real 10s hops) reduces to the exact
same bracket as `bars[i-3:i+4]`. Improvement (AIFF, real mixed-cadence
day): 26 lows and 27 highs total, 25 and 26 of those genuinely inside
the backfilled portion — still a real, substantial improvement over the
original fixed-window design's 0/0, though a real, honestly-reported
TRADEOFF against the superseded design's 44/39: this version is more
conservative in ordinary sparse (100–180s) stretches too, because it
requires independent real accumulation on each side rather than one
generous symmetric radius. That higher 44/39 figure was itself partly a
product of the very gap-bridging this version closes, so it was not a
trustworthy number to begin with.

**Deliberate break-then-fix**, both new tests
(`test_swing_points_time_aware_still_excludes_a_real_internal_gap`,
`test_swing_points_time_aware_requires_a_real_bracket_not_just_calendar_
room`): removed the `max_hop_seconds` cap from both the first-hop check
and the per-hop loop check in `_walk_real_neighbors`. Result: the
gap-exclusion test failed immediately (the walk crossed the real 360s
gap, returning 4 bars instead of `[]`), and the narrow-max_hop test also
failed (a spurious swing point reappeared). Reverted both; full suite
green again, no leftover markers.

**A third correction, found during phase 3.6 stage 3's migration
downstream regression (specs.md section 18), superseding the 25/26
figure above.** Migrating `detect_levels`/`confirmed_swing_lows` to this
function and running `setup_types.py`'s full existing test suite
surfaced a real bug this section's own tests never exercised:
`_walk_real_neighbors` returned whatever PARTIAL collection it had
gathered when it ran out of real bars (near either end of `bars`) or hit
an uncrossable hop BEFORE reaching its own `target` — accepting an
incomplete bracket as if it were sufficient. `setup_types.py`'s own
`test_micro_breakout_finds_a_level_the_main_window_misses` (a 5-bar
fixture built specifically so the main `swing_window=3` window has too
few bars to ever confirm anything) failed: the candidate was wrongly
confirmed using a partial 2-bar collection on each side, instead of
correctly finding nothing. Fixed: `_walk_real_neighbors` now returns
`[]` whenever the walk ends without reaching `target`, whether from
running out of bars or hitting a real gap partway through — matching
the bar-count design's actual intent (a genuinely adequate bracket on
both sides, not merely "some"). Proven directly in `core/tests/
test_core.py` (`test_swing_points_time_aware_requires_reaching_the_full_
target_not_a_partial_walk`) and via break-then-fix (reverting to
`return collected` mid-loop reproduced both the new core test's failure
and the original `setup_types.py` failure that caught this; reverted,
green again). Re-verified this doesn't regress anything already proven:
AEMD uniform equivalence still 210/210 and 192/192; the Finding-1/
Finding-2 full-history structural scan still 0 and 0/100. The AIFF real
mixed-cadence improvement number drops further, honestly reported: 11
lows and 12 highs total (10 and 12 genuinely inside the backfilled
portion), down from 25/26 — because several of those 25/26 were
themselves confirmed using an incomplete bracket the fix above no longer
accepts. Still a real, if now smaller, improvement over the original
fixed-window design's 0/0, and — more importantly — now the FIRST
version of this function whose confirmations are backed by a genuinely
complete real-time bracket in every case, not merely "no known gap
crossed."

**Status:** phase 3.6's design/proof work is now complete for all four
functions, with the swing-point windowing specifically re-verified
against a FULL real-history structural scan (not a single sampled
instance) THREE times over — the original fixed-window bug, the
gap/transition structural scan, and this partial-walk correction found
by downstream migration testing. `core/tests/test_core.py`: 37 tests
passing; `core` suite overall: 50 tests passing; full project suite: 276
tests passing, unchanged elsewhere. Stage 3 (migrating call sites,
including retiring `live_cadence_tail`) remains separate, later,
explicitly-gated work — not started as of this section; see section 18
for stage 3 part 1 (`detect_levels`/`confirmed_swing_lows`).

### 18. Phase 3.6 stage 3, part 1 — migrate detect_levels/confirmed_swing_lows

The first real PRODUCTION change in phase 3.6 — everything in sections
14-17 was additive/comparison only. Scoped narrowly, same discipline as
every prior stage: `detect_levels` and `confirmed_swing_lows` only, not
`ema`/`relative_volume`/`evaluate_hold` (those three currently share
`live_cadence_tail` as one mechanism — see section 3 — and retiring it
only makes sense once all three no longer need it; that's a separate,
later prompt). `detect_levels` and `confirmed_swing_lows` were the
natural first migration because they already see the FULL
backfilled+live series today (`state.py`: "detect_levels... deliberately
keep seeing the full backfilled+live series") — no `live_cadence_tail`
filtering to untangle, purely a window-logic swap.

**The change.** Both functions' internal `_swing_points(bars, window,
kind)` call is replaced with `swing_points_time_aware(bars, kind=kind,
multiple=float(window))` (sections 15-17's cadence-adaptive, gap-safe
two-directional walk). Public signatures are UNCHANGED —
`detect_levels(bars, swing_window=3, ...)` and `confirmed_swing_lows(
bars, window=3)` still take a plain int, now interpreted as `multiple`
rather than a bar count; every existing caller (`setup_types.py`'s
`_breakout_candidate` for both `resistance_breakout` (swing_window=3)
and `micro_breakout` (`MICRO_SWING_WINDOW=1`), `monitor-app/app.py`'s
swing-low-anchored stop, `state.py`'s plain `detect_levels(bars)`) needed
zero changes.

**A third real bug in `swing_points_time_aware` itself, found by this
migration's own downstream regression** (not by anything new written for
this stage) — documented in full in section 17, summarized here since it
directly affects this migration's real numbers: `_walk_real_neighbors`
was accepting a PARTIAL walk (ran out of bars, or hit a gap, before
reaching its own target) as if it were a sufficient bracket.
`setup_types.py`'s own pre-existing `test_micro_breakout_finds_a_level_
the_main_window_misses` — built specifically so a 5-bar fixture gives
the main `swing_window=3` window too few real bars to ever confirm
anything — failed the instant the migration landed, catching this
directly. Fixed (section 17 has the full fix and re-verification); this
migration's own before/after numbers below are against the corrected
version.

**Real before/after, on the same real AIFF mixed-cadence day used
throughout sections 14-17** (236 bars: 228 backfilled 60s-cadence, then
8 live 10s bars):

- `detect_levels`: OLD (bar-count) finds 24 levels total — top 5 by
  strength: support 1.0401 (13 touches), support 1.0205 (11), resistance
  1.0300 (9), support 1.1175 (8), support 0.9259 (6). NEW (migrated)
  finds 15 levels total — top 5: support 1.1192 (6 touches), resistance
  1.1000 (3), resistance 1.1500 (2), resistance 1.1999 (1), support
  1.1023 (1). The drop (24→15) is the DIRECT, expected consequence of
  sections 16/17's fixes actually taking effect here: several of OLD's
  higher-touch-count levels (13, 11, 9 touches) were themselves built
  from swing points confirmed via degenerate single-bar brackets and
  gap-bridging bugs already proven wrong on this exact data — NEW's
  lower, "less impressive-looking" numbers are the more trustworthy
  ones, not a regression.
- `confirmed_swing_lows`: OLD finds 47 confirmed lows, NEW finds 11.
  First few OLD entries (08:03, 08:11, 08:12, 08:16, 08:17, all clustered
  around a stale 0.926 price) are exactly the kind of gap/degenerate-
  bracket artifacts sections 16/17 diagnosed; NEW's first few entries
  (08:03, then 09:35, 13:43, 13:48, 13:49) are more sparsely and, per
  the underlying data, more genuinely spaced.

**Regression check: purely uniform-cadence data must be unaffected**,
re-confirmed directly (not assumed from sections 15-17 still holding) —
AEMD's real 2026-09-18 regular session (2,340 bars, strictly 10s
cadence): `detect_levels` — 21 levels, byte-for-byte identical
(kind/price/touch_count) between OLD and NEW. `confirmed_swing_lows` —
210 lows, identical entry-for-entry.

**Full downstream regression**, both suites that directly consume these
two functions: `core/tests/test_setup_types.py` (all four setup types
depend on `detect_levels`' output) and `monitor-app/tests/
test_journal_logic.py` + `test_journal_wiring.py` (the swing-low-anchored
stop depends on `confirmed_swing_lows`) — 135 tests passing together
(98 baseline + the new section-17 core test + the fix already covered
above), zero silently-adjusted tests: the one downstream test that did
fail (`test_micro_breakout_finds_a_level_the_main_window_misses`)
surfaced a real bug in the migrated function itself, which was fixed at
the root (section 17), not worked around by changing the test's
expectation. Full project suite: 276 tests passing, unchanged elsewhere.

**Production deployment: held, not performed.** `monitor-app/data/
journal.db` has one open position at the time of this migration (AEMD,
id 52, entered 2026-09-18, no `exit_ts`) — restarting `monitor-app`
would drop its in-memory per-symbol state for a live open trade, the
same risk this project has avoided all session. Per that standing
practice, the actual deploy is held; verification instead ran as an
ISOLATED check against the real, currently-running, already-live
`schwab-connector` (no production container touched, no restart, no
write): pulled DAIC's real live bar history directly from the running
`schwab-connector`'s own `/bars/DAIC` endpoint (13,540 real bars, freshest
one 27 seconds old at fetch time — genuinely live, not a snapshot),
and ran both OLD and NEW `detect_levels`/`confirmed_swing_lows` against
it locally. Result: the top 5 levels by strength are IDENTICAL between
OLD and NEW (DAIC's captured history is almost entirely live-cadence,
so little room for the migration to matter at the top) — support 4.7395
(96 touches), resistance 5.3437 (80), support 3.6552 (80), resistance
4.7688 (72), resistance 3.6957 (63) — with small, expected differences
further down the list (96 vs 93 total levels, 1,159 vs 1,153 confirmed
swing lows). This confirms the migrated code runs cleanly against real,
currently-streaming production data and produces sensible output,
without the risk of restarting a process holding a live position.

**Rollback awareness, stated plainly whether or not it's needed.** This
is the first change in phase 3.6 touching live trading decisions. If
anything looks wrong once this DOES get deployed (a level that doesn't
make sense, a setup type behaving unexpectedly) the correct response is
reverting this commit immediately and re-diagnosing from a clean state —
not attempting a live fix under pressure. Nothing wrong was observed in
the evidence above, but the option was explicitly considered, and the
deploy itself is deliberately deferred until AEMD's open position
clears, specifically so that if a problem DOES show up after deploy, the
response isn't complicated by an already-in-flight live position riding
on the change.

**Status:** `core/tests/test_core.py`: 37 tests passing; `core` suite:
50 tests passing; `setup_types.py` + `journal_logic.py` + `journal_
wiring.py`: 135 tests passing; full project suite: 276 tests passing.
Deploy held pending AEMD's open position clearing. Stage 3 part 2
(migrating `ema`/`relative_volume`/`evaluate_hold` together and retiring
`live_cadence_tail`) remains separate, later, explicitly-gated work —
not started.

### 19. Phase 3.6 stage 3, part 2 — migrate ema/relative_volume/evaluate_hold, retire live_cadence_tail

The higher-stakes half of stage 3: unlike section 18's level-detection
migration, `evaluate_hold`/`relative_volume` gate every real entry
directly, and `ema` feeds MACD and display. Migrating them means the
backfilled portion of a session influences live trading decisions for
the first time ever, not just display numbers — treated throughout as a
real behavioral change to investigate, not a correctness refinement to
verify.

**The migration.** `ema`→`ema_time_aware`, `relative_volume`→
`relative_volume_time_aware`, `evaluate_hold`→`evaluate_hold_time_aware`
at every real call site (`monitor-app/state.py`'s `build_state`/
`_level_block`, `core/setup_types.py`'s four candidate builders), now
fed the FULL backfilled+live bar series directly — the
`live_cadence_tail`/`LIVE_BAR_MAX_GAP_SECONDS` split this was built to
replace is REMOVED entirely (`monitor-app/state.py`), not left unused
next to its replacement. `macd`, not itself named in this migration's
explicit scope but structurally required to retire `live_cadence_tail`
cleanly (it fed on the same live-only closes), got its own
`macd_time_aware` — composed directly from `ema_time_aware`, exactly as
stage 1 anticipated ("composable directly from ema_time_aware once
needed"). `setup_types.evaluate_setups` drops its `bars`/`live_bars`
split entirely (one `bars` parameter now), gains `watch_added_ts`
(below), and its hold-block fields rename `consecutive_bars`/
`required_bars` → `elapsed_seconds`/`required_seconds` (monitor-app's
two server-rendered and two client-side JS hold-detail tables updated to
match: "time above level: Xs / Ys").

**Required investigation: can backfill alone fire an entry? Yes —
confirmed real, then fixed.** With `evaluate_hold_time_aware` fed the
full session, a hold's `required_seconds` of confirmation can complete
ENTIRELY within backfilled (pre-watch) bars. Proven directly on real
AIFF data: a real resistance level (1.1000) first confirmed at 13:39:00
using only backfilled bars, and (confirmation being monotonic, "once
confirmed, stays confirmed") remained `confirmed=True` all the way
through 14:40:10 — over an hour later, well into live streaming.
`monitor-app/journal_logic.py`'s `_first_newly_confirmed` fires on any
False→True transition against `was_confirmed_types`, which starts EMPTY
for a freshly-added symbol's slot — so a symbol added at, say, 14:00
would show this level as a *fresh* transition on its very first poll and
could fire a real entry instantly, based on a pattern that finished
before the symbol was ever being watched live.

**The fix, an explicit design decision, not buried:** `evaluate_hold_
time_aware` gained `watch_added_ts` (default `None`, no behavior
change for any caller that doesn't pass it). Backfilled bars may still
legitimately CONTRIBUTE real elapsed time to a streak — that's the
whole point of this migration — but `confirmed` may only be *set* at a
bar whose own `ts >= watch_added_ts`: the confirming instant itself must
not be purely historical, even though the streak backing it may have
started before the watch. A genuinely continuous, still-ongoing hold
that started before watch and never reverses simply confirms at the
first post-watch bar instead of instantly (proven:
`test_evaluate_hold_time_aware_confirms_once_a_post_watch_bar_extends_a_
pre_watch_streak`) — not withheld forever, just not backdated to a
moment nobody was watching. `_SymbolSlot` gained `added_ts` (captured
from `Poller`'s own `now_fn` at `add_symbol` time), threaded through
both `build_state` call sites. Proven end to end, not just at the
`evaluate_hold_time_aware` level: `test_watch_added_ts_prevents_
confirming_purely_from_pre_watch_bars` (setup_types.py) and
`test_build_state_watch_added_ts_reaches_setups_and_level_blocks`
(state.py) both reproduce the real risk with `watch_added_ts=None`, then
confirm it's blocked with a real value.

**A second, DISTINCT real risk, found by mandatory downstream regression
— found, investigated, and explicitly left OPEN, not silently
patched.** `setup_types.py`'s full test suite passed, but `monitor-app`'s
real integration suite (`test_journal_wiring.py`) initially showed 20
failures. 19 were a test-harness artifact (fixed): `_client`'s default
`now_fn=time.time` combined with this file's small synthetic bar
timestamps (0, 10, 20…) meant `watch_added_ts` (real wall-clock) sat far
in the future of every fixture bar, blocking every confirmation in the
file — fixed by defaulting the test harness's `now_fn` to a fixed
`0.0` (AGENT_PROTOCOL.md: no wall-clock dependence in tests), matching
real production's actual invariant (bars and "now" are naturally close
together) that this file's small-ts convention doesn't reproduce on its
own.

The 20th failure was real: `test_two_symbols_journal_positions_are_
fully_independent` — AEHL's `round_number_reclaim` position correctly
stopped out on a sharp price drop, but a NEW position immediately
reopened in the SAME tick, entry_price 8.0, using a trigger (8.25)
freshly recomputed from the new, much lower price. `round_number_
reclaim`'s trigger is DYNAMIC (`nearest_round_number_above(current_
price)`, recomputed every call) — unlike `detect_levels`' resistance/
support levels, which are relatively stable real touched prices. Once
`evaluate_hold_time_aware` sees the FULL session, bars from HOURS
earlier (when price was much higher) can satisfy a freshly-lower
trigger — a real, structural consequence of `confirmed`'s monotonic
design (`evaluate_hold`'s own "once confirmed, a single close back
through doesn't retroactively un-confirm history"), which was only ever
safe because every caller previously fed it a short, `live_cadence_tail`-
scoped window. `journal_logic.py`'s own comment already anticipates a
stop-out immediately followed by a genuine fresh confirmation — this was
a STALE one, not a genuine one, and the bar-count design's OWN
protection against it ("a single sharp-drop bar can't produce 3
consecutive bars against a new, lower trigger") silently depended on
that same short-window assumption.

A first attempted fix (`confirmed` resets to `False` on any close back
through the level) was tried, verified to close this exact case, then
**reverted** — it broke the genuine, load-bearing "recently confirmed,
still actionable" property 20+ OTHER tests (and the real entry-firing
design) depend on: the live entry bar itself is often one tick back
through the level even while a real, current hold is very much still in
play, and resetting on any reversal made confirmation intolerably
fragile. The real distinguishing factor is TIME-BASED staleness (a
one-tick, seconds-old pullback vs. an hours-old, since-reversed regime),
not a simple has-it-ever-reversed check — fixed properly in section 20,
below, without touching `evaluate_hold_time_aware`'s state machine at
all.

**Real before/after, on the same real AIFF mixed-cadence day (236 bars)
used throughout sections 14-18.** OLD reconstructed via `live_cadence_
tail`'s old 8-bar live-only tail; NEW via `build_state` on the full
236-bar series:
- `ema9`: 1.2910 → 1.2902 (small, real). `ema20`: 1.2982 → 1.2874
  (larger — longer memory reaches further into backfill).
- `macd`: **-0.006499 → +0.00923** — a full SIGN FLIP (bearish to
  bullish reading), `signal`: -0.003729 → 0.015366, `histogram`:
  -0.002769 → -0.006136. A materially different technical reading, not
  a refinement.
- `relative_volume`: **1.0000 → 0.2516** — OLD's flat 1.0 was a trivial
  "not enough history" default (only 8 live bars, fewer than
  lookback=20); NEW's 0.2516 is a real, informative reading against the
  day's actual volume (correctly quiet relative to a real earlier
  surge).
- Support-level hold confirmation: **`confirmed=False` → `confirmed=
  True`** — OLD's 8-bar live tail could never reach 3 consecutive bars
  below the level; NEW correctly recognizes a real, substantial
  real-time hold using genuine backfilled context. This is the single
  most consequential real difference: an entry-gating boolean flipping
  from false to true on the exact same real data.

Uniform-cadence regression, re-confirmed directly at the `build_state`
level against REAL data (not assumed from earlier stages, not just
synthetic tests): AEMD's real 2026-09-18 regular session (2,340 bars,
strictly 10s cadence) — `ema9`, `ema20`, `macd`, and `relative_volume`
all identical between the OLD reconstruction and NEW `build_state`,
to the same rounding.

**Full downstream regression.** `core/tests/` (56 tests: `test_core.py`
+ `test_setup_types.py`) and `monitor-app/tests/` (272 tests, including
the fixed `now_fn` default and the one honestly-updated assertion above)
all green — 327 tests total for the two directly affected suites, full
project suite 434 tests (`core` 55, `schwab-connector` 107, `monitor-app`
272 — the `run_tests.sh` breakdown), zero silently-adjusted tests hiding
either real finding.

**Production deployment: held, not performed**, same standing practice
as section 18 — `journal.db` still has AEMD's open position (id 52) at
migration time. Verified instead as an isolated check against the real,
currently-live `schwab-connector`: pulled DAIC's real live bar history
directly (15,134 real bars, freshest one 26 seconds old at fetch time),
ran the migrated `build_state` against it locally. Currently pre-market
quiet (flat 3.61, zero volume) — `ema9`/`ema20`/`vwap` all correctly
flat at 3.61, `relative_volume=1.0` (genuinely flat, not a bug), and
real, sensible resistance/support levels and setup candidates with real
touch counts and hold states, using this session's actual real price
history. Ran cleanly, no errors, sensible output.

**Rollback awareness, stated plainly — matters more here than for
section 18** given this migration's larger behavioral surface (it now
touches live entry-gating directly, not just display). If anything
looks wrong once this deploys — a level that doesn't make sense, a
setup type behaving unexpectedly, an entry that doesn't hold up — the
correct response is reverting the relevant commit immediately and
re-diagnosing from a clean state, not attempting a live fix under
pressure. Nothing wrong was observed in the evidence above; the deploy
itself is deliberately deferred until AEMD's position clears.

**Status:** `core` suite 55 tests, `monitor-app` suite 272 tests, full
project suite 434 tests, all green. `live_cadence_tail` fully retired
(confirmed by direct grep — no remaining definition or call, only
historical prose references). Two real risks investigated: the one
explicitly asked about (backfill-only confirmation) is FIXED and proven;
a second, related one found by mandatory downstream regression (stale
same-session reconfirmation for dynamically-recomputed triggers) was
investigated, reasoned about, and is FIXED in section 20 (a follow-up
prompt, same day) — see there for the staleness gate, the freshness
window value and reasoning, and the real-data scope decision. Deploy
held pending AEMD's open position clearing.

### 20. Phase 3.6 follow-up — confirmation-freshness gate (closes section 19's stale-reconfirmation risk)

Section 19 found a real bug (a stop-out on a sharp price drop could be
immediately followed, in the same tick, by a spurious fresh entry using
bars from well before the drop) and reverted a first fix attempt
("reset `confirmed` on any reversal") because it broke the genuine,
load-bearing "recently confirmed, still actionable" property 20+
existing tests and the real entry-firing design depend on. This closes
that gap properly: a staleness gate at the point confirmation is
CONSUMED to decide on a new entry, not inside `evaluate_hold_time_
aware`'s own monotonic state, which is completely untouched.

**The fix.** `HoldStateTimeAware` gains `confirmed_at_ts: float | None`
— the timestamp of the most recent bar where `confirmed` was genuinely
REAFFIRMED (still on-side, still past `required_seconds`). It refreshes
on every such bar while a hold continues (staying current for as long
as the hold is real and ongoing) and freezes at the last such bar once
the streak breaks — exposing exactly how stale a persisted
`confirmed=True` actually is, without `evaluate_hold_time_aware` needing
any notion of "now" itself. Propagated through `setup_types.py`'s
`_hold_dict` and `state.py`'s `_level_block` into every setup/level
dict's `hold.confirmed_at_ts` field (alongside the existing `confirmed`,
unchanged).

The actual gate lives in `monitor-app/journal_logic.py`'s
`_first_newly_confirmed` — the exact point a setup's `confirmed=True` is
consumed to decide on a NEW entry. It now also requires
`now_ts - confirmed_at_ts <= confirmation_freshness_seconds`, where
`now_ts` is the caller's own "now": the latest bar in the CURRENT poll's
batch (`new_bars[-1]["ts"]`, already available in `advance_journal`,
requiring no wall-clock dependency and staying fully deterministic in
tests). `confirmed` itself, `was_confirmed_types` bookkeeping, and every
other consumer of the hold dict (display, `_level_block`) are completely
unaffected — this is purely an additional condition on whether a
candidate is ACTIONABLE for a brand-new entry.

**New live-tunable strategy_param: `CONFIRMATION_FRESHNESS_SECONDS`,
default 30.0.** Same seed-only / live-tunable-via-`POST /api/strategy_
params` treatment as every other threshold in this project (`journal_
store.py`'s `_PARAM_BOUNDS`: `(0.0, 3600.0)` — the upper bound is a
generous ceiling, same "wide but not unbounded" pattern as the other
params, not a real expected operating value). Reasoning for the
default: 30.0 matches `REQUIRED_HOLD_SECONDS` itself (`evaluate_hold_
time_aware`'s own `required_seconds` default) — a confirmation remains
actionable for as long as it took to establish it in the first place.
Checked against both real numbers this section needed to reconcile: a
one-tick pullback at live 10s cadence ages 10-20 seconds (comfortably
under 30 — stays actionable, preserving the property the reverted fix
broke); the real bug found in section 19 (round_number_reclaim, real
AIFF data, 07:54:00) aged 240 seconds, and the real `test_two_symbols_
journal_positions_are_fully_independent` scenario aged 40 seconds — both
comfortably OVER 30 (correctly blocked).

**Scope investigated with real data: does this apply to all four setup
types, or just round_number_reclaim?** Simulated polling through the
real AIFF session (step-by-step, tracking every False→True transition
and its age for all four types) rather than assuming either way. Real
findings: `round_number_reclaim` reproduces it (age 240s at 07:54:00,
and again 50s at 14:40:00); `micro_breakout` ALSO reproduces it
independently (ages 420s, 120s, 300s, 180s, and 50s across five separate
transitions that real day) — proving the mechanism is NOT unique to
round_number_reclaim's dynamically-recomputed trigger, contrary to
section 19's initial hypothesis: `micro_breakout`'s trigger comes from
`detect_levels` (a real structural level), and it exhibits the exact
same staleness pattern whenever the "nearest" qualifying level's
identity changes and `was_confirmed_types` "forgets" the type in
between. `resistance_breakout` and `vwap_reclaim` never confirmed often
enough on this particular real day to independently exhibit a
transition either way (0 confirmed occurrences for `vwap_reclaim`, 0 for
`resistance_breakout` across the whole day) — genuinely inconclusive on
this data, not evidence of immunity. Since all four setup types run
through the IDENTICAL `evaluate_hold_time_aware` + `was_confirmed_types`
mechanism with no structural difference between them, and two of the
four independently reproduce the exact same bug on real data, the gate
is applied UNIFORMLY to all four in `_first_newly_confirmed` (no
per-setup_type special-casing) — not narrowed to round_number_reclaim
alone, based on what the real data actually showed, not assumed.

**Real before/after, using the actual real AIFF data and the real
`advance_journal` production path (not a synthetic reconstruction):**
- `round_number_reclaim` at the real 07:54:00 transition
  (`confirmed_at_ts=1789645800`, evaluated at `ts=1789646040`, age
  240s): OLD (`confirmation_freshness_seconds=inf`, i.e. no gate) opens
  a real position, `entry_price=0.96`. NEW (default 30.0) — `opened is
  None`, correctly blocked.
- `micro_breakout` at the real 08:04:00 transition
  (`confirmed_at_ts=1789646220`, evaluated at `ts=1789646640`, age
  420s): OLD opens a real position, `entry_price=0.9366`. NEW —
  correctly blocked.
- `test_two_symbols_journal_positions_are_fully_independent`, run
  through the REAL production wiring end to end (`create_app`/`Poller`/
  real HTTP, not a unit-level shortcut): AEHL's position now closes
  clean with NO reopening at all — the test's original, simple
  assertion (`store.open_position_for("AEHL") is None`) is restored
  verbatim, no longer needing the workaround language section 19 added.

**The load-bearing "recently confirmed, still actionable" property,
proven intact, not assumed:** the full pre-existing `monitor-app`/`core`
suite — every test that existed before this fix, including all 20+ that
the reverted "reset on reversal" attempt broke — passes unchanged (272
`monitor-app` tests, 58 `core` tests). `test_advance_journal_allows_
entry_on_a_fresh_confirmation` explicitly re-proves the specific
property using the real micro_breakout numbers (age 10s, one live bar's
worth of "the entry bar itself ticked back through the level") fires
correctly.

**Deliberate break-then-fix:** removed the freshness condition from
`_first_newly_confirmed` entirely. Result: `test_advance_journal_blocks_
entry_on_a_stale_confirmation`, `test_advance_journal_applies_the_
freshness_gate_uniformly_across_setup_types`, `test_advance_journal_the_
real_sharp_breach_scenario_reproduced_then_blocked`, and
`test_confirmation_freshness_gate_break_then_fix` all failed immediately
(the real bug reproduced exactly). Reverted; full suite green again, no
leftover markers.

**Status:** `core` suite 58 tests, `monitor-app` suite 277 tests, full
project suite 442 tests, all green. Both real risks from phase 3.6's
migration work are now FIXED and proven: backfill-only confirmation
(section 19, `watch_added_ts`) and stale same-session reconfirmation
(this section, the confirmation-freshness gate) — applied uniformly to
all four setup types per real data, not assumed narrower. Deploy still
held pending AEMD's open position clearing and current market hours
(unrelated, independent blockers, unaffected by this fix).

### 21. Reference-target display — informational only, no exit-logic change

Adds two purely informational reference values to the Virtual Position
panel, alongside the existing entry/stop/P&L rows. Does NOT change
section 6's deliberate "trailing stop only, no fixed target" design
decision in any way — a fixed R:R target was explicitly rejected for
this project because it capped winners in the EOD swing bot and
contributed to that strategy's edge not holding up under proper testing;
there is still no target anywhere in `should_enter`/`advance_journal`/
`apply_bar_to_open_position`, by design, not by omission. These two
values are display context for the user's own judgment, never consulted
by any exit or entry code path.

**The two values, both live, neither snapshotted:**
1. **Nearest-above resistance** — the SAME value already computed every
   cycle by `detect_levels` and already shown in the levels table
   elsewhere on the panel (`state["levels"]["resistance"]`), just
   surfaced again next to the open position. Deliberately NOT locked at
   entry, unlike `trail_pct_used` and this journal's other genuinely
   risk-relevant "used" snapshots (specs.md section 7) — it's read fresh
   from `slot.state` on every call, the same "full recompute keeps the
   app trivially correct" principle section 3 already establishes.
   "None on this side of price" (not a new, inconsistent phrase) when
   none exists, matching the exact language the levels table itself
   already uses for this case (`_level_block_html`).
2. **Target reference** — `entry_price * (1 + TARGET_REFERENCE_PCT)`, a
   new live-tunable strategy_param, default `0.10` (matching the user's
   stated actual target: a further ~10% push from an already-extended
   entry). `entry_price` itself is fixed once a position opens (it's
   real trade history), so this value is naturally stable across a given
   position's lifetime, but it's still computed fresh from the stored
   `entry_price` on every call rather than itself being a second stored
   field — if `TARGET_REFERENCE_PCT` changes live, an already-open
   position's displayed reference updates immediately, same live-tunable
   treatment as every other threshold in this project.

**Both rows labeled and styled distinctly from the real stop** — "(reference
only)" in both `<th>` labels, both `<td>` values rendered `muted` (the
same de-emphasis class this page already uses for non-actionable text),
so there is no ambiguity that these are context, not exit triggers,
sitting right next to the real `trailing stop` row above them.

**Explicitly out of scope, confirmed unaffected, not just asserted:**
`should_enter`, `advance_journal`, and `apply_bar_to_open_position` were
not touched — `journal_logic.py` has zero changes in this feature's
diff. The full pre-existing `test_journal_logic.py`/`test_journal_
wiring.py` suites (every entry/exit test in the project) pass completely
unmodified, proving this is genuinely display-only, not assumed from
"I didn't mean to change it." These two values are also NOT written to
the `trades` table as new "used" snapshot fields (unlike `trail_pct_
used` etc.) — they're live display, not risk-relevant history a later
review needs to see exactly as it was at entry time.

**Tests:** `_journal_open_html`/the JS `journalOpenHtml` mirror both
render the reference rows correctly (present, and the "none on this
side of price" case), and both tolerate a hand-built `open_block`
missing the new fields entirely (`.get()`, not direct indexing — a
caller from before this feature still renders, doesn't KeyError). A
Poller-level test proves liveness directly: real bars produce a real
`detect_levels` resistance level (the same known double-top shape
`core/tests/test_setup_types.py` already proves this against), the
displayed reference matches it exactly, and — the actual "not locked"
proof — mutating `slot.state["levels"]["resistance"]` between two calls
to `full_state_for` changes the displayed value immediately, with no
new entry. A separate test proves `target_reference_pct` is read live
from `JournalStore.get_param` (changing it via `set_param` updates the
displayed target immediately, while the position's own `entry_price` is
untouched).

**Status:** `monitor-app` suite 284 tests passing (7 new for this
feature); full project suite 449 tests passing. No `journal_logic.py`
changes at all.

### 22. Breakdown-below setup variants + closest-setup/open-position display fix

Two independent pieces: (1) four downside-mirror setup types, informational/
warning signals only, structurally incapable of ever firing a trade; (2)
a small wording fix to the existing closest-setup callout when a position
is already open.

**Part 1 — breakdown-below variants.** Exact downside mirrors of section
3.5's four bullish types, reusing the SAME primitives (`detect_levels`,
`evaluate_hold_time_aware` with `direction="below"` instead of `"above"`,
the same `REQUIRED_HOLD_SECONDS`/`MICRO_SWING_WINDOW`/`VWAP_PULLBACK_
THRESHOLD_PCT`) — no new detection logic invented, matching this
project's "reuse a primitive, don't reimplement" precedent:

- `support_breakdown` — nearest support level BELOW price
  (`_nearest_below`, the floor mirror of `_nearest_above`), holding below.
- `micro_breakdown` — same, at `MICRO_SWING_WINDOW`, mirroring
  `micro_breakout`.
- `vwap_breakdown` — price at/below session VWAP (a downtrend), a relief
  rally UP toward VWAP within `VWAP_PULLBACK_THRESHOLD_PCT`, then
  rejecting back below and holding — the downside mirror of `vwap_
  reclaim`'s pullback-then-reclaim pattern.
- `round_number_breakdown` — nearest round-number grid point BELOW price
  (`nearest_round_number_below`, the floor mirror of the pre-existing
  `nearest_round_number_above`, same tiered grid: dimes under $2,
  quarters $2–$10, half-dollars at/above $10), holding below. Always
  present, same reasoning as `round_number_reclaim` — there is always a
  next grid line below any positive price.

All four live in `core/setup_types.py`'s `evaluate_breakdown_setups()`, a
function with the exact same signature/sort contract as `evaluate_setups()`
(ascending by dollar distance to trigger) but returning a COMPLETELY
SEPARATE list. The four new `setup_type` strings (`support_breakdown`,
`micro_breakdown`, `vwap_breakdown`, `round_number_breakdown`) are
deliberately distinct from all four bullish ones, proven disjoint by an
explicit test — no downstream code could confuse the two lists even if
they were ever accidentally concatenated.

**Structural safety guarantee — proven, not assumed.** These setups must
never be able to fire a real entry. Two independent layers, not one:

1. **Structural separation.** `monitor-app/state.py`'s `build_state` calls
   `evaluate_breakdown_setups()` into its own `breakdown_setups` state key,
   never merged into the existing `setups` key. `app.py`'s `_update_journal`
   — the only call site that ever passes a setups list to `advance_journal`
   — reads `slot.state.get("setups", [])` exclusively; it has no reference
   to `breakdown_setups` anywhere in its body. There is no code path by
   which a breakdown candidate reaches entry-decision logic.
2. **Defense in depth: an explicit allowlist.** Investigation during this
   feature found `should_enter`/`_first_newly_confirmed` have ZERO
   awareness of setup_type identity beyond "is there a confirmed,
   not-yet-seen type" — meaning IF a breakdown-type dict were ever
   accidentally included in a `setups` list, current logic would have
   fired a trade on it, since nothing filtered by type name. Closed with
   `journal_logic.py`'s `_ENTRY_ELIGIBLE_SETUP_TYPES` frozenset (the four
   bullish types only) — `_first_newly_confirmed` now requires
   `setup_type in _ENTRY_ELIGIBLE_SETUP_TYPES` as its first condition, so
   even a hypothetical future wiring mistake could not let a breakdown
   type fire.

**The adversarial proof the user explicitly required:** `test_journal_
logic.py` constructs a `setups` list with a confirmed `support_breakdown`
candidate as the CLOSEST (lowest-distance, first-sorted) entry, alongside
a legitimately confirmed bullish type — `advance_journal` correctly skips
the breakdown candidate entirely and opens on the bullish one instead
(ineligibility is not treated as "nothing confirmed," it falls through).
A second, parametrized test proves all four breakdown types alone never
open a position. A third test (`test_breakdown_type_allowlist_break_then_
fix`) deliberately widens the allowlist via `monkeypatch` to include a
breakdown type, confirms a trade WOULD wrongly fire with the check
disabled, then restores it and confirms it's blocked again — proving this
suite can actually catch a regression here, not just that it currently
passes.

**Display.** Breakdown setups render in their own clearly-labeled section
— "⚠ Bearish signals (context only, not a trade opportunity)" — using the
same click-to-expand chip markup/CSS classes as the bullish setup chips
(`.setup-chip`/`.setup-detail`/`data-key`, so the existing delegated click
handler needed no new JS wiring), but visually and structurally SEPARATE
from the bullish closest-setup/setup-chips display: never mixed into that
ranking, never itself ranked by "closest" (these are context for the
user's own judgment, not something to chase). Both the Python renderer
(`_breakdown_setups_html`) and its JS mirror (`breakdownSetupsHtml`) stay
in sync, per this project's established dual-rendering pattern.

**Part 2 — closest-setup callout wording when a position is open.**
`should_enter` already correctly refuses a fresh entry while a position is
open for a symbol, but the "Closest setup: X @ price" callout's wording
didn't reflect that — it read like a live, actionable signal regardless of
position state. Fixed by threading `position_open` (derived from
`state["journal"]["open"] is not None`) into `_closest_setup_html`/the JS
`closestSetupHtml` mirror: the heading becomes "Setup context (position
already open, not a new signal): X @ price" when a position is open,
"Closest setup: X @ price" otherwise. The underlying setup data is left
showing either way — suppressing it was explicitly rejected; only the
framing changes. Defaults to `position_open=False` so no other call site
needed updating.

**Tests:** 10 new `core/tests/test_setup_types.py` tests (each breakdown
type detecting/confirming correctly, including a known double-bottom
fixture proving `support_breakdown` matches the same cluster `detect_
levels` is proven to find, plus the type-namespace-disjoint and
ascending-sort-order proofs) — all passing on first run after
implementation. 3 new `nearest_round_number_below` tests in `core/
tests/test_levels.py`-equivalent coverage inside `test_setup_types.py`,
mirroring the existing `nearest_round_number_above` tests exactly. 3 new
`monitor-app/tests/test_journal_logic.py` safety-proof tests (the
adversarial "closest breakdown" scenario, the per-type "alone" proof, and
the break-then-fix demonstration). 5 new `monitor-app/tests/test_state.py`
tests (the separate `breakdown_setups` key, the type-disjoint proof,
sort order, `hold.direction == "below"` for all, and `watch_added_ts`
reaching the breakdown path the same way it reaches the bullish one). 10
new `monitor-app/tests/test_app.py` tests (the closest-setup wording
change in both states, the empty-state case being unaffected by
`position_open`, the breakdown section's labeling/content, and one
real end-to-end test proving the section actually renders through the
live FastAPI app on real oscillating-price bars, not just the unit-level
renderer). Full project suite: 482 tests passing (`core` 71, `schwab-
connector` 107, `monitor-app` 304).

**Status: built, 2026-09-19.** Live-verified end to end through the real
app stack (`create_app` + `TestClient`, real bars, no hand-built state):
`breakdown_setups` correctly populated (`round_number_breakdown`,
`support_breakdown`, `micro_breakdown` all detected on a real oscillating
30-bar session) and rendered in the separate "Bearish signals" section on
the actual served page, distinct from the "Closest setup" callout showing
the bullish `round_number_reclaim` candidate.

### 23. Market backdrop — informational display only, no sizing tie-in

**The gap, closed 2026-09-19.** Section 7's last remaining data-collection
gap: "no data pipeline exists for [broad-market direction that day] at
all yet." Shows SPY's own day change (current price vs. prior close) as
global context for reading a candidate's move against the broader tape.

**Design: display only, deliberately not wired to sizing — a real
decision, not an oversight.** The original framing that motivated this
feature was "sizing down on a red day is rational, but not a reason to
dismiss a strong signal" — a genuine trading judgment call, not something
to silently bake into `risk_pct_per_trade` or any entry gate. Built as
pure context: `should_enter`/`advance_journal`/`apply_bar_to_open_position`
have NO market-backdrop-related parameter in their signatures at all —
confirmed directly (`inspect.signature`, not just code-reading), and the
full pre-existing `test_journal_logic.py`/`test_journal_wiring.py` suites
pass completely unmodified (zero-diff on `journal_logic.py` and both test
files, confirmed via `git diff --stat`) — same "prove it's display-only"
standard as section 21's reference-target feature. **If an automatic
sizing tie-in is ever wanted later, that is a separate, explicit decision
this section deliberately leaves open** — same standing-open-question
treatment as the portfolio-risk-cap question in section 7's own backlog.

**Data: reuses the existing daily-bars mechanism, pointed at SPY, not a
new pipeline.** Section 12/13's `/daily_bars/{symbol}` route (schwab-
connector) and `fetch_daily_history` (`price_history.py`) already fetch
daily candles for the session-volume gate and continuation flag — but
that existing call deliberately excludes today (`end_datetime` is
today's own NY midnight) so a still-forming session never drags the
volume average down, which meant it structurally couldn't answer "what's
SPY doing right now." Extended `fetch_daily_history` with one new
parameter, `include_today: bool = False` — default False is byte-for-
byte the pre-existing behavior every other caller (volume gate,
continuation flag) already depends on, confirmed by tests asserting
`end_datetime` is unchanged when omitted. When True, `end_datetime`
becomes `now`, so the still-forming CURRENT day's daily candle comes back
too, whose `close` is Schwab's continuously-updating last-traded price
for the session so far — exactly "current price," from the SAME
mechanism, not a new one. The route (`GET /daily_bars/{symbol}
?lookback_days=2&include_today=true`) forwards this straight through.
monitor-app's `fetch_market_backdrop(symbol)` calls this route with
`lookback_days=2`, giving exactly two candles: index `-2` (the prior
COMPLETED day's close) and index `-1` (today's forming close) —
`pct_change = (current_price - prior_close) / prior_close`.

**Honest caveat, not verified live against real Schwab in this session.**
Sections 10/12/13's own daily-bars call sites were previously verified
live against production Schwab data (section 4's real NVDA backfill).
This session has no real Schwab credentials available (same dev-
environment limitation sections 12/13 already documented), so the
assumption that Schwab's daily-candle endpoint returns a live-updating
"today" candle when the date range is extended to include it — standard
behavior for daily OHLC data mid-session, but not something this session
directly confirmed against the real API — is flagged here explicitly
rather than silently treated as proven. Everything downstream of that
API response (the `include_today` parameter's own date-range behavior,
the route's pass-through, the pct_change math, the refresh loop, and the
display) IS verified, live, in this session (below).

**Global, not per-symbol.** `Poller._market_backdrop` is a single dict on
the Poller itself, entirely separate from any `_SymbolSlot` — SPY is
never added to `_slots`, never counted against `MAX_SYMBOLS`, never
subscribed to schwab-connector's live tick stream. `GET /api/state`
exposes it as a new top-level `market_backdrop` key, sibling to
`symbols`/`current_equity`/`strategy_params`, not nested under any
symbol.

**Refresh: its own independent periodic REST poll, not a 5th streaming
slot.** `Poller.run_market_backdrop_loop()` is a separate `asyncio` task
(started in `create_app`'s lifespan alongside, but independent of, the
per-symbol push/stream tasks): fetches immediately on start, then every
`market_backdrop_refresh_seconds` (default 180 — a few minutes; this
context doesn't need sub-second freshness the way a watched candidate
does). A failed fetch, or a fetch returning fewer than 2 bars, leaves
`status: "unknown"` rather than crashing the loop, holding a stale value,
or showing a fabricated 0% — same "unknown, not silently zero" treatment
`avg_daily_volume`/the continuation flag already use. No-ops forever if
`fetch_market_backdrop` was never configured (same optional-dependency
pattern as `fetch_daily_bars`).

**Display.** A single line (`id="market-backdrop"`) rendered once at the
page level, above the current-equity/strategy-params lines, near the
watch-form's own "N/4 symbols watched" line — never duplicated inside
any of the up-to-4 symbol panels. "Market backdrop: SPY +0.42% (417.32,
prior close 415.58)" when known, colored `pos`/`neg` by sign (reusing
`_sign_class`, the same helper the MACD histogram and P&L cells already
use); "Market backdrop (SPY): unknown (not yet fetched)" when not. Both
the Python renderer (`_market_backdrop_html`) and its JS mirror
(`marketBackdropHtml`) patched via `outerHTML` (not `innerHTML`, since
the element's own CSS class changes between the two states) on every
push, same dual-rendering discipline as every other display feature this
project has built.

**Tests:** 8 new schwab-connector tests (`include_today`'s date-range
behavior and default-False preservation in `price_history.py`, the
route's pass-through and default in `app.py`). 12 new monitor-app tests:
`_market_backdrop_html`'s unknown/positive/negative-day rendering; the
page-level-once-not-per-panel proof (guarding against the same "matches
the JS mirror's own source text" false positive
`test_root_page_renders_closest_setup_and_chips_for_other_candidates`
already documented, by asserting on the single-quoted Python-rendered
attribute specifically); the periodic-refresh-cadence proof (a 0.05s
test interval, asserting 3+ fetches land); the refresh-runs-with-zero-
watched-symbols proof (this poll is not driven by, or dependent on, any
watched symbol); the not-configured and fetch-failure paths staying
`"unknown"` rather than crashing; the too-few-bars-returned path; the
configurable-symbol proof; and the `inspect.signature` structural proof
that `should_enter`/`advance_journal` have no market-backdrop parameter.
Full project suite: 498 tests passing (`core` 71, `schwab-connector`
112, `monitor-app` 315).

**Verified live (2026-09-19), against the real FastAPI app (`create_app`
+ `TestClient`), using realistic representative data — real epoch-second
timestamps converted from real 2026-09-17/2026-09-18 America/New_York
dates (not a loop index, learned the hard way in section 13), real
SPY-scale prices ($415.58 prior close, $417.32 current).** `GET
/api/state`'s `market_backdrop` showed `{"status": "ok", "symbol": "SPY",
"current_price": 417.32, "prior_close": 415.58, "pct_change": 0.004187,
"as_of_ts": 1789738200}` — `pct_change` independently hand-computed as
`(417.32 - 415.58) / 415.58 = 0.0041869...` rounds to the exact same
`0.004187`, and `as_of_ts` converted back to `2026-09-18 09:30:00
America/New_York`, the exact date used. The rendered page showed `Market
backdrop: SPY +0.42% (417.32, prior close 415.58)`, positive-day styled.
With `market_backdrop_refresh_seconds=0.3` and zero symbols watched at
all, 4 real fetch calls landed over 1.2 real seconds with measured
intervals of `0.301s` each — matching the configured cadence to the
millisecond and confirming the poll runs independently of, and is not
gated by, any watched symbol. `git diff --stat` on `journal_logic.py` and
both its test files showed zero changes for this entire feature.

### 24. Human review/labeling for closed trades

**The gap, closed 2026-09-19 — the last of section 7's data-collection
gaps.** Every prior feature this journal has built logs the system's OWN
mechanical decisions (what setup confirmed, what the stop did, what
volume gate passed). This is the first place a HUMAN judgment gets
attached to a trade after the fact — "was this a genuine, trustworthy
signal, or did it just happen to work out (or not) by chance?"

**Scope: closed trades only.** A trade can only be reviewed once it has
an `exit_ts` — an open position's outcome isn't known yet, so labeling
it doesn't mean anything yet. `JournalStore.review_trade` checks this
FIRST, before any other validation, and raises `InvalidReviewError`
(nothing written) for a still-open trade, with a message that says so
plainly, not a generic "invalid" — proven by test and live (below), not
just documented.

**review_label's three categories, chosen deliberately along the SIGNAL
QUALITY axis, not the outcome axis** (outcome is already fully captured
by `realized_pnl_pct`/`exit_reason` — a label duplicating that would add
nothing):
- **`clean_signal`** — the setup was genuine and well-formed; entry
  criteria were legitimately met and it reads correctly in hindsight. A
  clean signal can still LOSE (normal variance) without becoming
  `bad_signal` — this is about whether the signal itself was
  trustworthy, not whether it happened to win.
- **`lucky`** — won, but not because the signal was actually sound;
  credits the outcome to chance rather than the setup.
- **`bad_signal`** — the setup itself was flawed or questionable (chop,
  a marginal/false confirmation, thin volume), regardless of outcome — a
  loss here is a deserved one, not variance.

`review_note` (free text) gets the SAME length-cap treatment as
`watch_note`/reverse-split notes (500 chars, rejected outright rather
than truncated) — its own independently-validated constant
(`MAX_REVIEW_NOTE_LENGTH`), per this project's "each note field
validated on its own" precedent (specs.md section 8), even though the
number happens to match. `ideal_entry_price` (optional, nullable,
must be positive if given) is the feature's original motivating
intent — "where would you actually have entered" — for later comparison
against what the system's mechanical logic actually did; not read or
used by anything else in this codebase yet.

**Storage: plain mutable columns on `trades`, no history table —
deliberately different from watch_notes/reverse_splits.** Those two
needed an append-only history table because a symbol's reason for being
watched, or its reverse-split history, can genuinely accumulate distinct
real events over time. A review is different in kind: a single, current,
replaceable judgment about ONE already-finished, immutable-outcome
trade. Re-reviewing just overwrites the existing `review_label`/
`review_note`/`ideal_entry_price` in place — proven by test AND live
(below) to update the same row, not create a duplicate. Migrated onto an
existing `trades` table the same way every prior column addition was
(`_ADDED_COLUMNS`, `_migrate_added_columns` on connect) — a pre-existing
row simply reads back with all three as NULL, never confused with any of
the three real label categories.

**Endpoint:** `POST /api/trades/{trade_id}/review`, JSON body
(`review_label`/`review_note`/`ideal_entry_price`, each independently
optional — e.g. jotting a note without committing to a category is
valid). Full-replace semantics, matching the storage design above: the
three stored values become exactly what's in the request, not merged
with whatever was there before. Validates `review_label` against the
defined set, `review_note`'s length, and `ideal_entry_price`'s
positivity, and rejects an unknown or still-open trade id — all as a
clean `409` with a specific reason, same convention as every other
validated endpoint in this app (reverse-splits, watch-note,
strategy-params).

**Display: extends the existing closed-trades table, no separate
review page** (per the user's explicit instruction). A new "review"
column shows the current label as a colored badge (`pos`/`neg`/
`pending`-toned, matching the badge-confirmed/badge-pending styling this
page already uses) or "not reviewed" when none, plus a "review" button.
Clicking it reveals a hidden sibling `<tr>` (a form row, since this
lives inside a `<table>` and can't nest an arbitrary block inside a
`<td>` the way a card can) with a label `<select>`, a note `<input>`,
and an ideal-entry-price `<input>`, pre-filled with whatever's currently
stored — same click-to-expand pattern as this page's setup-chip/
setup-detail, reusing its CSS classes and event-delegation style so no
new interaction pattern was invented. A "Save review" button POSTs the
full form and refreshes the row from server truth on success; a failed
save shows the real rejection reason in place and does NOT refresh away
the error. Expanded/collapsed state for these form rows survives a
push-driven table rebuild the same way setup-chip's own expanded state
already did (specs.md section 5's 2026-09-17 fix) — a push landing
mid-edit no longer silently collapses the open form. Both the Python
renderer and its JS mirror stay in sync, per this project's established
dual-rendering discipline.

**Forward-looking note, explicitly NOT built now:** a future win-rate/
expectancy analysis could reasonably want to segment by `review_label`
— e.g. performance among trades marked `clean_signal` specifically vs.
overall. That's a later capability, contingent on enough real reviewed
trades accumulating over time; nothing here implements it, and nothing
in this feature's design blocks building it later (the label is a plain
queryable column on `trades`).

**Tests:** 10 new `journal_store.py` tests (store/retrieve all three
fields, the open-trade rejection, the unknown-id rejection, re-review
updating in place with a row-count assertion proving no duplicate, the
unrecognized-label/over-length-note/non-positive-price rejections, a
partial-fields-note-only case, and the pre-migration-table proof). 13
new `app.py`/route tests (the badge/not-reviewed/prefilled-form display,
`.get()`-tolerance for a trade dict with none of the three keys, the
full HTTP round trip storing correctly, the open-trade 409 with an
"open" substring check on the actual message, the unknown-id 409, the
re-review-updates-not-duplicates proof through the HTTP layer, the
invalid-label/over-length-note/non-numeric-price 409s, the journaling-
disabled 409, and a real end-to-end page-render proof). Full project
suite: 521 tests passing (`core` 71, `schwab-connector` 112,
`monitor-app` 338).

**Verified live (2026-09-19), against the real FastAPI app (`create_app`
+ `TestClient`) with a real SQLite `JournalStore`, real `OpenPosition`/
`ExitEvent` primitives (not hand-built dicts) creating a genuinely closed
trade (AEHL, entry 10.00, exit 11.00, `trailing_stop`) and a genuinely
still-open one (MSFT).** `POST /api/trades/{id}/review` on the closed
trade returned `200 {"ok": true, "reason": ""}`; the rendered page then
showed the `clean_signal` badge and the exact submitted note text.
`POST .../review` on the OPEN trade returned `409 {"ok": false, "reason":
"trade 2 is still open; only closed trades can be reviewed"}` — the
exact clear rejection message required, not a generic failure. A second
review call on the SAME closed trade (`lucky`, a different note/ideal
price) returned `200`, `store.recent_closed()` still showed exactly ONE
row (not two), and the rendered page's badge `class` attribute was
independently confirmed to have changed from
`review-label-clean_signal` to `review-label-lucky` — the old class
string gone, the new one present, checked precisely against the
specific `class='review-label review-label-X'` attribute rather than
loose substring matching, after an initial loose check falsely flagged
a mismatch (the old label's name legitimately still appears as plain
text inside the review form's own always-fully-listed `<select>`
options — traced and ruled out, not a real bug, before this section was
marked done; same "loose substring match on rendered HTML can produce a
false positive from unrelated markup" pitfall this session's own prior
features already documented for `id="market-backdrop"` and `Closest
setup:`).

### 25. Phase 3, stage 0 — CLI-auth feasibility investigation (no code built)

**The question, before designing anything else in phase 3.** Section 5's
`claude-connector` placeholder had assumed "no API key, CLI auth" without
ever confirming that assumption was viable unattended, inside a
container, with no browser available to complete an interactive login.
This stage answered exactly that question, and only that — no narration
logic, no event-detection design, no service scaffolding was built here.

**Finding: yes, confirmed live, with one important caveat flagged
honestly rather than assumed away.** Claude Code IS installed on the
host, authenticated via an OAuth session (`~/.claude/.credentials.json`)
from a Pro subscription — not an API key. A minimal container matching
this project's existing service pattern (`python:3.12-slim`, same base
image as `schwab-connector`/`monitor-app`), with ONLY `~/.claude/
.credentials.json` bind-mounted read-only (no full `~/.claude`, no
`~/.claude.json` — tested and found NOT required at all), ran `claude -p
"..."` non-interactively and got back a real, verified model response
(a `47 * 89` arithmetic check, not just an echo). The container's
filesystem was inspected and confirmed to hold ONLY the one mounted
file — no session history, no other config, nothing left over from an
interactive login. Confirmed reproducible across multiple independent
container runs, and the host's real credential file's mtime was
confirmed unchanged afterward (a read-only mount, verified not just
assumed).

**What's genuinely still open, not solved, flagged rather than
assumed:**
1. **Token refresh was never exercised.** The access token expires in a
   few hours; the refresh token lasts about 12 days. Forcing an actual
   expiry to test refresh-in-a-container was judged unsafe to do
   unprompted against this session's own live, in-use credentials — a
   real, legitimate test that remains undone. Stage 1 (section 27)
   treats any invocation failure as this exact risk materializing, by
   design, with no "probably transient" special-casing.
2. **This borrows a personal Pro subscription session** for an automated
   backend process — a usage-pattern question distinct from the
   technical one, requiring explicit human sign-off before phase 3
   depends on it long-term, not something to silently normalize.
3. **The standard production alternative**: `ANTHROPIC_API_KEY` (a
   dedicated key, usage-based billing) sidesteps both issues above
   entirely. Not tested (no key available in this environment), named
   here as the clean fallback if OAuth-in-a-container proves unreliable
   under real always-on load.

No specs.md/claude-connector/git changes were made in this stage —
purely investigation, reported back before any of phase 3's actual
design work began (section 27).

### 26. Loss monitoring & evaluation view — priority shift away from the portfolio risk cap

**Priority shift, decided explicitly, not silently.** Section 7's
"strategy gaps" list has carried "no portfolio-level risk cap across the
4 concurrent symbol slots" since the list existed. This section
DEPRIORITIZES that gap (not abandons it — see section 7's updated entry)
in favor of building this evaluation view first. Reasoning: a risk cap
protects against a downside that doesn't actually exist here — this is
paper trading, a "loss" costs nothing real, so a limit on it is solving
a problem this project doesn't currently have. What DOES matter, and
what this project has not yet built any way to check, is whether the
strategy's own entry/exit LOGIC is trustworthy at all — the actual
purpose the virtual journal (section 6) and human review labels (section
24) were both built toward. A risk cap without first knowing whether the
thing it would be capping is sound is solving the wrong problem first.

**Standing rule, reused not reimplemented (specs.md section 6).** Every
stat in this feature filters through `analysis.real_trades()` — the ONE
place `REAL_TRADE_EXIT_REASONS` (currently `{"trailing_stop"}`) is
defined; every other function in `monitor-app/analysis.py` calls it
first, directly or transitively, none reimplements the check.
`symbol_switched` rows (watchlist housekeeping) are excluded from
literally every number this view shows — proven directly by test AND
live (below) against a mixed real/housekeeping dataset with deliberately
extreme housekeeping P&L (+900%/-99%) that must never leak into any real
stat.

**Core aggregate stats (`monitor-app/analysis.py`, pure functions, no
I/O — same "core is pure, the app layer does I/O" separation section 3
already establishes):**
- `overall_stats()` — win rate and expectancy (mean `realized_pnl_pct`)
  across every real trade. `win_rate = wins / count` (breakeven trades
  counted in the denominator but neither wins nor losses — a trade that
  closes exactly flat is genuinely neither, and forcing it into either
  bucket would misstate both).
- `breakdown_by_setup_type()` — the same win/loss/expectancy block,
  grouped by `setup_type`, answering "which of the four entry-eligible
  types produces the best/worst real results." A trade with no
  `setup_type` at all (a pre-migration row) groups under an explicit
  `"unknown"` key, never silently dropped.
- `breakdown_by_review_label()` — grouped by `review_label` (section 24),
  among only the REVIEWED subset of real trades (an unreviewed trade has
  no label to group under). Reports `reviewed_count`/`total_real_count`
  explicitly alongside the breakdown, so the reader can see how much of
  the real history has actually been reviewed, not just how the reviewed
  slice happens to break down. This is the actual point of the feature:
  does `bad_signal` correlate with losses (validating the label means
  something), and can `clean_signal` trades still lose sometimes
  (expected and healthy — not every good signal wins)? Verified live
  (below) on real hand-traced data: `bad_signal` trades came back 0%
  win rate / -10.28% expectancy, `clean_signal` came back 100% win rate
  / +14.17% expectancy in that run — a real, checkable separation, not
  asserted from nothing (and a dedicated unit test separately proves a
  `clean_signal` trade CAN still show a loss without that being treated
  as a contradiction).

**Losses section — the actual priority, not an afterthought
(`losses_section()`).** Average loss size (both `%` and `$`, the latter
honestly `None`/`—` rather than a fabricated `$0` when `realized_pnl_
dollars` was never computed for a trade — same "unknown, not silently
zero" treatment this project uses throughout). Then CLUSTERING across
four dimensions — `setup_type`, `review_label`, `symbol`, and entry
hour-of-day (America/New_York, the same exchange-local timezone
convention `state.py` already anchors session VWAP to) — each reported
as a per-bucket LOSS RATE compared against the overall loss rate, not
raw loss counts. This distinction matters: a bucket with more trades
overall will also tend to have more raw losses without that meaning
anything about risk concentration; comparing RATES answers "does this
bucket actually lose more often than average," the real question behind
"is this expected noise or a sign something's wrong." A bucket is only
ever flagged `elevated_vs_overall` when its own sample size clears
`MIN_BUCKET_SIZE` (3) — proven by a dedicated test that a single losing
trade in an otherwise-untested bucket (100% loss rate, n=1) is NOT
flagged, since a coin flip isn't a pattern, and verified live: every
by-symbol and by-hour bucket in the real demo run below had exactly 1-2
trades and NONE were flagged elevated, even the ones showing a 50-100%
loss rate in isolation — correctly read as "too few to assess," not a
real signal.

**Honesty requirement — explicit output, not silent omission or a
falsely confident number (echoing this project's own EOD-swing-bot
early-small-sample lesson, cited by name in the originating
instruction).** Two sample-size floors, both in `analysis.py`:
`MIN_TRADES_FOR_STATS` (10) gates a whole group's `win_rate`/
`expectancy_pct` — below it, `sufficient_sample=False` and an explicit
`note` string is included (e.g. `"based on only 2 trades -- too few to
be statistically meaningful (want at least 10)"`), rendered directly in
the table, never hidden and never silently omitted. `MIN_BUCKET_SIZE`
(3) is a deliberately lower floor for the losses section's narrower,
per-bucket clustering question — requiring 10 trades in every individual
symbol/hour bucket would report nothing at all against realistically
thin early data (confirmed live: every real bucket in the demo run below
had 1-4 trades, well under 10). The numbers themselves are still always
shown — the caveat sits alongside them, not in place of them; this
project's own instruction was explicit that omitting a real number
outright would just be a different way of hiding information, the goal
is not presenting it as MORE confident than it is.

**Display: a SEPARATE view, not crammed into the 4-panel live layout —
a deliberate design choice, not something to ask about.** `GET
/analysis`, linked from the live page's topbar (`href='/analysis'`),
plain server-rendered HTML tables, computed fresh on every page load
directly from `Poller.recent_closed(limit=None)` — the FULL closed-trade
history, not the live page's own most-recent-10 window (`recent_closed`
grew a `limit=None` mode for exactly this; every existing caller still
passes an explicit int, so nothing else changed behavior). No
EventSource/JS-mirror dual-rendering here, unlike the live page — this
is retrospective batch analysis over accumulated history, a genuinely
different mode from live monitoring, and nothing on this page needs
sub-second freshness; revisiting it already recomputes from current
data. Simple, plain tables over visually elaborate, per the feature's
own explicit instruction — this is for genuine review, not a dashboard
to look impressive.

**Tests:** 25 new `monitor-app/analysis.py` unit tests (the exit_reason
filter proven against a realistic mixed dataset, `overall_stats`'
win-rate/expectancy/breakeven math, both breakdowns' correct grouping
and "unknown"/reviewed-only handling, the losses section's average-loss
math and honest handling of missing dollar amounts, and the clustering
logic's elevated-vs-overall proof AND its below-the-sample-floor
non-proof). 1 new `journal_store.py` test (`recent_closed(limit=None)`
returns every row, ordering unchanged). 8 new `app.py`/route tests (the
real-mixed-dataset filtering proof through the actual HTTP layer, both
breakdown displays, the explicit insufficient-data messaging, the
zero-real-trades clean message, the losses section's rendering, the
nav-link reachability, and a proof that none of this feature's markup
leaks into the live page's own `#symbols` grid). Full project suite:
555 tests passing (`core` 71, `schwab-connector` 112, `monitor-app`
372).

**Verified live (2026-09-19), against the real FastAPI app (`create_app`
+ `TestClient`) with a real SQLite `JournalStore` and a realistic
hand-constructed 13-row history (10 real `trailing_stop` trades across
all four setup types, some reviewed some not, plus 3 `symbol_switched`
housekeeping rows with deliberately extreme P&L) — every displayed
figure independently hand-traced and confirmed exact, not just visually
plausible.** Overall: 10 real trades (the 3 housekeeping rows correctly
excluded — their +900%/-99% figures appear NOWHERE on the rendered
page), win rate 50.0%, expectancy +0.53% (hand-summed: 12 - 7.5 + 18 - 8
+ 5 - 10 + 12.5 - 8.33 + 5 - 13.33 = 5.33, / 10 = 0.533, rounds to
0.53 — exact match). By setup type: `vwap_reclaim` +5.00% (hand-computed
(18-8)/2), `round_number_reclaim` -2.50% ((5-10)/2), `micro_breakout`
+2.08% ((12.5-8.33)/2), `resistance_breakout` -0.96% ((12-7.5+5-13.33)/4)
— all four exact matches, all four correctly flagged insufficient (2-4
trades each, well under 10). By review label: `bad_signal` (3 trades,
all losses) -10.28% expectancy exactly matching hand-sum
(-7.5-10-13.33)/3; `clean_signal` (3 trades, all wins) +14.17% exactly
matching (12+18+12.5)/3; both correctly flagged insufficient (n=3).
Losses: 5 of 10, average loss -9.43% (hand-sum of the 5 losing trades'
percentages / 5), average loss in dollars correctly shown as `—`
(`realized_pnl_dollars` was never computed for these hand-built trades —
honestly absent, not a fabricated `$0`). Clustering: `bad_signal`
correctly flagged `elevated vs. overall` (3/3 losses = 100% vs. the
10-trade overall rate of 50%, n=3 clears `MIN_BUCKET_SIZE`); every
by-symbol and by-hour-of-day bucket (each with only 1-2 trades) was
correctly left unflagged despite several individually showing 50-100%
loss rates in isolation — exactly the "don't mistake a coin flip for a
pattern" behavior the sample-size floor exists to enforce, confirmed
against real rendered output, not just asserted by a unit test in
isolation.

### 27. Phase 3, stage 1 — event-triggered narration (heavy tier only), with required safety gates

**Scope, deliberately narrow.** Following stage 0's confirmed-viable CLI
auth (section 25), this stage builds the actual narration loop — but
ONLY the heavy tier, ONLY three trigger events, reusing detection that
already exists rather than inventing new "was this meaningful" logic:
1. A setup's `hold_confirmed` transitioning False→True, for one of the
   four entry-eligible bullish setup types — reuses `journal_logic.
   advance_journal`'s own already-computed `confirmed_types_after`
   diffed against the prior tick's `was_confirmed_types`, the SAME
   bookkeeping `should_enter`'s freshly-confirmed check already relies
   on. Breakdown-below types (section 22) have no such transition
   tracking anywhere in this codebase and are deliberately excluded
   rather than building new tracking for them.
2. A real entry firing (`advance_journal`'s `tick.opened`).
3. A real exit firing, with its P&L (`advance_journal`'s `tick.closed`).

Ongoing lighter-tier updates and off-tab push delivery (e.g. a native
notification when the browser tab isn't focused) are explicitly
DEFERRED, not forgotten — a separate, later pass once this core loop is
proven reliable, not bundled in here.

**Architecture: `claude-connector` becomes a real service, matching the
credential-isolation boundary section 5 already designed for it.** The
ONLY container with the `claude` CLI's OAuth credentials mounted
(read-only, the ONE file the stage-0 investigation proved sufficient —
`~/.claude/.credentials.json`, nothing else). `monitor-app` holds no
Claude credentials at all and never shells out to `claude` itself — the
same isolation discipline already applied to Schwab auth
(`schwab-connector` holds it, `monitor-app` doesn't). `claude-connector`
does exactly one job: `POST /narrate {"prompt": "..."}` → runs `claude -p
<prompt>` as a real subprocess (`claude_cli.run_claude_prompt`, its own
thin module, mirroring `schwab-connector`'s `stream.py`/`price_history.py`
split) → returns `{"ok": true, "text": "..."}` or `{"ok": false, "error":
"..."}` (`502`), never an unhandled exception. No trigger-detection,
prompt-design, or safety-gate logic lives in `claude-connector` at all —
all of that is `monitor-app`'s job, reusing state that already exists
there.

**A real bug found and fixed while building `claude_cli.py`'s own test
suite, not assumed away:** killing a hung subprocess (the timeout path)
left `proc.wait()` hanging for the FULL original duration regardless of
the kill, because a grandchild process (e.g. the fake test script's own
`sleep`, or potentially a real helper `claude` itself spawns) inherits
the stdout/stderr pipes and keeps them open after the direct child dies.
Fixed by spawning in a new process group (`start_new_session=True`) and
killing the WHOLE group (`os.killpg`) on timeout — a genuine correctness
fix for production use, not a test-only workaround, and locked in by a
dedicated regression test asserting the timeout path returns promptly
(under 5s), not after the original ~30s duration.

**Safety gate 1 — rate-limit circuit breaker.** Tracks a rolling window
of call timestamps (`narration.prune_and_record_call`/`breaker_should_
trip`, pure functions). More than `narration_max_calls_per_window` calls
within `narration_window_minutes` trips it — blocking ALL further
narration calls until an explicit, manual `POST /api/narration/
reset_breaker` (never an automatic cooldown; a trip means something
worth a human actually looking at). The call that CROSSES the threshold
still fires (`"more than N calls ... trip it"` — the tripping call is
itself allowed, only calls AFTER it are blocked), proven by a dedicated
test distinguishing "correctly trips" from "correctly blocks further
calls," not just asserting the trip flag. Defaults, reasoned through
explicitly (`journal_store.py`'s own `_PARAM_BOUNDS` comment carries the
full reasoning): **10 calls / 15 minutes.** Expected real volume even at
4 concurrently watched symbols on a genuinely busy session is low — each
symbol realistically produces at most a handful of the three trigger
events in any 15-minute window, so 4-8 total is a busy-but-normal
ceiling; 10 sits just above that. A real bug (e.g. a debounce failure
firing on every live 10s bar) would produce dozens of calls per symbol
in the same window, tripping almost immediately, not after meaningful
damage. Both values live-tunable via the existing `strategy_params`
mechanism (section 8).

**Safety gate 2 — mandatory hourly re-arm, independent of gate 1.**
Narration only fires while "armed" — a bounded, rolling duration
(default `narration_rearm_minutes` = 60, live-tunable) from the last
explicit `POST /api/narration/arm`. Once expired, narration goes
dormant (no calls fire) until a human explicitly re-arms it — expiring
is NOT a "trip," just a return to the safe default state, and re-arming
while already armed simply extends the window (the same action, not a
distinct one). `narration.is_armed(armed_until, now)` is a pure,
one-line comparison — no flag to actively flip on expiry, "armed" is
just never true again once `now` passes `armed_until`.

**Both gates default to disarmed/not-tripped on every restart, by
construction, not by a reset step that could be forgotten.** All
narration state (`_narration_armed_until`, `_narration_call_timestamps`,
`_narration_breaker_tripped`, and the narration log itself) lives ONLY
in-memory on `Poller` — never written to `journal_store`/SQLite. A fresh
process start means a fresh `Poller`, which means these fields are back
at their dataclass-level starting values with nowhere they could have
survived from. Proven by a dedicated test creating a SECOND, fresh
`Poller`/app instance sharing the SAME `journal_store` as a first
instance that had armed and tripped — the second instance shows
disarmed and not-tripped, the same "resumed across a restart" test shape
this project already uses for open-position resume.

**Failure handling: any invocation failure is caught, logged, and
surfaced — never silently swallowed, and NEVER given "probably
transient" special treatment** (per stage 0's explicitly flagged,
still-untested OAuth-refresh risk — a real failure here gets treated
exactly like any other, by design). `Poller._fire_narration_call`
records the call against the rate-limit window BEFORE making it (an
ATTEMPT counts even if it then fails — a runaway bug producing rapid
failing attempts must still trip the breaker), then catches literally
any exception from the real `claude-connector` call (a non-2xx status, a
connector-reported failure, a network error, a malformed body) and
records it in the narration log as an explicit `{"ok": false, "error":
"..."}` entry — visible both in `GET /api/state`'s raw JSON and, styled
distinctly (`neg`-colored, prefixed "FAILED"), in the rendered page.
Proven live (below) and by a dedicated test that a narration failure
never crashes or blocks the real journal logic it's commenting on — the
real entry/exit still happens correctly regardless of narration's own
outcome.

**Never awaited inline in the bar-processing path.** A narration call
(up to `claude-connector`'s own 30s subprocess timeout) fires as a
detached `asyncio` background task (`Poller._maybe_narrate`, referenced
in `self._narration_tasks` to prevent premature garbage collection, a
real asyncio gotcha for a bare unreferenced `create_task()` call) —
`_update_journal` itself stays fully synchronous, unchanged in shape,
so a slow or hung `claude -p` call can never delay processing new bars
or the next poll cycle for any watched symbol. Cancelled at shutdown
alongside the other background tasks.

**Display: reuses the existing SSE push infrastructure, no second push
mechanism built.** `narration.status`/`narration.log` join the SAME
`_state_payload()`/`_broadcast_state()` every other live field on this
page already flows through (`GET /api/state`, `GET /api/state/stream`).
A new "Event-triggered narration" section on the live page (not the
retrospective `/analysis` view — this is live commentary on live events,
the opposite mode) shows armed/disarmed with time remaining, circuit-
breaker status, an "Arm / re-arm" button (always enabled) and a "Reset
circuit breaker" button (disabled client-side unless tripped — the real
guard is server-side, `POST /api/narration/reset_breaker` itself refuses
when not tripped, never trusting the client alone, same standard as
every other guarded action in this app), and the narration log itself,
most-recent-first, in-memory only (capped at `NARRATION_LOG_MAX_ENTRIES`
= 50 — no durable history table this stage, a deliberate scope choice:
narration is commentary on events that are ALREADY durably logged
elsewhere, so losing the commentary text on a restart loses nothing
structurally, and both safety gates already reset regardless).

**Tests:** 17 new `narration.py` unit tests (the pure trigger-diff/
prompt-composition/gate-arithmetic logic). 7 new `claude_cli.py` tests
against real tiny fake `claude` shell scripts (success, non-zero exit,
timeout, empty output, missing binary, the prompt reaching the real
subprocess, and the process-group-kill timing regression). 5 new
`claude-connector` `app.py` tests (the `/narrate` route's success/
failure/timeout paths never producing an unhandled 500, `/health`). 4
new `journal_store.py` tests (the three narration params' live-tunable
bounds). 11 new `test_journal_wiring.py` tests — REAL bar sequences
already verified elsewhere in this project's own suite to produce
genuine `hold_confirmed` transitions/entries/exits, replayed through the
real `Poller`/`advance_journal` pipeline: both trigger-firing-when-armed
tests, the not-firing-when-disarmed test, the circuit breaker's trip AND
block (proven separately), the reset-and-resume proof, the reset-
rejected-when-not-tripped proof, both re-arm gate tests (dormant after
expiry, resumes only after explicit re-arm), both restart-defaults
tests, and the failure-surfacing proof. 8 new `app.py` display tests.
Full project suite: 607 tests passing (`core` 71, `schwab-connector`
112, `claude-connector` 12, `monitor-app` 412).

**Verified live (2026-09-20).** `claude-connector`'s actual Dockerfile
(the native, non-npm `claude` CLI installer, matching the stage-0
investigation's own finding that the host's install is this same
standalone binary) was built and run as a real container — `docker build
-f claude-connector/Dockerfile` succeeded non-interactively (the
installer needs no interactive input), `docker run ... claude --version`
confirmed the binary present and runnable, and with ONLY the host's real
`~/.claude/.credentials.json` bind-mounted read-only, `POST /narrate`
against the running container returned a real, verified `claude -p`
response end to end through the actual service — not a stub.

**Then the full trigger pipeline, live, against that same running
container.** The real `monitor-app` `Poller`/`create_app` (armed via the
real `POST /api/narration/arm`) was fed two real bar sources: (1) real
captured historical AIFF market data (`schwab-connector/data/bars/
AIFF.jsonl`, the same real, previously-captured data this project's own
phase 3.6 proofs already used — first 3,541 real bars, spanning real
timestamps `2026-09-17 07:50:00` through `2026-09-18 00:00:20`
America/New_York) replayed through the
actual `build_state`/`advance_journal` pipeline unmodified; (2) the
project's own already-established, verified fixture sequence
(`test_journal_wiring.py`'s `_entry_bars`/`_ratchet_bars`/
`_sharp_breach_bar`, proven elsewhere in this exact suite to produce a
real entry+exit through this same pipeline), used for the entry/exit
narration proof specifically since a genuine real ENTRY did not occur
within the real AIFF window scanned (entry needs both a fresh
confirmation AND a real volume spike, confirmed rare even relative to
confirmations alone within this window — 14 real confirmation events
found, 0 real entries).

**7 real `claude -p` calls fired, all seven succeeded, covering all
three trigger types:**
- **5 confirmations** — 4 from the REAL AIFF historical data (`vwap_
  reclaim` at the real 1.24 trigger, `resistance_breakout` at the real
  1.2699 trigger — confirmed twice as price held — and `round_number_
  reclaim` at the real 1.30 trigger) plus 1 from the fixture
  (`round_number_reclaim` at 9.25). Real example output: *"AIFF has
  reclaimed VWAP and is holding above the 1.24 trigger, with price now
  essentially right at that level — a bullish signal, but with zero
  cushion, a trader would want to see it hold above 1.24 rather than
  immediately slip back below it."*
- **1 entry** — the fixture's real `round_number_reclaim` entry at
  $9.10, 43 shares. Real output: *"Long entry logged: SYNTH triggered a
  'round number reclaim' setup at $9.10 for 43 shares — essentially
  betting the stock holding above that psychological $9 level confirms
  bullish continuation."*
- **1 exit** — the fixture's real stop-out. Real output: *"Your
  simulated SYNTH position from the 9.1 entry got stopped out at 9.0545
  when the trailing stop triggered, closing for a small loss of about
  -0.50% (-$1.96). Basically a minor, controlled exit rather than a
  significant drawdown."* Independently cross-checked against the real
  `journal_store` row for this trade, not just trusted: `realized_pnl_
  pct = -0.5000...`, `realized_pnl_dollars = -1.9565` — the narration's
  stated `-0.50%` / `-$1.96` match the actual stored record exactly, not
  just plausibly.

Final `narration_status` after all seven calls: `armed=true` (not yet
expired), `breaker_tripped=false`, `recent_call_count=7` (well under the
default 50-call test threshold used for this run) — confirming the
safety gates stayed correctly out of the way of legitimate, real
traffic while remaining ready to trip on real overuse (proven separately
and precisely by the dedicated breaker tests in `test_journal_wiring.py`,
using a deliberately low threshold).

### 28. Roadmap / phases

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
   **Stage 0 (investigated, 2026-09-19) — see section 25:** confirmed
   live, with a caveat flagged rather than assumed away, that `claude -p`
   works non-interactively from an unattended container via the host's
   existing OAuth credentials, before any of the rest of this phase was
   designed. **Stage 1 (built, 2026-09-20) — see section 27:** the
   heavy-tier, event-triggered narration loop itself, exactly the three
   trigger events named above's own real analogues (a setup's
   `hold_confirmed` transition, a real entry, a real exit — the MACD-
   cross/retest/sharp-reversal examples in this original roadmap line
   remain a LATER, lighter-tier pass, not built now), gated behind a
   rate-limit circuit breaker and a mandatory hourly re-arm, both
   defaulting to off on every restart. `claude-connector` is now a real
   service, no longer a placeholder. Ongoing lighter-tier updates and
   off-tab push delivery remain explicitly deferred.
3.5. **(built, 2026-09-17) Multi-scenario setup evaluation.** See section
   3 (`core/setup_types.py`) for the four setup types and the
   dollar-distance comparison metric, and section 5 for the grid UI.
   Bullish/breakout-ABOVE direction only for this pass, deliberately — a
   symmetric breakdown-below version of each type was a natural future
   extension, not built at the time. **Built 2026-09-19 — see section
   22:** the four breakdown-below mirrors, informational/warning signals
   only, structurally (and, in defense-in-depth, via an explicit
   allowlist) incapable of ever firing a trade.
3.6. **Time-aware core indicators.** Rework `ema`/`macd`/`relative_volume`/
   `evaluate_hold`/`detect_levels`'s swing-point window (section 3) to
   decay/compare/require by elapsed real time rather than by bar count,
   replacing the `live_cadence_tail` stopgap (section 3, "Backfill vs.
   live bar width") that currently just excludes backfilled bars from the
   window-based functions instead of correctly weighting them. Also fixes
   the irregular gaps *within* the backfilled portion itself (Schwab
   skips zero-volume minutes), which the stopgap doesn't address.
   **Stage 1 (built, 2026-09-18) — see section 14:** purely additive
   time-aware `ema`/`relative_volume`/`evaluate_hold`/swing-point-window
   functions, proven EXACTLY equivalent to the bar-count versions on real
   uniform-cadence data; no existing call site touched. `macd`'s own
   time-aware version deferred to a later stage (composable directly from
   `ema_time_aware` once needed, not required by stage 1's explicit
   scope). **Stage 2 (built, 2026-09-18) — see section 15:** proved
   genuine improvement on real mixed-cadence data (a real backfill-to-
   live transition and a real internal backfill gap, both from AIFF's
   actual first watched session) — algebraically (a 60s ema step is
   proven identical to six compounded 10s steps) and live (real ema/
   relative_volume/evaluate_hold values, current production vs. time-
   aware, side by side). Found and fixed two real proportional-weighting
   gaps stage 1 hadn't yet closed: `relative_volume_time_aware` now
   compares volume RATES, not raw per-bar volume, once bar widths
   actually vary; `evaluate_hold_time_aware` now uses each bar's actual
   observed width instead of a fixed assumed one. `swing_points_time_
   aware`'s mixed-cadence behavior was explicitly deferred out of this
   stage's scope. **Stage 2 completion (built, 2026-09-18) — see section
   16:** closed that deferral. Found and fixed a real bug (not merely a
   design tradeoff): `window_seconds=30`, calibrated to live 10s cadence,
   is narrower than real 60s backfilled bar spacing, so a candidate's
   real-time bracket could degenerate to just itself and trivially "win"
   as both its own max and min — flagged 228 of 236 real AIFF bars as
   swing points before the fix, 1 after. Also found and quantified two
   real bar-count failure modes on the same real data: a real internal
   gap silently balloons a nominal "3 bars each side" window up to 22
   real minutes, and the real backfill-to-live transition makes the same
   nominal window mean anywhere from 360s down to 60s of real time
   depending on proximity to the boundary — both eliminated by
   construction in the time-aware version. Honest complication for stage
   3: at `window_seconds=30`, the time-aware version is more CORRECT
   (never confirms without genuine real-time bracketing) but finds
   almost nothing on 60s-cadence backfilled data, where `detect_levels`
   currently runs its bar-count window deliberately across the full
   backfilled+live series — a straight parameter swap would not be a
   drop-in improvement there without a larger or cadence-adaptive
   window. Stage 3 (migrating call sites) remains future work, not
   started. **Cadence-adaptive window (built, 2026-09-18, corrected same
   day) — see section 17:** closed that complication, then corrected
   itself. A first fix (`multiple` (default 3.0) times each candidate's
   own single observed width, via `_bar_duration`) passed both required
   proofs and found two edge cases, checked against one real instance
   each and reported as narrow/non-outcome-changing. A full structural
   scan of the ENTIRE real captured history (not just that one AIFF day)
   found both were actually common: 54 real bars with a narrow-window
   blind spot, and 63 of 100 real large-gap bars actually bridging their
   own gap — a majority, not a fluke. `min`/`max` of the two neighboring
   gaps were tested directly against the same real data and either
   changed nothing or traded one problem for a strictly worse version of
   the other (100/100 bridges). Replaced with a real two-directional
   walk (`_walk_real_neighbors`): each side accumulates real elapsed time
   hop by hop, with any single hop over `max_hop_seconds` (default 90.0)
   a hard stop, never crossed. Re-scanned against the same full real
   history: 0 remaining blind spots, 0 of 100 remaining bridges. Both
   required proofs re-run and still hold (AEMD real uniform: 210/210 and
   192/192 identical; AIFF real mixed-cadence: 25/26 real backfilled
   swing points found, a real, substantial improvement over the
   fixed-window version's 0/0, honestly reported as more conservative
   than the superseded design's 44/39 — a figure that was itself
   partly inflated by the very gap-bridging this version closes). A
   THIRD bug (accepting a partial walk that ran out of bars/hit a gap
   before reaching target) was then found by stage 3's own downstream
   regression, not by anything in this section's original tests — see
   below. Phase 3.6's design/proof work is complete for all four
   functions. Touches `core/`'s public function signatures and its
   authoritative test suite — a real redesign, not a quick patch, which
   is why it's a separate phase rather than bundled into the backfill
   work that motivated it.
   **Stage 3, part 1 (built, 2026-09-18) — see section 18:** the first
   real PRODUCTION change in this phase — migrated `detect_levels` and
   `confirmed_swing_lows` (both already see the full backfilled+live
   series, no `live_cadence_tail` entanglement) to the cadence-adaptive
   walk. Public signatures unchanged, zero caller edits needed. This
   migration's own full downstream regression (`setup_types.py`,
   `journal_logic.py`) caught the third bug above directly (a pre-
   existing test built to prove a window is too narrow to confirm
   anything wrongly confirmed one via a partial walk) — fixed at the
   root, not worked around. Real before/after on the same real AIFF
   mixed-cadence day: `detect_levels` 24→15 levels, `confirmed_swing_
   lows` 47→11, the drop being sections 16/17/18's fixes actually taking
   effect (OLD's higher counts were built from swing points already
   proven wrong on this exact data). Uniform-cadence AEMD data unaffected
   (byte-for-byte identical). Deploy HELD: `journal.db` has one open
   position (AEMD) at migration time, so verification ran isolated
   against the real, currently-live `schwab-connector` instead of
   restarting production — confirmed sensible against real live DAIC
   data (top 5 levels by strength identical old vs. new). **Stage 3, part
   2 (built, 2026-09-19) — see section 19:** the higher-stakes half —
   migrated `ema`/`relative_volume`/`evaluate_hold` (all three now
   directly gate real entries or feed MACD/display) to their time-aware
   versions, fed the FULL backfilled+live series, and fully retired
   `live_cadence_tail`/`LIVE_BAR_MAX_GAP_SECONDS` (confirmed removed, not
   left unused). Added `macd_time_aware` (composed from `ema_time_aware`)
   since `macd` structurally needed migrating too to retire the split
   cleanly, though not itself named in this stage's explicit scope.
   Investigated the explicit question this stage was scoped around — can
   backfill alone fire an entry — and confirmed it real on live AIFF data
   (a level confirmed at 13:39 stayed confirmed over an hour into live
   streaming); fixed with `watch_added_ts`, threaded from `_SymbolSlot`
   through `build_state`/`evaluate_setups` to `evaluate_hold_time_aware`,
   requiring the CONFIRMING bar (not the whole streak) to be at or after
   the symbol's own watch time. Downstream regression then found a
   SECOND, distinct real risk unprompted: `round_number_reclaim`'s
   dynamically-recomputed trigger could satisfy a stale, hours-old streak
   right after a sharp reversal, re-firing an entry the instant an
   unrelated position closed — a first fix (reset `confirmed` on any
   reversal) was tried, verified, and REVERTED because it broke the
   genuine "recently confirmed" property 20+ other tests and the real
   entry design depend on. Real before/after on the same real AIFF day:
   MACD sign-flips (bearish→bullish), relative_volume 1.0→0.2516
   (trivial→real reading), a support level's hold confirmation
   False→True — real, consequential differences, not refinements.
   Uniform-cadence AEMD data confirmed unaffected at the full
   `build_state` level. Deploy HELD (same open AEMD position); verified
   isolated against real live DAIC data instead. **Confirmation-freshness
   gate (built, 2026-09-19) — see section 20:** closed the reverted
   fix's gap properly — a staleness check at the point confirmation is
   CONSUMED (`journal_logic._first_newly_confirmed`), not inside
   `evaluate_hold_time_aware`'s state, using a new `confirmed_at_ts`
   field and a live-tunable `CONFIRMATION_FRESHNESS_SECONDS` (default
   30.0, matching `REQUIRED_HOLD_SECONDS`). Investigated with real data
   whether this applies to all four setup types or just round_number_
   reclaim: found `micro_breakout` independently reproduces the exact
   same staleness pattern on the same real AIFF day (ages up to 420s),
   so the gate applies uniformly to all four, not narrowed — a real,
   data-backed scope decision, not assumed either way. Both real risks
   phase 3.6's migration surfaced are now fixed and proven; the 20+
   "recently confirmed, still actionable" tests the first attempt broke
   all pass unchanged. Phase 3.6's migration work is functionally
   complete for all four original functions.
4. **(built)** Virtual trade journal — logs what the system would have
   done (entry, trailing stop) without placing anything, for end-of-day
   review against the user's own judgment. See section 6 for the full
   design — notably, no fixed target: a trailing stop only, by deliberate
   choice, not the "entry/stop/target" originally sketched here.
5. Anything beyond this point (more autonomy, live execution) requires
   its own explicit design discussion and is not assumed by this roadmap.

Do not build ahead of the current phase without an explicit instruction
to move to the next one.
