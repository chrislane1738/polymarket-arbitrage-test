# Handoff — polymarket-arbitrage-test

You are taking over a live, long-running cross-venue arbitrage research system between **Polymarket** and **Kalshi**. The user is Chris. Read this whole doc before doing anything except a freshness probe.

**Last update**: 2026-05-28 09:08 by previous Claude session.

## Depth-walking feature (shipped 2026-05-28, deployed gate=0)

A depth-walking fills feature was built and committed (commits `88fb0c4`..`c43a100`, plan at `docs/superpowers/plans/2026-05-27-depth-walking.md`). It replaces top-of-book paper fills with depth-walked VWAP for `FILL_SIZE_CONTRACTS` contracts, so paper PnL reflects real slippage. **Gated by `FILL_DEPTH_ENABLED` (default `0` = OFF).**

- **ACTIVATED gate=1 on 2026-05-28 09:14** — running `FILL_DEPTH_ENABLED=1 FILL_SIZE_CONTRACTS=100`. Verified live: BTC-dip arbs walk 2-3 levels (`levels_consumed`>1), deep books stay at 1, no `partial_fill`, no auto-exits on the flip. To revert: restart without the env var (or `=0`). New DB columns (`fill_vwap_*`, `levels_consumed_*`, `partial_fill` on `arb_candidates`; `entry_fill_vwap_*` etc. on `paper_positions`) now record real walked VWAPs.
- **To activate depth-walking**: restart with `FILL_DEPTH_ENABLED=1 FILL_SIZE_CONTRACTS=100 .venv/bin/python -m scripts.run_feeds > logs/run_feeds.log 2>&1 &`. ⚠️ This re-marks the 8 open positions at depth-walked bid VWAP (worse than top-of-book) — positions near the −$0.10/c MAE stop could auto-exit. Direction B exit path (ids 19/28) has no test coverage yet.
- 30 pytest tests cover the feature: `.venv/bin/python -m pytest tests/ -q`.

## ⚠️ iCloud eviction incident (2026-05-28) — RESOLVED, root cause fixed

The project lives on `~/Desktop` which is **iCloud-synced**. With **"Optimize Mac Storage" ON**, macOS offloaded local files to dataless placeholders under disk pressure (Time Machine + media analysis + the 2.6 GB `feed.db` churn). This zeroed out `analytics/book.py`, `analytics/arb.py`, and the `.git` internals mid-session, and corrupted the `.venv` (truncated `cryptography`, `websockets`, `pip` files), crashing every restart.

- **Fix applied**: turned OFF iCloud → Drive → **"Optimize Mac Storage"** (keeps everything downloaded locally). Files re-materialized, `.git` readable again, venv rebuilt clean from `requirements.txt`.
- **Keep "Optimize Mac Storage" OFF** — this is the durable fix the user chose. If it gets re-enabled, expect recurrence.
- The `.venv` was rebuilt fresh on 2026-05-28; if imports ever fail with "cannot import name X", suspect eviction again and check `wc -l` on source files for 0-byte placeholders.
- Data was never lost (feed.db + positions intact throughout).

---

## 60-second orientation

```bash
cd /Users/chrislane/Desktop/Claude_Code/polymarket-arbitrage-test

# Is the algo alive?
pgrep -f "scripts.run_feeds"                          # should return a PID
.venv/bin/python -c "import sqlite3,time;c=sqlite3.connect('data/feed.db');print('fresh',max(0,(time.time_ns()-c.execute('SELECT MAX(ts_ns) FROM events').fetchone()[0])/1e9),'s')"
# freshness should be < 10s if WS feeds are healthy

# Current PnL
.venv/bin/python -m scripts.paper pnl

# Dashboard
open http://127.0.0.1:8001
```

If `run_feeds` is missing: `.venv/bin/python -m scripts.run_feeds > logs/run_feeds.log 2>&1 &`

---

## What this system is

A research lab for cross-venue arbitrage on binary prediction markets. The thesis:
- The **same real-world event** (Fed rate decision, BTC price target, NBA game) sometimes has a binary market on Polymarket AND Kalshi.
- If you buy YES on the cheaper venue and NO on the more expensive venue, **one side always pays $1** at resolution.
- The "edge" is (1 − cost_yes_cheap − cost_no_expensive) minus fees.
- The strategy never touches real money. **Paper trading only.** User said "no real money" multiple times.

Two venues, two WebSocket feeds, one SQLite database (`data/feed.db`), one paper trader, one arb detector, one resolver, one dashboard.

---

## Current paper-trading state (snapshot at 10:11 2026-05-27)

| Metric | Value |
|--------|-------|
| Realized PnL | **−$21.44** |
| Open MTM | −$2.15 |
| Held-to-expiry on open positions | **+$9.26** |
| Total now (Realized + Open MTM) | **−$23.58** ← best of session |
| Positions: open / closed / total | 9 / 16 / 25 |

**Open positions (9):**
```
id  pair                            dir  mark   MAE    MFE    held  opened
 1  Fed Jun26 - Cut 25bps           polY  −2.04  −2.34  −0.84  +0.56 5/24 17:59
 3  BTC reach $90k in May 2026      polY  −0.24  −1.54  +0.36  +0.66 5/24 17:59
 8  BTC reach $85k in May 2026      polY  +0.49  −2.31  +1.29  +1.89 5/24 18:58
18  NHL 2026 - Colorado             polY  +1.22  −1.48  +3.02  +1.22 5/24 19:57
19  NHL 2026 - Colorado             kalY  +1.02  −0.38  +3.72  +1.12 5/24 20:00
21  BTC reach $200k in 2026         polY  −1.65  −1.65  −1.55  +0.85 5/25 06:42
28  Fed Jun26 - No change           kalY  −1.48  −1.48  −0.18  +1.02 5/26 00:38
30  BTC dip to $70k in May 2026     kalY  +0.33  −2.87  +2.43  +1.03 5/27 06:45
31  BTC dip to $70k in May 2026     polY  +0.22  −1.68  +0.22  +0.92 5/27 08:37
```

`dir polY` = `poly_yes_kalshi_no` (long Poly YES + long Kalshi NO).
`dir kalY` = `kalshi_yes_poly_no` (long Kalshi YES + long Poly NO).

**Pending resolutions** (these will move MTM → Realized when they fire):
- **BTC May markets** (id=3, 8, 30, 31) resolve **5/31** (~4 days). Will free up ~+$4.87 of held-to-exp.
- **UCL Final** (PSG vs Arsenal) 5/30 — no open positions on this pair.
- **Spurs/Thunder Game 5** — partial: Kalshi resolved NO (Thunder won) on 5/26 21:09. Polymarket result still pending. No open positions on this pair either.
- **Fed Jun26 decision** (id=1, 28 and 3 other inactive pairs) resolves **6/17**.
- **BTC Dec 2026** (id=21) resolves **2026-12-31** — 7 months out.

---

## What's been built

### Architecture (everything is in `/Users/chrislane/Desktop/Claude_Code/polymarket-arbitrage-test`)

| Path | What it does |
|------|--------------|
| `feeds/polymarket_ws.py` | Subscribes to Polymarket CLOB WS, parses book + trade events, maintains in-memory book state, writes quotes |
| `feeds/kalshi_ws.py` | Subscribes to Kalshi WS with RSA-signed auth (re-signs per reconnect — **this was a bug, see below**), parses book deltas, writes quotes |
| `feeds/kalshi_rest.py` | Fallback REST poller for Kalshi when no auth (not active when WS works) |
| `analytics/book.py` | `PolymarketBookState`, `KalshiBookState` — applies book/delta events, derives best bid/ask |
| `analytics/arb.py` | Cross-venue arb detector. Runs every 2s. Computes Direction A / Direction B edges, persists to `arb_candidates`, calls paper trader hooks |
| `analytics/paper.py` | The paper trader. **This is the brain.** `PaperTrader.maybe_enter()` and `maybe_exit()` with three exit policies |
| `analytics/resolver.py` | Fetches close_times + outcomes from venue REST APIs, auto-closes paper positions at $1 payout per leg on resolution |
| `match/markets.py` | YAML loader for paired markets |
| `store/sqlite.py` | All persistence. Tables: `events`, `quotes`, `trades`, `arb_candidates`, `paper_positions`, `pair_resolution` |
| `scripts/run_feeds.py` | **The main entrypoint.** Orchestrates all async tasks (WS feeds, arb detector, paper trader, resolver) |
| `scripts/paper.py` | CLI: `list`, `open`, `close <id>`, `pnl` (incl exit-policy review) |
| `scripts/analyze.py` | Persistence analytics over arb_candidates |
| `scripts/monitor.py` | TUI monitor (real-time CLI) |
| `scripts/discover.py` | Find new paired markets across both venues |
| `scripts/simulate_resolution.py` | Manually trigger a resolution (used during testing) |
| `scripts/replay.py` | Replay/tail events from sqlite |
| `server.py` | FastAPI backend for dashboard |
| `static/index.html` | Vanilla-JS dashboard (live quotes, edges, sparklines, positions, feed health, resolutions) |
| `config/markets.yaml` | **21 paired markets** currently configured |
| `data/feed.db` | SQLite — events, quotes, positions, resolutions. WAL mode. |
| `logs/run_feeds.log` | Live tail of the feeds process |

### Strategy in effect

All tunable via env vars. Set in `analytics/paper.py`. Currently (and why each value was chosen):

| Param | Value | Rationale |
|-------|-------|-----------|
| `PAPER_MIN_ENTRY_EDGE` | 0.008 | Started at 0.005 (too loose), tried 0.012 (zero entries in 2h), settled at 0.008 |
| `PAPER_SIZE_CONTRACTS` | 100 | $1/contract × 100 = $100 notional per leg |
| `PAPER_MAX_TTE_DAYS` | 30 | Skip new entries on pairs resolving >30 days out (avoids capital lock-up) |
| `PAPER_AUTO_EXIT_MAE_PER_CONTRACT` | −0.10 | **Was −0.02 — that was the bug that lost $33.** Catastrophe-only stop now |
| `PAPER_AUTO_EXIT_MIN_AGE_S` | 60 | Don't fire MAE rule on entry-spread cost (normal post-entry drift) |
| `PAPER_MFE_EXIT_PER_CONTRACT` | 0.03 | Fixed-tier profit realization — close when mark ≥ 3¢/c |
| `PAPER_MFE_EXIT_MIN_AGE_S` | 30 | Wait 30s after open before MFE-fixed can fire |
| `PAPER_TRAILING_MFE_ACTIVATION` | 0.06 | Once MFE crosses 6¢/c, switch to trailing logic |
| `PAPER_TRAILING_MFE_GIVEBACK` | 0.5 | Close when mark < 50% of MFE peak |
| `PAPER_REENTRY_COOLDOWN_S` | 60 | After exit, no re-entry on same (pair, direction) for 60s |
| `PAPER_BLACKLIST_LOSS_USD` | −5.0 | Pairs with cumulative realized < −$5 |
| `PAPER_BLACKLIST_MIN_CLOSES` | 3 | …AND at least 3 closes |
| `PAPER_BLACKLIST_EDGE_MULT` | 2.0 | …require 2× normal edge (1.6¢) to re-enter |

**Exit policy order** in `maybe_exit()`:
1. MAE catastrophe (`mark/c < −0.10` after age ≥ 60s)
2. Trailing MFE (`MFE > 6¢` AND `mark > 0` AND `mark < 0.5 × MFE`) ← **mark > 0 guard critical**
3. Fixed MFE (`mark ≥ 3¢` AND MFE hasn't crossed 6¢ activation)

### Fee model (in `analytics/arb.py`)
- `POLY_TAKER_FEE_BPS = 50` (0.5% on notional — proxy for spread + USDC + gas)
- `KALSHI_FEE_RATE = 0.07` (Kalshi published formula: `0.07 × p × (1-p)` per contract)

---

## Operating procedures

### Monitor cadence
User has historically asked for hourly checks at the top of the hour (e.g., 11:00, 12:00). Each tick:
1. `pgrep -f scripts.run_feeds` — alive?
2. Freshness check on `events` table
3. Diff since last check (events / arbs / opens / closes)
4. Open-position MAE/MFE evolution
5. Error scan: `tail -200 logs/run_feeds.log | grep -iE 'error|exception|traceback|HTTP 401|gaierror'`
6. Schedule next wakeup at top of next hour

### If process is dead
```bash
.venv/bin/python -m scripts.run_feeds > logs/run_feeds.log 2>&1 &
disown
```

### macOS sleep handling
**The laptop sleeps regularly.** When that happens:
- WS connections stall but TCP state lingers
- On wake, you may see DNS errors (`socket.gaierror`) or `Connection reset by peer` for ~30 seconds
- Old kalshi_ws had a reconnect bug that crashed the loop after wake — **fixed**, but transient errors are normal
- Just wait or restart if freshness >> 60s after wake

---

## Bugs we caught + lessons

### 1. The −$33 bleed: `PAPER_AUTO_EXIT_MAE` was too tight
- Original setting: −$0.02/c → fired on normal book drift (typical entry-spread cost is 1–2¢/c)
- 12 positions auto-exited in a 53-minute window on 5/24, all at losses, total **−$33.04**
- **Fix**: loosened to −$0.10/c (catastrophe-only). Same positions would have been **+$11** at expiry.
- **Lesson**: never set an exit rule below the natural noise floor of the metric

### 2. Trailing MFE pre-fix locked in losses on stale peaks
- Original trailing rule: close when `mark < 0.5 × MFE_peak`
- First firing: NHL Vegas id=20 had MFE +$12.68 from hours before the rule existed → current mark was −$1.12 → rule fired, locking in a loss
- **Fix**: added `mark_per_c > 0` guard. Trailing never closes at a loss; falls back to hold-to-expiry instead.

### 3. Kalshi WS reconnect crash (the biggest bug)
- `async for ws in websockets.connect(URL, headers=headers)` reuses the **original** headers on every reconnect — including the timestamp in the RSA signature
- After ~12h of uptime, the timestamp ages out of Kalshi's freshness window → HTTP 401 → asyncio race → whole feed crashes
- **Fix** in `feeds/kalshi_ws.py`: manual `while True` loop with `async with websockets.connect(...)`, re-signing headers each attempt
- **Caught it after the feed had been dead 51 minutes**. Now holds cleanly.

### 4. Mid-priced markets get crushed by Kalshi fees
- `0.07 × p × (1-p)` peaks at 1.75¢/c when p ≈ 0.5
- So UCL Final, NBA Finals, NHL Stanley Cup (all mid-priced) almost never show arb edges that survive fees
- The wins so far have ALL been in **tail markets** (id=29 BTC $70k dip, id=17/16/20 NHL Vegas where one team was heavy favorite)

---

## MFE-fixed exit rule track record (the key innovation)

Three profitable closes, banking **+$12.72**:

| id | Pair | PnL | Closed |
|----|------|-----|--------|
| 17 | NHL 2026 - Vegas | +$5.87 | 5/25 17:08 |
| 16 | NHL 2026 - Carolina | +$3.20 | 5/25 20:27 |
| 29 | BTC dip to $70k in May 2026 | +$3.65 | 5/27 06:46 |

One pre-fix trailing miss: id=20 NHL Vegas at −$1.12 (5/25 17:47). The `mark > 0` guard prevents this from recurring.

**Net realized improvement since the strategy fix: +$11.60.**

---

## Pending TODOs & known issues

1. **Mid-price-divergence exit signal** (TODO, not implemented). The principled resolution-risk indicator. For an open Direction A position, the failure mode is "Poly resolves NO + Kalshi resolves YES." That's foreshadowed by `kalshi_mid − poly_mid` widening. Better than MAE for catching real resolution risk.

2. **Depth-aware edges**. Currently arbs use top-of-book only. A 1.5¢ edge with 5 contracts of depth isn't really executable at 100c. Real implementation, not paper.

3. **NBA/NHL series tickers have `close_time` in 2028** on Kalshi (series expiration, not game end). Resolver works around this by polling once **either** venue is past close, but the Spurs/Thunder game already showed: Kalshi resolved 21:09 5/26, Polymarket still pending. If Polymarket never publishes, the system never closes — could add a "single-venue-decisive after N days" fallback rule.

4. **Carolina/Montreal NHL pair has 15¢ price divergence** between Poly (0.715) and Kalshi (0.56). Likely different resolution criteria (series vs game) — **NOT** added to YAML for that reason. Same story with Knicks/Cavs.

5. **macOS sleep is a recurring annoyance**. Caused 1 process death (recovered) and 1 false bug-hunt (network was just half-asleep). System recovers but ages of positions can show big jumps.

---

## User preferences & working style

- **Chris likes terse output.** Skip preamble. Headers + tables > paragraphs.
- **Use `Edit` to modify files, never `Write`** to existing files. Never write Markdown docs unless explicitly asked.
- **No emojis** unless he asks.
- **`/loop` for monitoring**: user invokes manually each tick, expects a 3–5 line observation + next wakeup scheduled.
- **Sub-agents**: Chris prefers a manager pattern when work is parallelizable. See `feedback_subagent_manager.md` in his memory.
- **Confirm before destructive ops** (kills, restarts, schema changes). He explicitly said "free reign" for monitoring + tweaks, but anything money-touching needs his go.

---

## Glossary

| Term | Meaning |
|------|---------|
| **Direction A** | `poly_yes_kalshi_no` — long Poly YES + long Kalshi NO |
| **Direction B** | `kalshi_yes_poly_no` — long Kalshi YES + long Poly NO |
| **mark** | Current liquidation P&L: `(poly_bid + kalshi_bid) − entry_cost − fees`. What you'd net closing at current bids. |
| **MAE** | Max Adverse Excursion. Lowest mark seen during position life. |
| **MFE** | Max Favorable Excursion. Highest mark. The new exit rule fires on this. |
| **held_to_expiry_pnl** | What you'll net if held to resolution: `($1 − entry_cost − fees) × size`. The "true" arb payoff for a same-resolving pair. |
| **fixed-MFE** | Exit policy 3 — close immediately when mark/c hits +3¢ |
| **trailing-MFE** | Exit policy 2 — once MFE/c exceeds +6¢, close when mark/c drops below 0.5 × MFE (and mark > 0) |

---

## First things to do as the new Claude

1. Read this whole file.
2. Run the 60-second orientation block.
3. Check the most recent state with `paper pnl` and the open-position query in this doc.
4. If Chris asks for a tick, follow the per-iteration checklist above. Schedule next wakeup at top of next hour.
5. **Don't reinvent**: the system is mature. Resist refactoring impulses. Tune parameters, don't rewrite modules.

The system has been running for ~3 days. It's earning its keep. Keep it alive.
