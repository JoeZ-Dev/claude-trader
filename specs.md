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
- Market backdrop (broad-market direction that day) — no data pipeline
  exists for this at all yet.
- Human review/labeling — no way to mark a trade as "real signal" vs.
  "worked by chance" after the fact.

**Strategy gaps:**
- ~~Position sizing does not exist.~~ **Built 2026-09-18 — see section
  11.** The journal now tracks a real share count, real dollar P&L, and
  a compounding virtual account balance — not just entry/exit price and
  percentage.
- No portfolio-level risk cap across the 4 concurrent symbol slots.
- ~~`TRAIL_PCT` was one global value despite volatility varying hugely
  across candidates.~~ **Partially addressed 2026-09-18 — see section
  13.** Section 8's live-tunable mechanism was the first step; section
  13 goes further for the early/pattern-forming part of a trade
  specifically (a swing-low-anchored stop, tighter and more structure-
  aware than a flat percentage) — still not per-symbol or volatility-
  adjusted, and the flat percentage still governs once a trade is
  established, so this gap isn't fully closed, just narrowed.
- Breakdown-below variants of the three phase-3.5 setup types remain
  deliberately deferred.

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
This closes that gap, same standard as the rest of phase 3.6, reusing
AIFF's real mixed-cadence session and AEMD's real uniform session from
sections 14–16, no existing call site touched.

**The design.** `swing_points_time_aware`'s `window_seconds` parameter
is replaced with `multiple` (default `3.0`, matching today's convention
of `window=3` bars). A candidate's window is now `multiple *` its own
real observed width, via `_bar_duration` — the SAME helper built for
`relative_volume_time_aware`'s rate fix (section 15), not a second
width-detection primitive. This stays a real elapsed-time bound
throughout (a genuine bar strictly before and after, within the computed
span — section 16's fix, unchanged); only the span itself is now
per-candidate instead of a single constant.

**Equivalence, reconfirmed on real uniform data.** AEMD's real
2026-09-18 regular session (2,340 bars, strictly 10s cadence, reused
from section 14): every candidate's own width is 10s, so `multiple=3.0`
produces exactly `30.0` for every candidate — identical to section 15's
fixed value. Result: `swing_points_time_aware` (adaptive) vs. the
bar-count `_swing_points(window=3)`: 210/210 lows identical, 192/192
highs identical.

**Genuine improvement, on the same real AIFF mixed-cadence day (236
bars) reused from sections 15/16.** The fixed-window version (section
16) found exactly 1 low and 1 high, both in the 8-bar live tail, zero in
the 228-bar backfilled portion. The cadence-adaptive version finds 45
lows and 40 highs total — **44 lows and 39 highs genuinely inside the
backfilled portion**, real numbers, comparable in magnitude to the
bar-count version's 47/39 (which found plenty, just not reliably — see
section 16's asymmetry findings). Example real finds: index 13 (08:03:00,
low=0.9251, own width 60s → window 180s), index 6 (07:56:00, high=1.03,
own width 60s). These are real swing points now confirmable at genuine
60s-cadence resolution, which a 30s live-calibrated window could never
bracket.

**The transition-boundary edge case, demonstrated on real data, not
assumed away.** AIFF's real last backfilled bar (index 228, 14:39:00,
close 1.3091) sits 60s after its own predecessor (index 227, 14:38:00)
but only 10s before the first live bar (index 229, 14:39:10) arrives.
Since `_bar_duration` measures "gap to the NEXT bar," index 228's own
computed width is `10.0`, not `60.0` — giving it a 30s window, narrower
than the 60s gap back to its own real predecessor. Concretely: index 228
can never be bracketed on its "before" side (no real bar within 30s
before it; the nearest is 60s away) and is excluded from consideration
entirely — confirmed directly (`228 not in` either the real high or low
results). This is honest, explained, and narrow: index 228's OWN
close/high/low values (1.3091) sit between index 227 (1.3051) and index
229 (1.32) anyway, so on this real data the exclusion changes nothing —
index 228 was never going to be a genuine extreme regardless. It is
still a real, worth-documenting consequence of scaling strictly off
"time to the next bar": a bar exactly at a cadence speed-up gets treated
as though it were as narrow as what comes after it, not as wide as what
it actually represents. No alternative width definition was built to
avoid this — the instruction was explicit not to add a second
width-detection primitive, and this stage's job was to demonstrate the
consequence honestly, not eliminate it.

**Internal-gap reconfirmation — mostly holds, one honest, non-outcome-
changing exception found.** A candidate with ORDINARY local width (60s,
not itself inflated) correctly cannot reach across a gap: window=180s is
well short of a 360s gap, confirmed both algebraically (a constructed
case mirroring AIFF's real gap magnitude) and on the real AIFF data
itself (index 25, 26, 27, 29 — none bridge the gap). But index 28
(08:29:00, immediately after the real 360s gap) has its OWN next-gap
also unusually wide (120s, since the following bar arrives at 08:31:00),
giving it `window = 3*120 = 360s` — exactly wide enough to reach back
across the 360s gap to index 27 (08:23:00). Verified directly: index 27
IS included in index 28's bracket. This did NOT change index 28's
verdict here (index 28's low, 0.927, is still lower than index 27's low,
0.9445, with or without index 27 in the comparison), but it means the
"never crosses a real gap" property is a strong PRACTICAL consequence of
scaling the window to LOCAL cadence, not an absolute guarantee — in a
region unusually sparse on BOTH sides of a candidate, that candidate's
own inflated width can, in principle, still reach across a neighboring
gap. Reported honestly per this stage's standing instruction, not
glossed over.

**Deliberate break-then-fix**
(`test_swing_points_time_aware_still_finds_real_swing_points_when_
window_actually_brackets`,
`test_swing_points_time_aware_uses_bar_duration_helper_not_a_second_
primitive`): replaced `multiple * _bar_duration(bars, i, reference_
interval_seconds)` with `multiple * reference_interval_seconds`
(reverting to a single fixed span, section 16's version). Result: both
tests failed immediately (`[] == [5]` — the real swing low no longer
found once the window stopped scaling with actual bar width). Reverted;
full suite green again.

**Status:** phase 3.6's design/proof work is now complete for all four
functions. `core/tests/test_core.py`: 36 tests passing (33 from before
this stage plus 3 new — one new synthetic gap-reconfirmation test, one
new transition-boundary test, one new bar-duration-helper-usage test;
the two pre-existing degenerate-bracket tests were updated in place to
the new `multiple` parameter, not replaced); `core` suite overall: 49
tests passing; full project suite: 276 tests passing, unchanged
elsewhere. Stage 3 (migrating call sites, including retiring
`live_cadence_tail`) remains separate, later, explicitly-gated work —
not started.

### 18. Roadmap / phases

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
   started. **Cadence-adaptive window (built, 2026-09-18) — see section
   17:** closed that complication. Replaced the single fixed
   `window_seconds` with `multiple` (default 3.0) times each candidate's
   own observed width (`_bar_duration`, reused, not a second width-
   detection primitive) — a real elapsed-time bound throughout, still
   never a bar count. Reconfirmed exact equivalence on real uniform data
   (AEMD, 210/210 lows and 192/192 highs identical to the bar-count
   version) and proved genuine improvement on the same real AIFF mixed-
   cadence day: 44 real lows and 39 real highs now confirmed WITHIN the
   backfilled portion (vs. zero for the fixed-window version), comparable
   to the bar-count version's 47/39 but without its asymmetries. Found
   and honestly documented, not hidden, two narrow real edge cases on the
   same data: a bar exactly at a cadence speed-up (60s predecessor gap,
   10s successor gap) gets scaled by its narrower successor gap and can
   become unbracketable — confirmed on the real transition bar, which
   didn't change any actual verdict there; and in a doubly-sparse
   stretch, a candidate's own inflated width can, rarely, still bridge a
   neighboring real gap — confirmed on the real AIFF gap, also without
   changing any actual verdict. Phase 3.6's design/proof work is complete
   for all four functions. Stage 3 (actually migrating call sites,
   including retiring `live_cadence_tail`) remains a separate, later,
   explicitly-gated prompt. Touches `core/`'s public function signatures
   and its authoritative test suite — a real redesign, not a quick
   patch, which is why it's a separate phase rather than bundled into
   the backfill work that motivated it.
4. **(built)** Virtual trade journal — logs what the system would have
   done (entry, trailing stop) without placing anything, for end-of-day
   review against the user's own judgment. See section 6 for the full
   design — notably, no fixed target: a trailing stop only, by deliberate
   choice, not the "entry/stop/target" originally sketched here.
5. Anything beyond this point (more autonomy, live execution) requires
   its own explicit design discussion and is not assumed by this roadmap.

Do not build ahead of the current phase without an explicit instruction
to move to the next one.
