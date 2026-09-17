# momentum_monitor phase 1 — live runbook & definition-of-done checks

This project was built and tested in an environment with **no Schwab
credentials and no access to financial data APIs**. The code path for live
data is exercised only against a fake stream and the replay fixture. The
six definition-of-done checks below need a real Schwab app, a real token,
and (for the chart comparison) a human looking at a chart — so they are
written here as operator steps for you to run locally.

Everything in steps 1–2 happens **once**. Steps 3–6 are the actual checks.

---

## 1–2. Provision credentials

The credential model and why it's shaped this way (one Schwab app
registration instead of two, the $1-funded/no-margin/zero-order-code
mitigation, the 7-day refresh-token limit, why `schwab-connector` never
holds the refresh token) live in `specs.md` §4 — read that first. This
section is operational steps only; it does not re-derive any of that
reasoning, and if it ever seems to disagree with specs.md, specs.md wins
and this section needs fixing, not the reverse.

`companion-auth` — a separate service at `/srv/apps/companion-auth` on
this host, **not part of this repo** — is the only thing that ever holds
the Schwab refresh token. `schwab-connector` only ever holds a short-lived
access token, fetched from `companion-auth` over `AUTH_HELPER_URL`, and
never performs OAuth login itself. `companion-auth`'s own `README.md` is
the source of truth for its own endpoints and deployment; what follows is
just the parts `momentum_monitor` operators actually need to run.

### One-time OAuth bootstrap

`companion-auth`'s `GET /access_token` returns `409 {"error":
"AUTH_REQUIRED"}` until a refresh token exists. Create one by running its
interactive OAuth flow, on the homelab, inside the already-running
container (its `SCHWAB_CLIENT_ID` / `SCHWAB_CLIENT_SECRET` are already in
that container's environment via its own `.env` + `env_file:` — no need
to pass them again):

```bash
docker exec -it companion-auth python bootstrap.py
```

It prints an authorize URL. Open it, log in to Schwab, approve, then copy
the **entire** redirect URL from the browser's address bar (it starts with
`https://companion-auth.p3l.co/callback?code=...`) and paste it back at
the `Redirect URL>` prompt. The refreshable token is written to
`companion-auth`'s `data/tokens.json` (outside this repo).

**Expect a "state suffix mismatch" message — this is a known Schwab-side
quirk, not an error, and `bootstrap.py` already handles it.** Schwab's own
redirect chain intermittently appends extra trailing characters to the
OAuth `state` parameter it echoes back. `bootstrap.py` detects that the
received state still starts with the expected one, normalizes it, and
proceeds with the exchange automatically — confirmed across multiple live
bootstrap runs, not a one-off. Seeing this message means the flow is
working correctly; don't restart it or treat it as a bad paste.

This bootstrap is required again roughly every 7 days (Schwab caps
refresh tokens at that lifetime — platform-enforced, unaffected by which
app or client is used; see `specs.md` §4). "Survives a container restart"
is the achievable, testable claim (checked in DoD check 6 below); "never
needs re-auth" is not achievable by any client and must not be implied.

### The X-Internal-Auth header

Every caller of `companion-auth`'s `GET /access_token` — `schwab-connector`
included — must send the shared secret as an `X-Internal-Auth` header:

```bash
curl -s http://companion-auth:8766/access_token \
  -H "X-Internal-Auth: $INTERNAL_AUTH_SECRET"
```

This header is the actual security boundary for the endpoint. The
Cloudflare route (`companion-auth.p3l.co`) exists only so Schwab's own
servers can reach the OAuth callback during bootstrap above — it is never
used for service-to-service calls, and is convenience path-scoping on top
of this header, not a substitute for it. A missing or wrong header fails
the request closed (`401`), never silently through. `schwab-connector`'s
own copy of this value is `INTERNAL_AUTH_SECRET` in
`momentum_monitor/.env`, and must match `companion-auth`'s `.env` exactly.

**DoD check 5 (auth boundary, not scope):** confirm the header check
actually rejects bad requests, not just accepts good ones. Verified live
(2026-09-16, from inside a container on `joelab-ingress` — `monitor-app`
itself can't reach `companion-auth` directly, by design, so run this from
`schwab-connector` or similar):

```bash
# no header at all
curl -s -o /dev/null -w "%{http_code}\n" http://companion-auth:8766/access_token
# -> 401 {"error":"UNAUTHORIZED"}

# wrong header value
curl -s -o /dev/null -w "%{http_code}\n" http://companion-auth:8766/access_token \
  -H "X-Internal-Auth: definitely-wrong"
# -> 401 {"error":"UNAUTHORIZED"}

# correct header
curl -s -o /dev/null -w "%{http_code}\n" http://companion-auth:8766/access_token \
  -H "X-Internal-Auth: $INTERNAL_AUTH_SECRET"
# -> 200 once bootstrapped, 409 if not bootstrapped yet
```

Record all three status codes. This replaces the original "Portal shows
Market Data Production only" scope check, which is stale under the
revised credential model (`specs.md` §4): the running credential
deliberately has BOTH Market Data and Accounts and Trading scopes (the
existing ToS_Companion app registration is reused — a dedicated
Market-Data-only app isn't achievable under Schwab's one-app-per-developer
limit), so a portal screenshot of scopes would no longer show what this
tool's safety actually depends on. What it depends on instead is (a) this
header check failing closed, and (b) `momentum_monitor` containing zero
order-placement/account-endpoint code paths — see `specs.md` §4 and
`AGENT_PROTOCOL.md`'s Credentials section.

---

## 3. Bring the stack up on live data — DoD check 1

Pick one liquid, currently-active symbol (during regular hours, so bars
actually flow). `SPY` (used below) is a reasonable default **for this
check specifically** — a neutral, always-liquid smoke test for "does the
pipeline work at all," not a stand-in for how this tool is actually used
day to day. The real use case is thinly-traded momentum candidates (see
whatever's in `schwab-connector/data/bars/` from actual sessions), which
aren't guaranteed to be trading at all whenever you happen to run this
checklist — that's exactly why they're the wrong choice for a repeatable
smoke test.

Credentials and the watched symbol live in `momentum_monitor/.env`
(gitignored — see section 1–2 above for `AUTH_HELPER_URL` /
`INTERNAL_AUTH_SECRET`). Set `WATCH_SYMBOL` there, then:

```bash
cd momentum_monitor
docker compose up --build         # STREAM_SOURCE defaults to "schwab"
```

`SCHWAB_API_KEY` / `SCHWAB_APP_SECRET` are still read by
`schwab-connector` and passed to schwab-py's `client_from_access_functions`,
but are not functionally required under the current (companion-auth)
credential model — verified live: the actual running container has
`SCHWAB_API_KEY` unset entirely and streams real data correctly regardless.
schwab-py's ordinary REST/streaming calls authenticate with the bearer
access token alone, the same reasoning `.env`'s own comment already gives
for `SCHWAB_APP_SECRET` being an unused placeholder. Leave both unset
unless something about this changes.

**DoD check 1 passes if:** both `schwab-connector` and `monitor-app`
reach "Application startup complete" with no errors, and
`curl -s localhost:7878`… is not reachable from the host (it must be
internal only) while `http://localhost:8012` loads.

Quick internal health check from inside the network:

```bash
docker compose exec monitor-app python -c \
  "import urllib.request,json; print(json.load(urllib.request.urlopen('http://schwab-connector:7878/health')))"
# -> {'status': 'ok', 'watching': ['SPY'], 'connected': True}
```

---

## 4. Let it run — DoD check 2

Leave it up for **several real minutes** during regular trading hours.
`http://localhost:8012` updates in place via JS polling `GET /api/state`
every 4 seconds and patching specific elements directly (price, VWAP,
EMAs, MACD, levels, the journal section) — not a full-page reload. A
full-page `<meta http-equiv="refresh">` every 5s was the original,
unintended behavior here (a bug, not the design — it caused visible
flicker) and has since been replaced with this in-place update mechanism.

A "Pause updates" button in the top bar (next to the ticker box) can stop
polling entirely — both this client-side display loop and, via
`POST /api/polling`, `monitor-app`'s own server-side poll loop against
`schwab-connector` (the one that actually costs anything; the client-side
loop alone never left localhost/the LAN). Confirm it reads **live**, not
**paused**, before treating a flat readout as meaningful for this check.

Confirm `bar_count` climbs roughly one per 10 seconds and `last_price`
tracks the tape.

---

## 5. Record the numbers — DoD check 3

At one moment, capture the actual readout — don't describe it, paste it:

```bash
curl -s localhost:8012/api/state | python -m json.tool
```

Note the wall-clock time you captured it.

---

## 6. Compare one value to a real chart — DoD check 4

At (or very close to) the same moment you captured step 5, open a chart for
the same symbol on the same session (TradingView, thinkorswim, the Schwab
site) and read off:

- **Session VWAP** — this tool anchors VWAP at the **first bar of the
  day, premarket included**. If your chart's VWAP is set to regular-session
  anchor, expect a small difference, largest early in the day and
  shrinking as RTH volume dominates. For an apples-to-apples check, set the
  chart's VWAP anchor to the pre-market open, or compare **EMA(9)** on a
  ~10s/tick chart instead (no anchoring ambiguity).

Write down: chart value, tool value, and **whether they matched** (to
within rounding / the anchor caveat). This is the check — state the result
explicitly either way.

---

## 7. Restart and re-check persistence — DoD check 6

While still within the 7-day token window:

```bash
docker compose restart
```

Then confirm, explicitly:

1. **No re-authentication.** `schwab-connector` logs show it came back up
   and resumed streaming without opening a browser or erroring on the
   token. (`docker compose logs schwab-connector | grep -i token` — no
   "login flow" / "refresh failed" lines.)
2. **Bars survived.** `curl -s localhost:8012/api/state` shows a
   `bar_count` at least as high as before the restart, and the earliest
   bars are still retrievable:
   ```bash
   docker compose exec monitor-app python -c \
     "import urllib.request,json; d=json.load(urllib.request.urlopen('http://schwab-connector:7878/bars/SPY')); print(len(d),'bars, first ts', d[0]['ts'])"
   ```
   The bar file lives in the mounted volume at
   `schwab-connector/data/bars/SPY.jsonl` and is not touched by the
   restart. (The store also drops non-monotonic bars, so the restart does
   not duplicate history.)

---

## Done criteria

Report phase 1 complete only when all six are true and evidenced:

| # | Check | Evidence to record |
|---|---|---|
| 1 | `docker compose up` brings up both services cleanly | logs, `/health` |
| 2 | One real symbol, several real minutes | `bar_count` over time |
| 3 | Actual `/api/state` numbers pasted | the JSON |
| 4 | One value (VWAP or EMA) vs a real chart, match stated | both numbers + verdict |
| 5 | companion-auth's `/access_token` rejects missing/wrong `X-Internal-Auth` (401), accepts the correct one | curl status codes for no-header / wrong-header / correct-header |
| 6 | Restart: no re-auth, bars still present | logs + `bar_count` + bar file |
