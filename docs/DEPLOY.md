# Server deployment (Hetzner VPS, shared with contrafact)

The paper-trading pipeline and scheduled recordings run on the same Hetzner
CX22 (Ubuntu 24.04) that serves contrafact.quest. Rationale: the Mac kept
killing long runs (sleep), and the Phase 6 gate — weeks of continuous paper
trading — needs an always-on box. The pipeline is tiny (~60 MB RSS, µs of CPU
per event, ~8 MB DuckDB per day) and does not disturb the website.

**Latency note:** Hetzner (DE) → Alpaca (US-East) adds ~60–70 ms of feed
latency vs the UK Mac. Irrelevant at 5–10 s horizons (~1% of horizon); would
matter only if the project ever chased sub-second horizons (then: US-East VPS).

## Layout (deploy user)

| path | what |
|---|---|
| `~/intraday-ml-signals` | repo clone (read-only deploy key `~/.ssh/gh_intraday_deploy`) |
| `~/intraday-ml-signals/.env` | Alpaca **paper** keys only — never live keys on shared infra |
| `~/intraday-ml-signals/data/` | DuckDB session/live stores (pull to Mac for notebook analysis) |
| `~/.config/systemd/user/intraday-pipeline.service` | 24/7 crypto paper pipeline |
| `crontab -l` | weekday equities recording + nightly report (shared with contrafact crons — merge, never replace) |

## Setup / redeploy

First time: run `deploy/setup_server.sh` on the server as `deploy`, register
the printed public key on the GitHub repo (Settings → Deploy keys, read-only),
re-run the script, fill `.env`, then:

```
systemctl --user enable --now intraday-pipeline
```

Redeploy after pushing to main:

```
ssh -i ~/.ssh/contrafact_vps deploy@<SERVER_IP> \
  'cd ~/intraday-ml-signals && git fetch origin main && git reset --hard origin/main \
   && .venv/bin/pytest -q && systemctl --user restart intraday-pipeline'
```

Health checks:

```
systemctl --user status intraday-pipeline      # running?
journalctl --user -u intraday-pipeline -n 20   # status lines (events, dir, pnl, breaker)
tail logs/equities_cron.log                    # last equities session
```

## Constraints & gotchas

- **One WS connection per Alpaca feed.** The server owns the streams now; do
  not run soaks/recordings from the Mac while the service is up (crypto), or
  during 14:30–21:00 UTC weekdays (stocks cron).
- **Cron times are UTC.** US market open = 14:30 UTC in EDT (Mar–Nov) but
  15:30 UTC in EST (Nov–Mar): adjust the equities cron at the November change.
- The daily-loss circuit breaker resets on UTC day rollover (pipeline handles
  this internally).
- DuckDB single-writer: don't open `data/paper_live.duckdb` read-write while
  the service runs; `read_only=True` connections are fine after a checkpoint,
  or pull a copy to the Mac.

## Mobile monitoring dashboards

**Browser (basic-auth):** `https://contrafact.quest/stats/trading.html` — same
creds as /stats. Auto-refreshes every 60s; light/dark follows the phone.

**Telegram Web App (2026-07-20):** each bot message carries a "Live dashboard"
button that opens a full interactive page *inside* Telegram's own webview (one
tap, native-feeling, matches Telegram's theme) — the actual ceiling of what the
platform allows for "a chart in Telegram" (there is no surface for a live
widget to sit directly in the message feed). Served at a SEPARATE, no-basic-
auth route — `https://contrafact.quest/app/<token>/{trading,stocks_app}.html` —
since a Web App opened from inside Telegram's embedded webview can't cleanly
satisfy an HTTP auth prompt; the token (an unguessable path, not a login) lives
in `~/digest.env` as `DASHBOARD_BASE_URL` and in the server's Caddyfile, not in
this repo. `trading.html` = crypto (gen_dashboard.py); `stocks_app.html` =
equities blotter/position/research (gen_stocks_app.py).

Data flow: the pipeline service writes `data/status.json` (crypto) /
`data/status_stocks.json` (equities) atomically every status tick (never
expose the single-writer DuckDB to other processes); a per-minute cron renders
each generator and copies the HTML into BOTH the `/stats` (basic-auth) and
`/app/<token>` (Web App) paths inside the `chord-gen_stats_data` docker volume.
Freshness banner: LIVE (<90s), STALE (<5m), OFFLINE — OFFLINE means the service
stopped writing status, which is itself the alert.

Future control plane (deliberately not built yet): a page gains inputs that
write `control.json` (position caps, kill switch), which the pipeline reads
each status tick. Real-money control requires stronger auth than an unguessable
URL (at minimum: Telegram `initData` verification, a confirmation step, and
signed requests) — revisit only if paper trading proves an edge.
