# momentum_monitor phase 1 — live runbook & definition-of-done checks

This project was built and tested in an environment with **no Schwab
credentials and no access to financial data APIs**. The code path for live
data is exercised only against a fake stream and the replay fixture. The
six definition-of-done checks below need a real Schwab app, a real token,
and (for the chart comparison) a human looking at a chart — so they are
written here as operator steps for you to run locally.

Everything in steps 1–2 happens **once**. Steps 3–6 are the actual checks.

---

## 1–2. Provision credentials — **PENDING, section stale**

> **TODO:** Steps 1–2 below describe the *original* credential plan (a
> dedicated Market-Data-only Schwab app, with `schwab-connector` minting
> and holding its own `token.json` via `client_from_login_flow`). That
> plan is superseded — see `specs.md` §4 ("Revised credential model") —
> `schwab-connector` now fetches short-lived access tokens from the
> joelab `companion-auth` helper over `AUTH_HELPER_URL` and never performs
> OAuth login itself. **`companion-auth` does not exist on this host
> yet — it's being built separately.** Do not follow steps 1–2 as written;
> they need a full rewrite once that service is running and its bootstrap
> flow is confirmed. DoD check 5 below (credential-scope evidence) is
> stale for the same reason and needs the same rewrite, against the
> mitigation model in `specs.md` §4 (dedicated $1 funded account, no
> margin, zero order-placement code paths, Schwab's Order Limit setting)
> rather than the old "Market Data Production only" scope check.

## 1. Register the Schwab app — "Market Data Production" only

1. Go to <https://developer.schwab.com/> → Dashboard → **Create App**.
2. Give it its own name (not shared with any other project in this
   environment — e.g. `momentum-monitor-marketdata`).
3. Under **API Product**, select **Market Data Production** and nothing
   else. Do **not** add **Accounts and Trading Production**.
4. Callback URL: `https://127.0.0.1:8182` (any loopback URL works; it just
   has to match what you pass to schwab-py).
5. Submit and wait for the app status to reach **Ready For Use**.

**DoD check 5 (scope):** open the app in the portal and confirm the API
Products list shows **Market Data Production** and does **not** list
Accounts and Trading Production. Screenshot it. The credential is then
structurally incapable of placing orders or reading positions — not merely
unused for that.

---

## 2. Mint the token (once per 7 days)

schwab-py drives the OAuth login in a browser and writes a token file.

```bash
cd momentum_monitor
python -m venv .venv && ./.venv/bin/pip install schwab-py
./.venv/bin/python - <<'PY'
from schwab.auth import client_from_login_flow
client_from_login_flow(
    api_key="YOUR_APP_KEY",
    app_secret="YOUR_APP_SECRET",
    callback_url="https://127.0.0.1:8182",
    token_path="schwab-connector/data/token.json",
)
PY
```

A browser opens → log in to Schwab → approve → the flow captures the
redirect and writes `schwab-connector/data/token.json`.

- Keep `schwab-connector/data/` untracked (it already is, via
  `.gitignore`). Never commit `token.json`.
- **Token lifetime (platform-enforced):** the Schwab *refresh* token is
  valid for **7 days**. After that a fresh run of the block above is
  required — this is a Schwab policy, not something any client can work
  around. The achievable, testable claim for this tool is *"survives a
  container restart"*, checked in step 6 — **not** "never re-authenticates".

---

## 3. Bring the stack up on live data — DoD check 1

Pick one liquid, currently-active symbol (during regular hours, so bars
actually flow). Then:

```bash
cd momentum_monitor
export SCHWAB_API_KEY=YOUR_APP_KEY
export SCHWAB_APP_SECRET=YOUR_APP_SECRET
export WATCH_SYMBOL=SPY            # or whatever you picked
docker compose up --build         # STREAM_SOURCE defaults to "schwab"
```

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
Watch `http://localhost:8012` fill in and refresh every 5s. Confirm
`bar_count` climbs roughly one per 10 seconds and `last_price` tracks the
tape.

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
| 5 | Portal shows Market Data Production only | screenshot |
| 6 | Restart: no re-auth, bars still present | logs + `bar_count` + bar file |
