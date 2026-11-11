# groww-orb-bot

An intraday Sensex/Nifty options bot for the **Groww Trade API**. It runs **4 strategies in parallel**
in paper mode on the same live prices and writes a **daily HTML report** for each. A **leaderboard**
compares them so you can see which one actually works before you put money on it.

> **Paper mode is the default.** No real orders are placed unless you choose *Live trade* and type
> `I UNDERSTAND` (or set `ORB_MODE=live` **and** `ORB_CONFIRM_LIVE=YES`). Live mode trades exactly one strategy.

## Quick start: just run it

```bash
python bot.py
```

The bot asks you everything, step by step:

1. **What to do:** Paper trade · Live trade · Simulate · Backtest · Train ML · Rebuild reports · Cron setup
2. **Index:** SENSEX (BSE) or NIFTY (NSE). Exchange, strike step and lot size are filled in for you
3. **Groww login:** `GROWW_API_KEY`, then TOTP secret or API secret, typed **hidden**. Offers to test the login
4. **Money & risk:** capital, risk %, daily loss limit, max trades, monthly target (validated)
5. **Strategies:** pick any of the 4 (e.g. `1,3` or `all`)
6. **Headlines (optional):** paste this morning's news for sentiment scoring
7. **Summary → Start now?** → offers to **save your answers to `.env`**, so next time you just press Enter.
   Secrets are saved only if you say yes; the file is readable only by you

If you start it after 10:00 IST or on a holiday, it offers to **wait and run at the next session's 09:05**.
Press `Ctrl+C` at any prompt to quit.

Every option is also available as a command-line flag for automation (section 5). The bot reads
`.env` by itself, so no `source` step is needed.

---

## Paper vs live: the only difference

| | **Paper** (default) | **Live** |
|---|---|---|
| Prices | Real Groww prices | Real Groww prices |
| Orders | **Simulated** at real bid/ask; nothing sent to Groww | **Real** limit orders + SL-M protective stop at Groww |
| Strategies | All at once (compare them) | Exactly one |
| How to start | `python bot.py` → **1) Paper trade** | `python bot.py` → **2) Live trade** → type `I UNDERSTAND` |
| Without the wizard | `python bot.py --strategies orb,momentum,supertrend,ml,random` | `ORB_MODE=live ORB_CONFIRM_LIVE=YES python bot.py --strategies supertrend` |
| Reports | `reports/paper/` | `reports/live/` |

## Commodities (MCX)

MCX options trade until 23:30 IST, so you can paper test in the evening:

```bash
python bot.py
#  1) Paper trade
#  Market  -> CRUDEOILM (crude-mini, lot 10)   <- best fit for ~₹1L capital
#  Session -> 1) Start NOW   (or "MCX evening session 18:00")
#  enter Groww key + TOTP, accept the defaults, Start now? -> y
```

- **Underlying price:** the nearest futures contract (e.g. `CRUDEOILM19NOV26FUT`), found automatically in Groww's instrument list. Same for the ATM CE/PE of the nearest expiry, lot size, and tick size.
- **No India VIX** (it's an equity index), so the trailing buffer is not widened. Charges use commodity CTT (0.05% on sell).
- **Capital fit:** full CRUDEOIL (lot 100) and NATURALGAS (lot 1250) usually exceed a ₹1,000 risk budget, so the bot logs "skipped: stop too wide". Use the mini contracts (**CRUDEOILM**, **NATGASMINI**, **GOLDM**, **SILVERM**).
- **"Start now" windows** are for testing the pipeline. The ORB strategy is designed for a real session open (09:15 for equities; for MCX try 18:00 when US data and liquidity arrive).
- **The ML model is per market.** One trained on SENSEX is refused for CRUDEOILM, so retrain with history for that market.

## 1. Files

| File | What it is | Do you edit it? |
|---|---|---|
| `bot.py` | Everything that trades: config, risk, Groww connection, 4 strategies, engine, backtest, ML training, commands | **Yes, the `Config` class** (section 3) |
| `report.py` | Daily report, month dashboard, leaderboard | No |
| `test_bot.py` | 33 automated tests | No |
| `data/headlines.txt` | Optional morning news headlines (one per line) | Optional, daily |
| `data/holidays.txt` | Exchange holidays, `YYYY-MM-DD` per line | **Yes, once a year** |
| `requirements.txt` | Python packages | No |

The bot creates these as it runs: `reports/` (all reports), `logs/` (one log per day),
`data/cache/` (downloaded history) and `data/ml_model.json` (the trained ML model).

---

## 2. One-time setup

### 2.1 Install

You need Python **3.10 or newer** on Linux, macOS, WSL, or a cloud VM.

```bash
cd groww-orb-bot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest -q test_bot.py        # expect: 33 passed
```

### 2.2 What you need from Groww

| Item | Where to get it | Needed for |
|---|---|---|
| **API key** | Groww web → Profile → Trade API / Groww Cloud → API Keys | everything except `--simulate` |
| **API secret** *or* **TOTP secret** | Same page. The key+secret login must be approved again **every day**. TOTP never expires, so it's better for automation | everything except `--simulate` |
| F&O segment enabled, plus margin | Groww account settings | paper (data) and live |
| Static IP registered with Groww | Required for API order placement under SEBI's retail-algo rules. Check Groww's current requirement | **live only** |

### 2.3 Settings file `.env` (the wizard writes this for you)

Running `python bot.py` and answering the prompts creates `.env` automatically. You can also write
or edit it by hand. Git ignores it, and it is never put into reports.

```bash
# --- required ---
GROWW_API_KEY=your_api_key
GROWW_TOTP_SECRET=your_totp_secret      # recommended
# GROWW_API_SECRET=your_api_secret      # alternative (needs daily approval)

# --- market (defaults shown) ---
ORB_EXCHANGE=BSE
ORB_UNDERLYING=SENSEX                   # NIFTY with ORB_EXCHANGE=NSE
ORB_SPOT_SYMBOL=SENSEX                  # index trading symbol for spot price
ORB_STRIKE_STEP=100                     # SENSEX 100, NIFTY 50
ORB_LOT_SIZE=20                         # fallback only; bot reads the live lot size
ORB_VIX_SYMBOL=INDIAVIX

# --- run options ---
ORB_MODE=paper                          # paper | live
ORB_STRATEGIES=orb,momentum,supertrend,ml,random
ORB_SKIP_EXPIRY=0                       # 1 = don't trade on expiry day
# TELEGRAM_BOT_TOKEN=...               # optional alerts (wizard can set these)
# TELEGRAM_CHAT_ID=...
ORB_AUTO_ATM=1                          # pick ATM strike of nearest expiry each morning
ORB_USE_FINBERT=0                       # 1 = score data/headlines.txt with FinBERT (big download)
```

The bot loads `.env` automatically on every run. Real environment variables, if set, take priority.

---

## 3. Configuration: which file and what to change

There are **two places**:

1. **`.env`** (above, or just answer the wizard): keys, market, capital, risk %, daily loss, max trades, target, strategies.
2. **`bot.py` → `class Config`** (near the top, section `# CONFIG`): money and risk rules.

| Setting in `Config` | Default | Meaning |
|---|---|---|
| `total_capital` (`ORB_CAPITAL`) | `100_000.0` | Capital used for position sizing |
| `base_risk_pct` (`ORB_RISK_PCT`) | `1.0` | % of capital risked per trade (₹1,000) |
| `max_risk_pct` | `1.5` | Hard ceiling, even after sentiment or slippage |
| `daily_loss_limit` (`ORB_DAILY_LOSS`) | `2_000.0` | Stop trading for the day after this net loss |
| `max_trades_per_day` (`ORB_MAX_TRADES`) | `2` | Per strategy |
| `max_capital_deploy_pct` | `60.0` | Max % of capital spent on option premium |
| `monthly_target` (`ORB_MONTHLY_TARGET`) | `50_000.0` | Used only for the report's progress bar |
| `opening_range_end` / `entry_cutoff` / `square_off` | 09:30 / 09:45 / 10:00 | Session timing (IST) |
| `max_initial_sl_pct` | `20.0` | ORB stop cap (% of premium) |
| `signal_sl_pct` | `10.0` | Stop for momentum/supertrend/ml (% of premium) |
| `base_trail_pct` | `12.0` | Trailing-stop distance (% of entry), widened when VIX > 18 |
| `ema_fast`, `ema_slow` | 5, 13 | momentum strategy |
| `st_atr`, `st_mult` | 7, 2.5 | supertrend strategy |
| `ml_up`, `ml_down` | 0.58, 0.42 | ml strategy confidence thresholds |
| `brokerage_per_order`, `stt_sell_pct`, … | Groww rates | Charges estimate. Check against your contract note |
| `spread_pct`, `open_spread_mult` | 0.4%, ×2 | Backtest/sim bid-ask spread model (paper uses real quotes) |
| `broker_stop` | `True` | Place and trail an SL-M order at Groww in live mode |
| `stale_sec` | `30` | Block entries if prices freeze this long |

> **Important with ₹1L capital:** at 1% risk (₹1,000), one Sensex lot (20 units) can only afford a
> stop about ₹50 below entry. On wide-range days the bot logs **"skipped: stop too wide for 1 lot"**.
> That is the risk rule working, not a bug. Your options: raise `base_risk_pct` to 1.5, lower
> `max_initial_sl_pct` / `signal_sl_pct`, or use more capital. Decide using the backtest (section 5.2).

---

## 4. The 4 strategies

Every strategy shares the **same** sizing, stops, trailing, −₹2,000 daily limit, 10:00 square-off
and charges. **Only the entry signal differs**, so the comparison measures prediction quality alone.

| Name | Entry signal | Window |
|---|---|---|
| `orb` | The option premium breaks its 09:15–09:30 high by 0.5%, **the index confirms**, the break **holds 20s**, and the range is 3–30% wide | 09:30–09:45 |
| `momentum` | EMA(5) crosses EMA(13) on 1-min **index** bars, and price is on the same side of the session average | 09:20–09:45 |
| `supertrend` | Supertrend (ATR 7 × 2.5) flips direction on 1-min index bars | 09:20–09:45 |
| `ml` | Logistic regression predicts the index 5 minutes ahead. Buys CE if P(up) ≥ 0.58, PE if ≤ 0.42. **Must be trained first** (section 5.3) | 09:20–09:45 |
| `random` | **Control:** random CE/PE entries with the same exits. A strategy that can't beat this has no real signal | 09:20–09:45 |
| `orb_baseline` | (Optional control) Blueprint ORB with safety fixes, no filters | 09:30–09:45 |

Shared exits: initial stop → breakeven at +1R → trailing stop (VIX-widened) → **time stop** after
12 min if below +0.3R → **hard exit 10:00**.

---

## 5. Commands (non-interactive, for automation)

`python bot.py` with no options starts the interactive wizard. Every flag below skips the wizard.
Run from the project folder; `.env` is loaded automatically.

### 5.1 Try it offline (no keys needed)

```bash
python bot.py --simulate --days 22                  # 22 synthetic days, all 4 strategies
# open reports/sim/compare.html
```

### 5.2 Backtest on real Groww history

```bash
# Downloads 1-min candles (cached in data/cache/), then replays every strategy on the same days.
python bot.py --backtest --from 2026-06-01 --to 2026-09-30
python bot.py --backtest --from 2026-06-01 --to 2026-09-30 --strategies orb_baseline,orb   # A/B one filter set
# open reports/backtest/compare.html
python bot.py --backtest --synthetic --days 44      # offline pipeline check
```

### 5.3 Train the ML model (needs history from 5.2 first)

```bash
# Train only on days up to --to, so the later days stay unseen for an honest test.
python bot.py --train-ml --to 2026-08-31
python bot.py --backtest --from 2026-09-01 --to 2026-09-30 --strategies ml   # test on unseen days
```

The training output shows test accuracy against a "guess the majority" baseline, plus a **VERDICT**.
If it says *NO reliable edge*, don't trust the `ml` strategy. A model trained with `--synthetic` is
**refused** in paper and live mode automatically.

### 5.4 Paper trading: all 4 strategies in parallel (the main thing to run)

```bash
python bot.py                                        # interactive: choose "1) Paper trade"
python bot.py --strategies orb,momentum,supertrend,ml,random   # same, no questions (uses .env)
python bot.py --strategies orb,supertrend            # a subset
```

- All strategies share **one** price fetch per second, about 120 requests/minute. Groww's limit is 300/minute.
- Fills are simulated at the live price plus 0.15% slippage. No orders reach Groww.
- Start before **09:10 IST**. If started after 10:00, it records "skipped" and exits.

### 5.5 Live trading (only after weeks of paper results)

```bash
python bot.py                                        # interactive: choose "2) Live trade", type I UNDERSTAND
ORB_MODE=live ORB_CONFIRM_LIVE=YES python bot.py --strategies supertrend   # non-interactive, exactly ONE strategy
```

### 5.6 Run automatically every trading day (cron, Linux/macOS)

Cron can't answer questions, so first run the wizard once and say **yes** to saving settings *and*
keys to `.env`. The wizard's option *7) Show how to run automatically* prints the exact line. Then run
`crontab -e` and add:

```cron
CRON_TZ=Asia/Kolkata
5 9 * * 1-5  cd /path/to/groww-orb-bot && .venv/bin/python bot.py --strategies orb,momentum,supertrend,ml,random >> logs/cron.log 2>&1
```

It skips weekends and dates in `data/holidays.txt`. On a cloud VM, keep the machine's clock synced (NTP).

### 5.7 Other commands

```bash
touch STOP                    # EMERGENCY: every strategy closes its position and exits on the next tick
rm STOP                       # remove it before the next session, or the bot exits immediately
python bot.py --rebuild-reports   # regenerate all HTML from the saved journals
python bot.py --wizard        # force the interactive wizard
python bot.py --help          # every option + strategy list
tail -f logs/$(date +%F).log  # watch today's session live
python -m http.server -d reports 8000   # view reports from another device: http://<ip>:8000
```

---

## 6. Daily inputs (optional)

| Input | How | Effect |
|---|---|---|
| `data/headlines.txt` | Before 09:10, write that morning's market headlines, one per line (by hand or from your news fetcher). Set `ORB_USE_FINBERT=1` and `pip install torch transformers` | Sentiment scales risk 0.2–1.5%. A score below −0.5 blocks trading for the day |
| `data/holidays.txt` | Once a year, paste the NSE/BSE holiday list | Bot skips those days |

With no headlines, sentiment is neutral (0.0) and risk stays at the base 1%.

---

## 7. Reports (created after every session)

```
reports/
├── paper/                       # one folder per mode: paper / live / sim / backtest
│   ├── compare.html             # ★ LEADERBOARD: all strategies, cumulative P&L, metrics, daily table
│   ├── orb/
│   │   ├── 2026-11-12.html      # daily report
│   │   ├── month-2026-11.html   # month dashboard (index.html = latest month)
│   │   ├── journal.jsonl        # raw data (source of truth)
│   │   └── journal.csv          # one row per trade (Excel / tax)
│   ├── momentum/ …  supertrend/ …  ml/ …
```

- **Daily report:** net P&L after charges, gross, charges, R-multiple, sentiment, VIX, month-to-date progress, interactive price chart (premium, trailing stop, buy/exit markers, opening-range band), trade table, risk checks, charges breakdown, headlines, event log. Has a dark mode and works offline.
- **Leaderboard:** cumulative P&L per strategy, win rate, profit factor, expectancy (avg R), max drawdown, charges, how sessions ended, best strategy each day.

---

## 8. How to choose a strategy (read this before trusting any number)

**The backtest decides; paper trading only confirms execution.** Per-trade results swing by about ±1R,
so a small sample can't separate skill from luck:

| Trades | 95% uncertainty of avg R | Can +0.15R be told apart from zero? |
|---|---|---|
| 40 (≈ 2 months of paper) | ±0.31R | No |
| 170 | ±0.15R | Barely |
| 500+ | ±0.09R | Yes |

So: **(1)** backtest 1–3 years on real Groww history (section 5.2). **(2)** Paper trade 2–4 weeks to check
that live fills, spreads and the stop orders behave as the backtest assumed. **(3)** Go live with one strategy only.

Open `compare.html` and use the **"Is the edge real?"** table:

1. **Edge vs zero** must say *edge > 0 (95%)*, meaning the whole confidence interval is above zero.
2. **vs random entries** must say *beats random*. If random entries with the same exits do as well, the
   signal adds nothing, and the profit came from the exits or luck.
3. **Trades** must reach the **Trades needed** figure shown in the same row.
4. **Where it works:** check whether the edge only appears in one condition (for example VIX > 18 or expiry
   day). If so, trade only that condition, or treat the result with suspicion.
5. **Exit quality:** a high *"…then closed < +0.5R"* means trades reach +1R and give it back. Tune
   `breakeven_at_r` / `base_trail_pct` and re-run the backtest.
6. **Don't over-tune.** When you change settings to improve a backtest, re-check on a different date range.
   Settings that only work in one period are overfitted.

> **Reality check.** ₹50,000/month on ₹1L is a 50% monthly return. Historically, SEBI has found
> most individual F&O traders lose money. Treat the target as an upper bound, and let the leaderboard
> decide what the bot actually earns.

---

## 9. Safety features

- Paper by default; live needs two explicit settings and is restricted to one strategy
- **Stop-loss order at Groww (live):** an SL-M order is placed right after entry and moved up as the stop
  trails, so the exchange exits the position even if the bot, network or machine dies. If Groww rejects the
  stop, the bot exits immediately. On exit it cancels the stop *first* and never sells twice (tested)
- **Position check (live):** at start it skips any contract where Groww already shows a position; at 10:00 it
  confirms you are flat and warns loudly if not
- **Realistic fills:** paper fills at Groww's real bid/ask; the backtest models a spread that is wider at the open
- **Stale-feed guard:** no new entries if prices haven't changed for 30s
- **Expiry awareness:** days to expiry are logged and reported; `ORB_SKIP_EXPIRY=1` skips expiry day
- **Telegram alerts (optional):** entries, exits, risk events, end-of-day summary
- Risk per trade capped at 1.5% of capital, even after slippage (the stop is tightened if needed)
- Daily loss limit, sentiment and VIX circuit breakers, time stop, hard 10:00 exit
- Limit orders with retries; exits fall back to a market order so they always complete
- After 10 consecutive data errors the position is closed; on a crash the bot attempts to close it
- `touch STOP` kill switch
- Secrets are read only from environment variables and never written to logs or reports

---

## 10. Troubleshooting

| Message | Fix |
|---|---|
| `Set GROWW_API_KEY plus GROWW_API_SECRET (or GROWW_TOTP_SECRET)` | Run `python bot.py` and enter them, or add them to `.env` |
| `Live mode requires ORB_CONFIRM_LIVE=YES` | Intentional safety check |
| `skipped: started after session window` | Start before 10:00 IST (ideally 09:05) |
| `skipped: stop too wide for 1 lot` | See the note in section 3 |
| `Spot data unavailable - spot confirmation disabled` | Check `ORB_SPOT_SYMBOL` / `ORB_EXCHANGE` |
| VIX always 15.00 in reports | That's the fallback value. Fix `ORB_VIX_SYMBOL` |
| `Auto-ATM failed … using configured symbols` | Check `ORB_UNDERLYING` / `ORB_STRIKE_STEP`, or set `ORB_AUTO_ATM=0` with `ORB_CE_SYMBOL` / `ORB_PE_SYMBOL` |
| `ML strategy idle: no model` | Run section 5.3 |
| `SDK has no CANDLE_INTERVAL_MIN_1` | Update the SDK: `pip install -U growwapi` |
| Bot exits immediately | A leftover `STOP` file exists. Run `rm STOP` |

---

## 11. Checklist before going live

- [ ] Real backtest (1+ year): edge > 0 **and** beats random, with enough trades (section 8)
- [ ] Same result on a second, unseen date range
- [ ] 2–4 weeks of paper trading where fills, spreads and P&L match the backtest
- [ ] On the first live day, check in the Groww app that the SL-M stop order appears after each entry
- [ ] First paper log shows a real VIX value (not 15.00) and the correct ATM strike and lot size
- [ ] Charges in reports match a real Groww contract note (within a few rupees)
- [ ] Static IP and any algo registration confirmed with Groww
- [ ] `holidays.txt` updated for the year
- [ ] You can stop it (`touch STOP`) and you know how to close a position manually in the Groww app
