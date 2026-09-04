# momentum_monitor

Real-time discretionary trading support tool. Watches one symbol, runs its
bars through the tested analysis core, and shows an objective technical read
(VWAP, EMAs, MACD, relative volume, nearest levels + hold-confirmation) on a
plain web page. It places no orders and fires no signals. See `../specs.md`
section "momentum_monitor/" for the full design; `../AGENT_PROTOCOL.md` for
the working discipline.

## Layout

| Path | What |
|---|---|
| `core/` | Pure analysis logic (indicators, level detection, hold-confirmation) + its unit tests. No I/O. |
| `schwab-connector/` | Holds the Schwab token. Streams one symbol, builds 10s bars, persists them, exposes an internal API (port 7878, not published). |
| `monitor-app/` | Polls schwab-connector, full-recompute through `core/`, serves `/api/state` + a plain HTML page on published port **8012**. |
| `claude-connector/` | Phase-3 placeholder. No service. |
| `fixtures/` | `replay_sample.jsonl` — illustrative AEHL-shaped session for the no-token demo. Regenerate with `python fixtures/make_sample.py`. |
| `docker-compose.yml` | Orchestrates schwab-connector + monitor-app. |

## Run the replay demo (no Schwab token needed)

```bash
cd momentum_monitor
STREAM_SOURCE=replay WATCH_SYMBOL=AEHL docker compose up --build
```

Then open <http://localhost:8012>. The connector replays
`fixtures/replay_sample.jsonl` (paced ~60x) into 10s bars; the page fills in
as bars arrive and refreshes every 5s. `GET http://localhost:8012/api/state`
returns the same data as JSON.

## Run against live Schwab data

Requires a **Market Data Production** Schwab app and a token. See
[`RUNBOOK.md`](RUNBOOK.md) — it also covers the definition-of-done checks
(chart comparison, restart persistence, scope verification).

## Tests

```bash
./run_tests.sh
```

Each service runs as its own pytest invocation (the two service directories
have colliding flat module names and are not importable packages).
