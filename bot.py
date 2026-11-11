"""Groww morning Opening-Range-Breakout bot - single-file trading core.

  python bot.py                                   # today's session (paper unless ORB_MODE=live)
  python bot.py --simulate --days 22              # offline synthetic month -> reports/sim/
  python bot.py --backtest --synthetic --days 66  # A/B pipeline check (proves nothing)
  python bot.py --backtest --from 2026-08-03 --to 2026-10-30   # A/B on real Groww 1-min candles
  python bot.py --rebuild-reports

Sections: CONFIG · CLOCK · RISK · SENTIMENT · INSTRUMENTS · BROKERS · ENGINE · BACKTEST · CLI
Paper mode is the default. Live needs ORB_MODE=live AND ORB_CONFIRM_LIVE=YES.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import sys
import time as _time
import traceback
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import report


# ==========================================================================
# CONFIG
# ==========================================================================
IST = ZoneInfo("Asia/Kolkata")
SECRET_FIELDS = ("api_key", "api_secret", "totp_secret", "telegram_token")  # never written to reports/logs


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Config:
    # --- Mode -------------------------------------------------------------
    # "paper" = real market data, simulated fills (DEFAULT, safe).
    # "live"  = real orders. Must ALSO set ORB_CONFIRM_LIVE=YES.
    mode: str = field(default_factory=lambda: _env("ORB_MODE", "paper"))

    # --- Credentials (env only) ------------------------------------------
    api_key: str = field(default_factory=lambda: _env("GROWW_API_KEY", ""))
    api_secret: str = field(default_factory=lambda: _env("GROWW_API_SECRET", ""))
    totp_secret: str = field(default_factory=lambda: _env("GROWW_TOTP_SECRET", ""))  # optional

    # --- Capital & risk ---------------------------------------------------
    total_capital: float = field(default_factory=lambda: float(_env("ORB_CAPITAL", "100000")))
    base_risk_pct: float = field(default_factory=lambda: float(_env("ORB_RISK_PCT", "1.0")))  # % per trade
    min_risk_pct: float = 0.2            # sentiment clamp floor
    max_risk_pct: float = 1.5            # HARD ceiling (blueprint rule: 1.0-1.5%)
    max_capital_deploy_pct: float = 60.0 # never put >60% of capital into premium
    daily_loss_limit: float = field(default_factory=lambda: float(_env("ORB_DAILY_LOSS", "2000")))  # stop for the day
    daily_profit_lock: float = 0.0       # optional: stop after this much profit (0 = off)
    max_trades_per_day: int = field(default_factory=lambda: int(_env("ORB_MAX_TRADES", "2")))

    # --- Contracts --------------------------------------------------------
    # CE trades upside breakouts; PE (optional) trades downside breakouts.
    ce_symbol: str = field(default_factory=lambda: _env("ORB_CE_SYMBOL", "SENSEX26NOV86000CE"))
    pe_symbol: str = field(default_factory=lambda: _env("ORB_PE_SYMBOL", "SENSEX26NOV86000PE"))
    exchange: str = field(default_factory=lambda: _env("ORB_EXCHANGE", "BSE"))
    segment: str = field(default_factory=lambda: _env("ORB_SEGMENT", "FNO"))            # FNO | COMMODITY
    spot_segment: str = field(default_factory=lambda: _env("ORB_SPOT_SEGMENT", "CASH"))  # CASH (index) | COMMODITY (futures)
    use_vix: bool = field(default_factory=lambda: _env("ORB_USE_VIX", "1") == "1")     # India VIX: equity only
    # Fallback only - the bot tries to read the live lot size from the
    # instrument master first. Exchanges revise lot sizes; never trust a constant.
    lot_size_fallback: int = int(_env("ORB_LOT_SIZE", "20"))
    tick_size: float = field(default_factory=lambda: float(_env("ORB_TICK_SIZE", "0.05")))

    # --- Volatility -------------------------------------------------------
    vix_symbol: str = field(default_factory=lambda: _env("ORB_VIX_SYMBOL", "INDIAVIX"))
    vix_threshold: float = 18.0
    vix_refresh_sec: int = 60            # VIX moves slowly - don't poll it every tick
    vix_fallback: float = 15.0
    max_vix_to_trade: float = 30.0       # extreme regime => sit out

    # --- Session windows (IST) -------------------------------------------
    market_open: time = time(9, 15, 0)
    opening_range_end: time = time(9, 30, 0)   # 15-minute opening range
    entry_cutoff: time = time(9, 45, 0)        # no new entries after this
    square_off: time = time(10, 0, 0)          # flat by 10:00, no exceptions

    # --- Strategy parameters ---------------------------------------------
    breakout_buffer_pct: float = 0.5     # entry = OR high * (1 + 0.5%)
    max_initial_sl_pct: float = 20.0     # option premium SL cap (% of entry)
    min_initial_sl_pct: float = 6.0      # avoid noise-tight stops on premium
    base_trail_pct: float = 12.0         # trailing buffer as % of entry premium
    breakeven_at_r: float = 1.0          # move stop to entry once +1R
    min_or_ticks: int = 30               # need enough data to trust the range

    # --- Strategy filters (each one A/B-testable via backtest.py) --------
    strategy_name: str = "orb"   # set per run via --strategies; see STRATEGIES

    # --- Signal-model parameters (momentum / supertrend / ml) ------------
    signal_start: time = time(9, 20, 0)   # indicators need a few bars first
    signal_sl_pct: float = 10.0           # stop for signal models = entry premium - 10%
    ema_fast: int = 5
    ema_slow: int = 13
    st_atr: int = 7
    st_mult: float = 2.5
    ml_model_file: Path = Path("data/ml_model.json")
    ml_horizon: int = 5                   # minutes ahead the model predicts
    ml_up: float = 0.58                   # buy CE if P(up) >= this
    ml_down: float = 0.42                 # buy PE if P(up) <= this
    # 1. Underlying confirmation: CE only if the INDEX breaks its own OR high,
    #    PE only if it breaks its OR low. Filters premium spikes from IV/noise.
    use_spot_confirm: bool = True
    # 2. Breakout must HOLD above trigger this long (seconds) - kills 1-tick wicks.
    confirm_sec: int = 20
    # 3. Opening-range width filter (% of OR high). Too narrow = noise; too wide =
    #    stop so far away that size collapses and R:R is poor.
    or_min_width_pct: float = 3.0
    or_max_width_pct: float = 30.0
    # 4. Time stop: no follow-through (< time_stop_min_r R) after N minutes => exit.
    #    Long options bleed theta; failed breakouts rarely recover in this window.
    time_stop_min: int = 12
    time_stop_min_r: float = 0.3

    # --- Contract selection ----------------------------------------------
    # Auto-pick the at-the-money CE/PE of the nearest expiry at 09:15 (live/paper).
    auto_atm: bool = field(default_factory=lambda: _env("ORB_AUTO_ATM", "1") == "1")
    underlying: str = field(default_factory=lambda: _env("ORB_UNDERLYING", "SENSEX"))
    spot_symbol: str = field(default_factory=lambda: _env("ORB_SPOT_SYMBOL", "SENSEX"))  # index trading_symbol, CASH segment
    strike_step: int = int(_env("ORB_STRIKE_STEP", "100"))  # SENSEX 100, NIFTY 50

    # --- Execution --------------------------------------------------------
    poll_interval_sec: float = 1.0       # respect broker rate limits
    limit_offset_pct: float = 0.5        # marketable-limit offset vs LTP
    fill_timeout_sec: float = 5.0        # cancel+retry if not filled
    max_fill_retries: int = 3
    paper_slippage_pct: float = 0.05     # extra slippage on top of the bid/ask spread
    # Spread model for sim/backtest (paper uses REAL bid/ask from Groww quotes).
    spread_pct: float = 0.4              # full bid-ask spread as % of premium
    open_spread_mult: float = 2.0        # spreads are wider in the first 5 minutes
    # Broker-side protective stop (LIVE): SL-M order placed at Groww right after entry and
    # trailed with modify_order, so the position is protected even if this bot dies.
    broker_stop: bool = True
    stop_update_min_ticks: int = 4       # only modify when the stop moved >= 4 ticks
    stop_update_min_sec: float = 5.0     # ...and at most every 5 s (order rate limits)
    stop_poll_sec: float = 3.0           # check whether Groww already triggered the stop
    stale_sec: float = 30.0              # no price change this long => block new entries
    skip_expiry_day: bool = field(default_factory=lambda: _env("ORB_SKIP_EXPIRY", "0") == "1")
    # Optional Telegram alerts (trades, risk events, end-of-day summary)
    telegram_token: str = field(default_factory=lambda: _env("TELEGRAM_BOT_TOKEN", ""))
    telegram_chat: str = field(default_factory=lambda: _env("TELEGRAM_CHAT_ID", ""))

    # --- Sentiment --------------------------------------------------------
    use_finbert: bool = field(default_factory=lambda: _env("ORB_USE_FINBERT", "0") == "1")
    headlines_file: Path = field(default_factory=lambda: Path(_env("ORB_HEADLINES", "data/headlines.txt")))
    sentiment_block_below: float = -0.5

    # --- Charges (approximate; verify against Groww's current schedule) --
    brokerage_per_order: float = 20.0
    stt_sell_pct: float = 0.1            # options: on sell-side premium
    exchange_txn_pct: float = 0.0325     # BSE Sensex options ~0.0325%; NSE ~0.0350%
    sebi_fee_per_crore: float = 10.0
    stamp_buy_pct: float = 0.003
    gst_pct: float = 18.0

    # --- Calendar ---------------------------------------------------------
    holidays_file: Path = Path("data/holidays.txt")  # YYYY-MM-DD per line

    # --- Paths ------------------------------------------------------------
    reports_dir: Path = Path("reports")
    journal_file: Path = Path("reports/journal.jsonl")
    log_dir: Path = Path("logs")
    kill_switch_file: Path = Path("STOP")  # `touch STOP` => flatten & exit

    monthly_target: float = field(default_factory=lambda: float(_env("ORB_MONTHLY_TARGET", "50000")))

    def __post_init__(self) -> None:
        # Session window from env: ORB_SESSION_OPEN=HH:MM (+ optional ORB_OR_MIN / ORB_ENTRY_MIN / ORB_SESSION_MIN)
        op = os.environ.get("ORB_SESSION_OPEN")
        if op:
            o = datetime.combine(date(2000, 1, 3), time.fromisoformat(op))
            m = lambda k, d: timedelta(minutes=int(os.environ.get(k, d)))  # noqa: E731
            self.market_open = o.time()
            self.opening_range_end = (o + m("ORB_OR_MIN", 15)).time()
            self.entry_cutoff = (o + m("ORB_ENTRY_MIN", 30)).time()
            self.square_off = (o + m("ORB_SESSION_MIN", 45)).time()
            self.signal_start = (o + timedelta(minutes=5)).time()
        for attr, key in (("stt_sell_pct", "ORB_STT_PCT"), ("exchange_txn_pct", "ORB_EXCH_PCT")):
            if os.environ.get(key):
                setattr(self, attr, float(os.environ[key]))

    def validate(self) -> None:
        if self.mode not in ("paper", "live", "sim", "backtest"):
            raise ValueError(f"Unknown ORB_MODE={self.mode!r}")
        if self.mode == "live" and os.environ.get("ORB_CONFIRM_LIVE") != "YES":
            raise RuntimeError("Live mode requires ORB_CONFIRM_LIVE=YES in the environment.")
        if self.mode in ("paper", "live") and not (self.api_key and (self.api_secret or self.totp_secret)):
            raise RuntimeError("Set GROWW_API_KEY plus GROWW_API_SECRET (or GROWW_TOTP_SECRET).")
        if not (0 < self.base_risk_pct <= self.max_risk_pct):
            raise ValueError("base_risk_pct must be within (0, max_risk_pct].")


def baseline(cfg: Config) -> Config:
    """The blueprint's logic (with safety fixes) but none of the extra filters - the A/B control."""
    from dataclasses import replace
    return replace(cfg, strategy_name="orb_baseline", use_spot_confirm=False, confirm_sec=0,
                   or_min_width_pct=0.0, or_max_width_pct=1e9, time_stop_min=0)

# ==========================================================================
# CLOCK
# ==========================================================================
class RealClock:
    def now(self) -> datetime:
        return datetime.now(IST)

    def sleep(self, seconds: float) -> None:
        _time.sleep(seconds)


class SimClock:
    """Advances virtual time instantly on sleep()."""

    def __init__(self, start: datetime):
        self._t = start

    def now(self) -> datetime:
        return self._t

    def sleep(self, seconds: float) -> None:
        self._t += timedelta(seconds=seconds)

# ==========================================================================
# RISK
# ==========================================================================
def round_to_tick(price: float, tick: float = 0.05) -> float:
    return round(round(price / tick) * tick, 2)


def sentiment_adjusted_risk_pct(cfg: Config, sentiment: float) -> float:
    """Risk_adj = Base * (1 + S), clamped to [min_risk_pct, max_risk_pct]."""
    adj = cfg.base_risk_pct * (1.0 + sentiment)
    return max(cfg.min_risk_pct, min(adj, cfg.max_risk_pct))


@dataclass
class SizeDecision:
    lots: int
    qty: int
    risk_pct: float
    rupee_risk_budget: float
    rupee_risk_actual: float
    capital_needed: float
    reason: str


def size_position(cfg: Config, sentiment: float, entry: float, stop: float, lot_size: int) -> SizeDecision:
    """Lots = floor((Capital * Risk%) / (Entry - SL) / LotSize), also capped by deployable capital.

    Returns 0 lots (never a forced minimum of 1) when the risk budget can't afford one lot.
    """
    risk_pct = sentiment_adjusted_risk_pct(cfg, sentiment)
    budget = cfg.total_capital * risk_pct / 100.0
    per_unit = entry - stop
    if per_unit <= 0 or entry <= 0:
        return SizeDecision(0, 0, risk_pct, budget, 0, 0, "invalid stop distance")

    lots_by_risk = math.floor(budget / per_unit / lot_size)
    deployable = cfg.total_capital * cfg.max_capital_deploy_pct / 100.0
    lots_by_capital = math.floor(deployable / (entry * lot_size))
    lots = max(0, min(lots_by_risk, lots_by_capital))

    if lots == 0:
        why = ("risk budget too small for 1 lot" if lots_by_risk == 0
               else "insufficient deployable capital for 1 lot")
        return SizeDecision(0, 0, risk_pct, budget, 0, 0, why)

    qty = lots * lot_size
    limiter = "risk" if lots_by_risk <= lots_by_capital else "capital"
    return SizeDecision(lots, qty, risk_pct, budget, qty * per_unit, qty * entry, f"limited by {limiter}")


def initial_stop(cfg: Config, entry: float, or_low: float) -> float:
    """Structural stop at the opening-range low, clamped to a sane % band of premium."""
    lo = entry * (1 - cfg.max_initial_sl_pct / 100.0)
    hi = entry * (1 - cfg.min_initial_sl_pct / 100.0)
    return round_to_tick(min(max(or_low, lo), hi), cfg.tick_size)


def vix_multiplier(cfg: Config, vix: float) -> float:
    """1.0 if VIX <= threshold else 1 + (VIX - threshold) / 10."""
    return 1.0 if vix <= cfg.vix_threshold else 1.0 + (vix - cfg.vix_threshold) / 10.0


def trailing_buffer(cfg: Config, entry: float, vix: float) -> float:
    return entry * cfg.base_trail_pct / 100.0 * vix_multiplier(cfg, vix)


def next_stop(cfg: Config, current_stop: float, entry: float, peak: float, vix: float, r_unit: float) -> float:
    """S(t) = max(current stop, P_max - D(t)); stop only ratchets UP. Breakeven after +1R."""
    candidate = peak - trailing_buffer(cfg, entry, vix)
    if r_unit > 0 and peak - entry >= cfg.breakeven_at_r * r_unit:
        candidate = max(candidate, entry)
    return round_to_tick(max(current_stop, candidate), cfg.tick_size)


@dataclass
class Charges:
    brokerage: float
    stt: float
    exchange: float
    sebi: float
    stamp: float
    gst: float

    @property
    def total(self) -> float:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst


def estimate_charges(cfg: Config, buy_value: float, sell_value: float, orders: int = 2) -> Charges:
    turnover = buy_value + sell_value
    brokerage = cfg.brokerage_per_order * orders
    stt = sell_value * cfg.stt_sell_pct / 100.0
    exch = turnover * cfg.exchange_txn_pct / 100.0
    sebi = turnover * cfg.sebi_fee_per_crore / 1e7
    stamp = buy_value * cfg.stamp_buy_pct / 100.0
    gst = (brokerage + exch + sebi) * cfg.gst_pct / 100.0
    return Charges(*(round(x, 2) for x in (brokerage, stt, exch, sebi, stamp, gst)))

# ==========================================================================
# SENTIMENT
# ==========================================================================
log = logging.getLogger("orb")


@dataclass
class SentimentResult:
    score: float
    source: str
    headlines: list[tuple[str, float]] = field(default_factory=list)


def load_headlines(path: Path) -> list[str]:
    """One headline per line. Feed this file from your news source (RSS/API) before 9:10 AM."""
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip() and not ln.startswith("#")]


class FinBertScorer:
    MODEL = "ProsusAI/finbert"

    def __init__(self) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(self.MODEL)
        self.model = AutoModelForSequenceClassification.from_pretrained(self.MODEL).eval()
        # Read label order from the model config instead of assuming index positions.
        labels = {v.lower(): int(k) for k, v in self.model.config.id2label.items()}
        self.pos, self.neg = labels["positive"], labels["negative"]

    def score(self, texts: list[str]) -> list[float]:
        with self.torch.no_grad():
            enc = self.tok(texts, return_tensors="pt", padding=True, truncation=True, max_length=128)
            probs = self.torch.softmax(self.model(**enc).logits, dim=-1)
        return [float(p[self.pos] - p[self.neg]) for p in probs]


def morning_sentiment(path: Path, use_finbert: bool) -> SentimentResult:
    heads = load_headlines(path)
    if not heads:
        return SentimentResult(0.0, "no headlines - neutral")
    if not use_finbert:
        return SentimentResult(0.0, "FinBERT disabled - neutral", [(h, 0.0) for h in heads])
    try:
        scores = FinBertScorer().score(heads)
    except Exception as e:
        log.warning("FinBERT unavailable (%s) - using neutral sentiment", e)
        return SentimentResult(0.0, "FinBERT error - neutral", [(h, 0.0) for h in heads])
    # Average, but let the single most negative headline dominate if it's severe:
    # one "exchange halts trading" outweighs three mildly positive stories.
    avg = sum(scores) / len(scores)
    worst = min(scores)
    final = worst if worst < -0.7 else avg
    return SentimentResult(round(final, 3), f"FinBERT on {len(heads)} headline(s)", list(zip(heads, scores)))

# ==========================================================================
# INSTRUMENTS
# ==========================================================================
_STRIKE = re.compile(r"-(\d+(?:\.\d+)?)-(CE|PE)$")


def _as_list(resp, *keys) -> list:
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        for k in keys:
            if isinstance(resp.get(k), list):
                return resp[k]
    return []


def nearest_expiry(g, exchange: str, underlying: str, day: date) -> date:
    for y, m in ((day.year, day.month), ((day.year + (day.month == 12)), day.month % 12 + 1)):
        resp = g.get_expiries(exchange=exchange, underlying_symbol=underlying, year=y, month=m)
        exps = sorted(date.fromisoformat(str(e)[:10]) for e in _as_list(resp, "expiries", "data"))
        later = [e for e in exps if e >= day]
        if later:
            return later[0]
    raise RuntimeError(f"No expiry found for {underlying} on/after {day}")


def option_groww_symbol(exchange: str, underlying: str, expiry: date, strike: float, side: str) -> str:
    k = int(strike) if float(strike).is_integer() else strike
    return f"{exchange}-{underlying}-{expiry.strftime('%d%b%y')}-{k}-{side}"


def atm_pair(g, exchange: str, underlying: str, day: date, spot: float, step: int) -> tuple[str, str, date, float]:
    """Return (ce_groww_symbol, pe_groww_symbol, expiry, strike) closest to spot."""
    expiry = nearest_expiry(g, exchange, underlying, day)
    strike = round(spot / step) * step
    try:  # prefer strikes that actually exist for this expiry
        contracts = [str(c) for c in _as_list(
            g.get_contracts(exchange=exchange, underlying_symbol=underlying, expiry_date=expiry.isoformat()),
            "contracts", "data")]
        strikes = {float(m.group(1)) for c in contracts if (m := _STRIKE.search(c))}
        if strikes:
            strike = min(strikes, key=lambda s: abs(s - spot))
    except Exception:
        pass
    return (option_groww_symbol(exchange, underlying, expiry, strike, "CE"),
            option_groww_symbol(exchange, underlying, expiry, strike, "PE"), expiry, strike)

# ==========================================================================
# BROKER
# ==========================================================================


@dataclass
class Fill:
    order_id: str
    side: str
    qty: int
    avg_price: float
    attempts: int
    note: str = ""


class Broker(Protocol):
    def ltps(self, symbols: list[str]) -> dict[str, float]: ...
    def ltp(self, symbol: str) -> float: ...
    def spot(self) -> float | None: ...
    def vix(self) -> float: ...
    def lot_size(self, symbol: str) -> int | None: ...
    def buy(self, symbol: str, qty: int, ref_price: float) -> Fill | None: ...
    def sell(self, symbol: str, qty: int, ref_price: float) -> Fill: ...


def _pick(d: Any, *keys: str, default: Any = None) -> Any:
    """Tolerant dict access across SDK response shapes."""
    if not isinstance(d, dict):
        return default
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


# --------------------------------------------------------------------------
# Real Groww
# --------------------------------------------------------------------------
class GrowwBroker:
    FILLED = {"EXECUTED", "COMPLETED"}
    DEAD = {"REJECTED", "FAILED", "CANCELLED"}

    def __init__(self, cfg: Config, clock: RealClock):
        from growwapi import GrowwAPI  # imported lazily so sim mode needs no SDK

        self.cfg, self.clock = cfg, clock
        if cfg.totp_secret:  # TOTP flow: no daily approval needed on Groww Cloud
            import pyotp
            token = GrowwAPI.get_access_token(api_key=cfg.api_key, totp=pyotp.TOTP(cfg.totp_secret).now())
        else:                # key+secret flow: approve the key daily on Groww Cloud
            token = GrowwAPI.get_access_token(api_key=cfg.api_key, secret=cfg.api_secret)
        self.g = GrowwAPI(token)
        self._vix_cache: tuple[float, float] | None = None  # (epoch, value)
        self.seg = getattr(self.g, f"SEGMENT_{cfg.segment}")            # FNO or COMMODITY
        self.spot_seg = getattr(self.g, f"SEGMENT_{cfg.spot_segment}")  # CASH (index) or COMMODITY (futures)
        self._spot_symbol: str | None = None
        self._master = None

    # ---- market data ----
    # Live Data limit: 10 req/s, 300 req/min. One batched get_ltp per tick for all
    # option legs (~60/min at 1s polling) + VIX once a minute stays well inside it.
    def ltps(self, symbols: list[str]) -> dict[str, float]:
        keys = tuple(f"{self.cfg.exchange}_{s}" for s in symbols)
        resp = self.g.get_ltp(segment=self.seg, exchange_trading_symbols=keys)
        out = {}
        for s, k in zip(symbols, keys):
            v = resp.get(k) if isinstance(resp, dict) else None
            if isinstance(v, dict):
                v = _pick(v, "ltp", "last_price")
            if v is None:
                raise RuntimeError(f"get_ltp missing {k}: {resp}")
            out[s] = float(v)
        return out

    def ltp(self, symbol: str) -> float:
        return self.ltps([symbol])[symbol]

    def vix(self) -> float:
        if not self.cfg.use_vix:
            return 0.0
        now = self.clock.now().timestamp()
        if self._vix_cache and now - self._vix_cache[0] < self.cfg.vix_refresh_sec:
            return self._vix_cache[1]
        try:
            q = self.g.get_quote(trading_symbol=self.cfg.vix_symbol, exchange=self.g.EXCHANGE_NSE, segment=self.g.SEGMENT_CASH)
            val = float(_pick(q, "last_price", "ltp"))
        except Exception as e:  # keep last known value rather than a fake "calm" number
            log.warning("VIX fetch failed (%s); using %s", e, "last known" if self._vix_cache else "fallback")
            val = self._vix_cache[1] if self._vix_cache else self.cfg.vix_fallback
        self._vix_cache = (now, val)
        return val

    def spot(self) -> float | None:
        """Underlying index LTP (CASH segment). One extra Live Data request per tick."""
        if self._spot_symbol is None:
            self._spot_symbol = (self.nearest_future() if self.cfg.spot_symbol.upper() == "AUTO"
                                 else self.cfg.spot_symbol)
            log.info("Underlying price source: %s_%s (%s)", self.cfg.exchange, self._spot_symbol, self.cfg.spot_segment)
        key = f"{self.cfg.exchange}_{self._spot_symbol}"
        resp = self.g.get_ltp(segment=self.spot_seg, exchange_trading_symbols=(key,))
        v = resp.get(key) if isinstance(resp, dict) else None
        if isinstance(v, dict):
            v = _pick(v, "ltp", "last_price")
        return float(v) if v is not None else None

    # ---- instrument master (documented: get_all_instruments -> DataFrame) ----
    def _instruments(self):
        if self._master is None:
            df = self.g.get_all_instruments()
            u = self.cfg.underlying.upper()
            mask = (df["exchange"].astype(str).str.upper() == self.cfg.exchange.upper()) & (
                df["underlying_symbol"].astype(str).str.upper() == u)  # exact: CRUDEOIL != CRUDEOILM
            self._master = df[mask].copy()
            self._master["exp"] = self._master["expiry_date"].astype(str).str[:10]
        return self._master

    def nearest_future(self) -> str:
        df = self._instruments()
        today = self.clock.now().date().isoformat()
        fut = df[(df["instrument_type"].astype(str).str.upper().isin(["FUT", "FUTCOM", "FUTIDX"])) & (df["exp"] >= today)]
        if fut.empty:
            raise RuntimeError(f"no {self.cfg.underlying} futures found in the instrument master")
        return str(fut.sort_values("exp").iloc[0]["trading_symbol"])

    def atm_from_master(self, spot: float) -> tuple[str, str]:
        df = self._instruments()
        today = self.clock.now().date().isoformat()
        opt = df[df["instrument_type"].astype(str).str.upper().isin(["CE", "PE"]) & (df["exp"] >= today)]
        if opt.empty:
            raise RuntimeError(f"no {self.cfg.underlying} options found in the instrument master")
        exp = opt["exp"].min()
        opt = opt[opt["exp"] == exp]
        strike = min(opt["strike_price"].astype(float).unique(), key=lambda k: abs(k - spot))
        pick = opt[opt["strike_price"].astype(float) == strike]
        sym = {str(r["instrument_type"]).upper(): str(r["trading_symbol"]) for _, r in pick.iterrows()}
        self.expiry = date.fromisoformat(exp)
        log.info("ATM (master): spot %.2f -> strike %s expiry %s", spot, strike, exp)
        return sym["CE"], sym["PE"]

    def atm_contracts(self) -> tuple[str, str] | None:
        """At session open: nearest-expiry ATM CE/PE, returned as exchange trading_symbols."""
        if self.cfg.segment == "COMMODITY":  # MCX: resolve from the instrument master
            spot = self.spot()
            return self.atm_from_master(spot) if spot else None

        spot = self.spot()
        if spot is None:
            return None
        ce_g, pe_g, expiry, strike = atm_pair(self.g, self.cfg.exchange, self.cfg.underlying,
                                              self.clock.now().date(), spot, self.cfg.strike_step)
        out = []
        for gs in (ce_g, pe_g):
            inst = self.g.get_instrument_by_groww_symbol(groww_symbol=gs)
            ts = _pick(inst, "trading_symbol")
            if not ts:
                raise RuntimeError(f"no trading_symbol for {gs}")
            out.append(ts)
        log.info("ATM: spot %.2f -> strike %s expiry %s", spot, strike, expiry)
        self.expiry = expiry
        return out[0], out[1]

    tick: float | None = None

    def lot_size(self, symbol: str) -> int | None:
        try:
            inst = self.g.get_instrument_by_exchange_and_trading_symbol(exchange=self.cfg.exchange, trading_symbol=symbol)
            ls, tk = _pick(inst, "lot_size"), _pick(inst, "tick_size")
            if tk:
                tk = float(tk) / (100.0 if self.cfg.exchange.upper() == "MCX" else 1.0)  # MCX: paise -> rupees
                self.tick = round(tk, 4) if tk > 0 else None
            return int(ls) if ls else None
        except Exception as e:
            log.warning("Lot-size lookup failed: %s", e)
            return None

    # ---- orders ----
    def _place(self, symbol: str, qty: int, side: str, order_type: str, price: float | None) -> str:
        g = self.g
        kwargs = dict(
            trading_symbol=symbol, quantity=qty, validity=g.VALIDITY_DAY, exchange=self.cfg.exchange,
            segment=self.seg, product=g.PRODUCT_MIS, order_type=order_type,
            transaction_type=g.TRANSACTION_TYPE_BUY if side == "BUY" else g.TRANSACTION_TYPE_SELL,
            order_reference_id=uuid.uuid4().hex[:20],
        )
        if price is not None:
            kwargs["price"] = price
        resp = g.place_order(**kwargs)
        oid = _pick(resp, "groww_order_id", "order_id")
        if not oid:
            raise RuntimeError(f"place_order returned no order id: {resp}")
        return oid

    def _await(self, oid: str) -> tuple[str, int, float]:
        deadline = self.clock.now().timestamp() + self.cfg.fill_timeout_sec
        status, filled, avg = "NEW", 0, 0.0
        while self.clock.now().timestamp() < deadline:
            st = self.g.get_order_status(segment=self.seg, groww_order_id=oid)
            status = str(_pick(st, "order_status", "status", default="")).upper()
            filled = int(_pick(st, "filled_quantity", default=0) or 0)
            if status in self.FILLED or status in self.DEAD:
                break
            self.clock.sleep(0.5)
        if filled:
            det = self.g.get_order_detail(segment=self.seg, groww_order_id=oid)
            avg = float(_pick(det, "average_fill_price", "avg_price", default=0) or 0)
        return status, filled, avg

    def _cancel(self, oid: str) -> None:
        try:
            self.g.cancel_order(segment=self.seg, groww_order_id=oid)
        except Exception as e:
            log.warning("Cancel %s failed: %s", oid, e)

    def _execute(self, symbol: str, qty: int, side: str, ref_price: float, must_fill: bool) -> Fill | None:
        remaining, value, done, last_oid = qty, 0.0, 0, ""
        sign = 1 if side == "BUY" else -1
        for attempt in range(1, self.cfg.max_fill_retries + 1):
            ref = self.ltp(symbol) if attempt > 1 else ref_price
            px = round_to_tick(ref * (1 + sign * self.cfg.limit_offset_pct / 100.0), self.tick or self.cfg.tick_size)
            last_oid = self._place(symbol, remaining, side, self.g.ORDER_TYPE_LIMIT, px)
            status, filled, avg = self._await(last_oid)
            if filled:
                value += filled * (avg or px)
                done += filled
                remaining -= filled
            if remaining <= 0:
                return Fill(last_oid, side, done, value / done, attempt)
            if status not in self.DEAD:
                self._cancel(last_oid)
            log.info("%s %s: %d/%d filled after attempt %d", side, symbol, done, qty, attempt)

        if must_fill and remaining > 0:  # exits must never be left hanging
            log.warning("Exit limit retries exhausted; sending MARKET for %d", remaining)
            last_oid = self._place(symbol, remaining, side, self.g.ORDER_TYPE_MARKET, None)
            _, filled, avg = self._await(last_oid)
            value += filled * (avg or ref_price)
            done += filled
            remaining -= filled
            if remaining > 0:
                raise RuntimeError(f"EXIT NOT COMPLETE: {remaining} units of {symbol} still open - CHECK MANUALLY")
        if done == 0:
            return None
        return Fill(last_oid, side, done, value / done, self.cfg.max_fill_retries,
                    note="partial" if done < qty else "")

    def buy(self, symbol: str, qty: int, ref_price: float) -> Fill | None:
        return self._execute(symbol, qty, "BUY", ref_price, must_fill=False)

    def sell(self, symbol: str, qty: int, ref_price: float) -> Fill:
        return self._execute(symbol, qty, "SELL", ref_price, must_fill=True)  # type: ignore[return-value]

    # ---- protective stop at the broker (live) ----
    supports_broker_stop = True

    def place_stop(self, symbol: str, qty: int, trigger: float) -> str:
        """SL-M SELL at Groww: if this bot dies, the exchange still exits the position."""
        g = self.g
        resp = g.place_order(
            trading_symbol=symbol, quantity=qty, validity=g.VALIDITY_DAY, exchange=self.cfg.exchange,
            segment=self.seg, product=g.PRODUCT_MIS, order_type=g.ORDER_TYPE_STOP_LOSS_MARKET,
            transaction_type=g.TRANSACTION_TYPE_SELL, trigger_price=trigger,
            order_reference_id=uuid.uuid4().hex[:20])
        oid = _pick(resp, "groww_order_id", "order_id")
        status = str(_pick(resp, "order_status", default="")).upper()
        if not oid or status in self.DEAD:
            raise RuntimeError(f"protective stop rejected: {resp}")
        return oid

    def modify_stop(self, oid: str, qty: int, trigger: float) -> None:
        self.g.modify_order(quantity=qty, order_type=self.g.ORDER_TYPE_STOP_LOSS_MARKET, segment=self.seg,
                            groww_order_id=oid, trigger_price=trigger)

    def stop_state(self, oid: str) -> tuple[str, int, float]:
        """(status, filled_qty, avg_fill_price) without waiting."""
        st = self.g.get_order_status(segment=self.seg, groww_order_id=oid)
        status = str(_pick(st, "order_status", "status", default="")).upper()
        filled = int(_pick(st, "filled_quantity", default=0) or 0)
        avg = 0.0
        if filled:
            det = self.g.get_order_detail(segment=self.seg, groww_order_id=oid)
            avg = float(_pick(det, "average_fill_price", default=0) or 0)
        return status, filled, avg

    def cancel_stop(self, oid: str) -> tuple[str, int, float]:
        """Cancel, then return the FINAL state - the stop may have filled in the meantime."""
        self._cancel(oid)
        for _ in range(6):
            status, filled, avg = self.stop_state(oid)
            if status in self.FILLED or status in self.DEAD:
                return status, filled, avg
            self.clock.sleep(0.5)
        return status, filled, avg

    def net_qty(self, symbol: str) -> int | None:
        """Net open quantity at Groww for this contract (reconciliation)."""
        try:
            resp = self.g.get_position_for_trading_symbol(trading_symbol=symbol, segment=self.seg)
        except Exception as e:
            log.warning("Position check failed for %s: %s", symbol, e)
            return None
        rows = resp.get("positions", []) if isinstance(resp, dict) else (resp or [])
        return sum(int(_pick(r, "quantity", default=0) or 0) for r in rows if isinstance(r, dict))

    def bid_ask(self, symbol: str) -> tuple[float, float] | None:
        q = self.g.get_quote(trading_symbol=symbol, exchange=self.cfg.exchange, segment=self.seg)
        bid, ask = _pick(q, "bid_price"), _pick(q, "offer_price")
        if not (bid and ask):
            depth = _pick(q, "depth", default={}) or {}
            b, a = (depth.get("buy") or [{}])[0], (depth.get("sell") or [{}])[0]
            bid, ask = _pick(b, "price"), _pick(a, "price")
        return (float(bid), float(ask)) if bid and ask and float(ask) >= float(bid) > 0 else None


# --------------------------------------------------------------------------
# Paper: live data, fake fills
# --------------------------------------------------------------------------
class _SimFillsMixin:
    cfg: Config

    def _half_spread(self, px: float) -> float:
        """Modelled half bid-ask spread; wider in the first 5 minutes after the open."""
        mult = 1.0
        clock = getattr(self, "clock", None)
        if clock is not None:
            t = clock.now().time()
            if time(9, 15) <= t < time(9, 20):
                mult = self.cfg.open_spread_mult
        return max(self.cfg.tick_size, px * self.cfg.spread_pct / 200.0 * mult)

    def _fill(self, side: str, qty: int, ref_price: float, bid_ask: tuple[float, float] | None = None) -> Fill:
        slip = ref_price * self.cfg.paper_slippage_pct / 100.0
        if bid_ask:  # real quote: buy at the offer, sell at the bid
            px = bid_ask[1] + slip if side == "BUY" else bid_ask[0] - slip
            note = f"simulated at {'ask' if side == 'BUY' else 'bid'}"
        else:        # modelled spread around the last price
            hs = self._half_spread(ref_price)
            px = ref_price + hs + slip if side == "BUY" else ref_price - hs - slip
            note = "simulated (modelled spread)"
        tk = getattr(self, "tick", None) or self.cfg.tick_size
        px = round_to_tick(max(tk, px), tk)
        return Fill(f"PAPER-{uuid.uuid4().hex[:8]}", side, qty, px, 1, note)

    def buy(self, symbol: str, qty: int, ref_price: float) -> Fill | None:
        return self._fill("BUY", qty, ref_price)

    def sell(self, symbol: str, qty: int, ref_price: float) -> Fill:
        return self._fill("SELL", qty, ref_price)


class PaperBroker(_SimFillsMixin):
    def __init__(self, cfg: Config, clock: RealClock):
        self.cfg = cfg
        self.data = GrowwBroker(cfg, clock)

    @property
    def expiry(self):
        return getattr(self.data, "expiry", None)

    @property
    def tick(self):
        return getattr(self.data, "tick", None)

    def _quote(self, symbol: str):
        try:
            return self.data.bid_ask(symbol)
        except Exception as e:
            log.warning("Quote for paper fill failed (%s) - using modelled spread", e)
            return None

    def buy(self, symbol: str, qty: int, ref_price: float) -> Fill | None:
        return self._fill("BUY", qty, ref_price, self._quote(symbol))

    def sell(self, symbol: str, qty: int, ref_price: float) -> Fill:
        return self._fill("SELL", qty, ref_price, self._quote(symbol))

    def ltps(self, symbols: list[str]) -> dict[str, float]:
        return self.data.ltps(symbols)

    def ltp(self, symbol: str) -> float:
        return self.data.ltp(symbol)

    def spot(self) -> float | None:
        return self.data.spot()

    def atm_contracts(self) -> tuple[str, str] | None:
        return self.data.atm_contracts()

    def vix(self) -> float:
        return self.data.vix()

    def lot_size(self, symbol: str) -> int | None:
        return self.data.lot_size(symbol)


# --------------------------------------------------------------------------
# Sim: synthetic market for offline runs
# --------------------------------------------------------------------------
class SimBroker(_SimFillsMixin):
    """Index random walk with a per-day trend regime; CE/PE premiums follow the index
    with delta ~0.5 plus their own noise, transient spikes and theta decay.
    NOT a market model - it only exercises every code path. Results prove nothing."""

    def __init__(self, cfg: Config, clock: SimClock, seed: int):
        self.cfg, self.clock = cfg, clock
        self.rng = r = random.Random(seed)
        self.vrng = random.Random(seed + 1)  # separate stream: A/B variants see identical prices
        self._vix = r.uniform(11, 24)
        self.s0 = self.s = r.uniform(80_000, 88_000)
        self.vol = r.uniform(0.00002, 0.00005)            # per-second index vol (~0.06-0.15% per 15 min)
        self.drift = r.choice([-1, 0, 1]) * r.uniform(0.0, 0.0000025)  # trend days up to ~0.7% by 10:00
        self.base = {"CE": r.uniform(300, 500), "PE": r.uniform(300, 500)}
        self.noise = {"CE": 0.0, "PE": 0.0}
        self.spike = {"CE": 0.0, "PE": 0.0}
        self._t = clock.now().timestamp()
        self._elapsed = 0

    def _advance(self) -> None:
        now = self.clock.now().timestamp()
        while self._t + 1 <= now:
            self._t += 1
            self._elapsed += 1
            self.s *= math.exp(self.rng.gauss(self.drift, self.vol))
            for k in ("CE", "PE"):
                self.noise[k] += self.rng.gauss(0, 0.12)
                self.spike[k] *= 0.93
                if self.rng.random() < 0.0008:              # IV pop / fake breakout
                    self.spike[k] += self.rng.uniform(0.04, 0.09) * self.base[k]

    def _premium(self, side: str) -> float:
        d = (self.s - self.s0) * (0.5 if side == "CE" else -0.5)
        theta = 0.002 * self._elapsed
        return max(5.0, self.base[side] + d + self.noise[side] + self.spike[side] - theta)

    def ltps(self, symbols: list[str]) -> dict[str, float]:
        self._advance()
        return {s: round_to_tick(self._premium("PE" if s.endswith("PE") else "CE"), self.cfg.tick_size)
                for s in symbols}

    def ltp(self, symbol: str) -> float:
        return self.ltps([symbol])[symbol]

    def spot(self) -> float | None:
        self._advance()
        return round(self.s, 2)

    def vix(self) -> float:
        self._vix = max(9.0, self._vix + self.vrng.gauss(0, 0.01))
        return round(self._vix, 2)

    def lot_size(self, symbol: str) -> int | None:
        return self.cfg.lot_size_fallback


# --------------------------------------------------------------------------
# Replay: historical 1-minute candles (backtesting)
# --------------------------------------------------------------------------
class ReplayBroker(_SimFillsMixin):
    """Replays 1-min candles second by second. Intra-candle path is O->L->H->C for
    green candles and O->H->L->C for red ones (the conservative-ish standard
    assumption). Fills use paper slippage. `series` maps a symbol (and "__spot__")
    to {epoch_minute_start: (o, h, l, c)}."""

    def __init__(self, cfg: Config, clock: SimClock, series: dict[str, dict[int, tuple]], vix: float, lot: int):
        self.cfg, self.clock, self.series, self._vix, self._lot = cfg, clock, series, vix, lot
        self._first = {k: min(v) for k, v in series.items() if v}
        self._last = {k: max(v) for k, v in series.items() if v}

    def _price(self, key: str) -> float:
        bars = self.series.get(key)
        if not bars:
            raise RuntimeError(f"no candles for {key}")
        t = self.clock.now().timestamp()
        m = int(t // 60 * 60)
        if m not in bars:
            if m < self._first[key]:
                return bars[self._first[key]][0]
            prev = [k for k in bars if k <= m]
            return bars[max(prev)][3] if prev else bars[self._last[key]][3]
        o, h, l, c = bars[m]
        path = (o, l, h, c) if c >= o else (o, h, l, c)
        f = (t - m) / 60 * 3
        i = min(int(f), 2)
        return path[i] + (path[i + 1] - path[i]) * (f - i)

    def ltps(self, symbols: list[str]) -> dict[str, float]:
        return {s: round_to_tick(self._price(s), self.cfg.tick_size) for s in symbols}

    def ltp(self, symbol: str) -> float:
        return self.ltps([symbol])[symbol]

    def spot(self) -> float | None:
        return round(self._price("__spot__"), 2) if self.series.get("__spot__") else None

    def vix(self) -> float:
        return self._vix

    def lot_size(self, symbol: str) -> int | None:
        return self._lot


# ==========================================================================
# STRATEGIES  (signal models - everything else: sizing, stops, exits, limits is shared)
# ==========================================================================
STRATEGIES = {
    "orb": "Opening-range breakout on the option premium + underlying confirmation, 20s hold, range-width filter, time stop",
    "momentum": "EMA(5)/EMA(13) crossover on 1-min underlying bars (index or futures), confirmed by price vs session TWAP",
    "supertrend": "Supertrend(ATR 7 x 2.5) flip on 1-min underlying bars (index or futures)",
    "ml": "Logistic-regression forecast of the underlying 5 min ahead (train with --train-ml); trades when P(up) >= 0.58 or <= 0.42",
    "orb_baseline": "Blueprint ORB with safety fixes only (control group)",
    "random": "CONTROL: random CE/PE entries (~1 per 10 bars) with the same exits - a real signal must beat this",
}
DEFAULT_STRATEGIES = "orb,momentum,supertrend,ml,random"


def preset(cfg: Config, name: str) -> Config:
    """Return the config for a named strategy. Shared risk settings are inherited unchanged."""
    if name not in STRATEGIES:
        raise ValueError(f"Unknown strategy {name!r}. Choose from: {', '.join(STRATEGIES)}")
    if name == "orb_baseline":
        return baseline(cfg)
    return replace(cfg, strategy_name=name, use_spot_confirm=True if name != "orb" else cfg.use_spot_confirm)


class BarBuilder:
    """Turns 1-second index prints into completed 1-minute OHLC bars."""

    def __init__(self) -> None:
        self.bars: list[list[float]] = []   # [minute_epoch, o, h, l, c]
        self.cur: list[float] | None = None
        self.twap_sum, self.twap_n = 0.0, 0

    def update(self, epoch: float, px: float) -> bool:
        """Feed a print; returns True when a bar just completed."""
        m = int(epoch // 60 * 60)
        self.twap_sum += px
        self.twap_n += 1
        if self.cur is None:
            self.cur = [m, px, px, px, px]
            return False
        if m != self.cur[0]:
            self.bars.append(self.cur)
            self.cur = [m, px, px, px, px]
            return True
        c = self.cur
        c[2], c[3], c[4] = max(c[2], px), min(c[3], px), px
        return False

    @property
    def twap(self) -> float:
        return self.twap_sum / self.twap_n if self.twap_n else 0.0


def ema_series(vals: list[float], n: int) -> list[float]:
    k, out = 2 / (n + 1), []
    for v in vals:
        out.append(v if not out else out[-1] + k * (v - out[-1]))
    return out


class MomentumSignal:
    """CE when EMA-fast crosses above EMA-slow and close > TWAP; PE on the mirror."""

    def __init__(self, cfg: Config):
        self.f, self.s = cfg.ema_fast, cfg.ema_slow

    def on_bar(self, bb: BarBuilder) -> tuple[str | None, str]:
        closes = [b[4] for b in bb.bars]
        if len(closes) < self.s + 1:
            return None, ""
        fast, slow = ema_series(closes, self.f), ema_series(closes, self.s)
        up = fast[-2] <= slow[-2] and fast[-1] > slow[-1] and closes[-1] > bb.twap
        dn = fast[-2] >= slow[-2] and fast[-1] < slow[-1] and closes[-1] < bb.twap
        why = f"EMA{self.f} {fast[-1]:,.1f} vs EMA{self.s} {slow[-1]:,.1f}, close {closes[-1]:,.1f} vs TWAP {bb.twap:,.1f}"
        return ("CE" if up else "PE" if dn else None), why


def supertrend(bars: list[list[float]], n: int, mult: float) -> list[int]:
    """Direction per bar: +1 uptrend, -1 downtrend, 0 warming up. Wilder ATR."""
    out, atr, fu, fl, d = [], None, None, None, 0
    for i, (_, o, h, l, c) in enumerate(bars):
        tr = h - l if i == 0 else max(h - l, abs(h - bars[i - 1][4]), abs(l - bars[i - 1][4]))
        atr = tr if atr is None else (atr * (n - 1) + tr) / n
        if i < n:
            out.append(0)
            continue
        mid = (h + l) / 2
        bu, bl = mid + mult * atr, mid - mult * atr
        prev_c = bars[i - 1][4]
        fu = bu if fu is None or bu < fu or prev_c > fu else fu
        fl = bl if fl is None or bl > fl or prev_c < fl else fl
        d = (1 if c > fu else -1 if c < fl else (d or (1 if c >= mid else -1)))
        out.append(d)
    return out


class SupertrendSignal:
    def __init__(self, cfg: Config):
        self.n, self.m = cfg.st_atr, cfg.st_mult

    def on_bar(self, bb: BarBuilder) -> tuple[str | None, str]:
        d = supertrend(bb.bars, self.n, self.m)
        if len(d) < 2 or d[-2] == 0:
            return None, ""
        flip = d[-1] != d[-2]
        return (("CE" if d[-1] > 0 else "PE") if flip else None), f"Supertrend {'up' if d[-1] > 0 else 'down'}{' (flip)' if flip else ''}"


# ---- ML: logistic regression, pure python (no sklearn needed) -----------
ML_FEATURES = ["r1", "r3", "r5", "or_pos", "twap_dist", "bar_range", "t"]


def ml_features(bars: list[list[float]], i: int, or_hi: float, or_lo: float, twap: float) -> list[float] | None:
    if i < 5:
        return None
    c = bars[i][4]
    rng = (or_hi - or_lo) or 1.0
    return [
        math.log(c / bars[i - 1][4]) * 1e3, math.log(c / bars[i - 3][4]) * 1e3, math.log(c / bars[i - 5][4]) * 1e3,
        (c - (or_hi + or_lo) / 2) / rng, (c - twap) / c * 1e3, (bars[i][2] - bars[i][3]) / c * 1e3, i / 45.0,
    ]


def day_samples(bars: list[list[float]], horizon: int) -> list[tuple[list[float], int]]:
    """(features, label) for one session; OR = first 15 bars, TWAP = running mean of closes."""
    out, s = [], 0.0
    for i in range(len(bars)):
        s += bars[i][4]
        or_b = bars[: min(i + 1, 15)]
        x = ml_features(bars, i, max(b[2] for b in or_b), min(b[3] for b in or_b), s / (i + 1))
        if x is not None and i + horizon < len(bars):
            out.append((x, 1 if bars[i + horizon][4] > bars[i][4] else 0))
    return out


def train_logreg(samples, epochs: int = 300, lr: float = 0.1, l2: float = 1e-3) -> dict:
    xs = [x for x, _ in samples]
    k = len(xs[0])
    mu = [sum(x[j] for x in xs) / len(xs) for j in range(k)]
    sd = [max(1e-9, math.sqrt(sum((x[j] - mu[j]) ** 2 for x in xs) / len(xs))) for j in range(k)]
    z = [[(x[j] - mu[j]) / sd[j] for j in range(k)] for x in xs]
    y = [t for _, t in samples]
    w, b = [0.0] * k, 0.0
    for _ in range(epochs):
        gw, gb = [0.0] * k, 0.0
        for zi, yi in zip(z, y):
            p = 1 / (1 + math.exp(-max(-30, min(30, b + sum(a * c for a, c in zip(w, zi))))))
            e = p - yi
            gb += e
            for j in range(k):
                gw[j] += e * zi[j]
        n = len(z)
        b -= lr * gb / n
        w = [w[j] - lr * (gw[j] / n + l2 * w[j]) for j in range(k)]
    return {"w": w, "b": b, "mu": mu, "sd": sd, "features": ML_FEATURES}


def predict(model: dict, x: list[float]) -> float:
    z = [(x[j] - model["mu"][j]) / model["sd"][j] for j in range(len(x))]
    s = model["b"] + sum(a * c for a, c in zip(model["w"], z))
    return 1 / (1 + math.exp(-max(-30, min(30, s))))


class MLSignal:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = json.loads(cfg.ml_model_file.read_text()) if cfg.ml_model_file.exists() else None
        self.refused = ""
        if self.model and self.model.get("underlying", cfg.underlying) != cfg.underlying:
            self.refused = f"model was trained on {self.model.get('underlying')} - retrain for {cfg.underlying}"
            self.model = None
        if self.model and cfg.mode in ("paper", "live") and self.model.get("trained_on") == "synthetic":
            self.refused = "model was trained on SYNTHETIC data - retrain on real history: python bot.py --train-ml"
            self.model = None

    def on_bar(self, bb: BarBuilder, or_hi: float, or_lo: float) -> tuple[str | None, str]:
        if not self.model:
            return None, self.refused or "no model - run: python bot.py --train-ml"
        x = ml_features(bb.bars, len(bb.bars) - 1, or_hi, or_lo, bb.twap)
        if x is None:
            return None, ""
        p = predict(self.model, x)
        sig = "CE" if p >= self.cfg.ml_up else "PE" if p <= self.cfg.ml_down else None
        return sig, f"P(up in {self.cfg.ml_horizon}m) = {p:.2f}"


class RandomSignal:
    """Null hypothesis: if a strategy can't beat random entries under identical risk management,
    its 'edge' is coming from the exits (or luck), not from the signal."""

    def __init__(self, cfg: Config):
        self.rng: random.Random | None = None

    def on_bar(self, bb: BarBuilder) -> tuple[str | None, str]:
        if self.rng is None:  # reproducible per session (same day => same random draws)
            day = datetime.fromtimestamp(bb.bars[0][0], IST).date() if bb.bars else date.today()
            self.rng = random.Random(day.toordinal() * 7919)
        if self.rng.random() < 0.10:
            return self.rng.choice(["CE", "PE"]), "random entry (control)"
        return None, ""


def make_signal(cfg: Config):
    return {"momentum": MomentumSignal, "supertrend": SupertrendSignal, "ml": MLSignal,
            "random": RandomSignal}.get(cfg.strategy_name, lambda c: None)(cfg)


# ==========================================================================
# ALERTS (optional Telegram: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)
# ==========================================================================
def notify(cfg: Config, text: str) -> None:
    """Fire-and-forget Telegram message. Never raises; never blocks trading for long."""
    if not (cfg.telegram_token and cfg.telegram_chat) or cfg.mode in ("sim", "backtest"):
        return
    import threading
    import urllib.parse
    import urllib.request

    def send() -> None:
        try:
            data = urllib.parse.urlencode({"chat_id": cfg.telegram_chat, "text": text[:4000]}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage", data, timeout=5)
        except Exception as e:  # alerts are best-effort
            log.warning("Telegram alert failed: %s", type(e).__name__)
    threading.Thread(target=send, daemon=True).start()


# ==========================================================================
# ENGINE
# ==========================================================================


@dataclass
class Trade:
    symbol: str
    lots: int
    qty: int
    entry_time: str
    entry_price: float
    initial_stop: float
    risk_pct: float
    risk_budget: float
    risk_actual: float
    exit_time: str = ""
    exit_price: float = 0.0
    exit_reason: str = ""
    peak: float = 0.0
    trough: float = 0.0
    final_stop: float = 0.0
    gross_pnl: float = 0.0
    charges: float = 0.0
    charges_detail: dict = field(default_factory=dict)
    net_pnl: float = 0.0
    r_multiple: float = 0.0


@dataclass
class SymbolTrack:
    symbol: str
    or_high: float = 0.0
    or_low: float = float("inf")
    or_ticks: int = 0
    traded: bool = False
    skip: str = ""                 # set when a filter disqualifies this contract for the day
    above_since: float = 0.0       # epoch when price first held above trigger (0 = not above)
    ticks: list = field(default_factory=list)  # [iso_time, ltp, stop_or_None]

    @property
    def side(self) -> str:
        return "PE" if self.symbol.upper().endswith("PE") else "CE"


@dataclass
class SessionResult:
    date: str
    mode: str
    strategy: str = ""
    status: str = "pending"
    spot: dict = field(default_factory=dict)
    expiry: str = ""
    dte: int = -1        # days to expiry (-1 = unknown)
    sentiment: dict = field(default_factory=dict)
    vix_open: float = 0.0
    vix_high: float = 0.0
    vix_low: float = 0.0
    lot_size: int = 0
    tracks: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    events: list = field(default_factory=list)
    errors: int = 0
    params: dict = field(default_factory=dict)

    @property
    def net_pnl(self) -> float:
        return round(sum(t["net_pnl"] for t in self.trades), 2)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["net_pnl"] = self.net_pnl
        return d


class Engine:
    def __init__(self, cfg: Config, broker: Broker, clock, sentiment: SentimentResult):
        self.cfg, self.b, self.clock, self.sent = cfg, broker, clock, sentiment
        self.r = SessionResult(date=clock.now().date().isoformat(), mode=cfg.mode, strategy=cfg.strategy_name)
        self.spot_hi, self.spot_lo, self.spot_px = 0.0, float("inf"), None
        self.spot_ok = cfg.use_spot_confirm
        self.entry_epoch = 0.0
        self.size_skips = 0
        self.signal = make_signal(cfg)      # None => ORB logic
        self.use_broker_stop = bool(cfg.broker_stop and cfg.mode == "live" and getattr(broker, "supports_broker_stop", False))
        self.stop_oid: str | None = None
        self.stop_sent, self.stop_sent_at, self.stop_polled_at = 0.0, 0.0, 0.0
        self.last_change, self.last_sig, self.stale = 0.0, None, False
        self.bars = BarBuilder()
        self.pending: tuple[str, str] | None = None
        self.r.sentiment = {"score": sentiment.score, "source": sentiment.source,
                            "headlines": sentiment.headlines}
        self.r.params = {k: str(v) for k, v in vars(cfg).items()
                         if k not in SECRET_FIELDS}
        self.position: Trade | None = None
        self.stop = 0.0
        self.realized = 0.0
        self.halted = False

    # ---------------------------------------------------------------- utils
    def _now(self) -> datetime:
        return self.clock.now()

    def _t(self) -> str:
        return self._now().strftime("%H:%M:%S")

    def event(self, level: str, msg: str) -> None:
        self.r.events.append([self._t(), level, msg])
        getattr(log, "warning" if level in ("warn", "risk") else "info")(f"[{self.cfg.strategy_name}] {msg}")
        if level in ("trade", "risk"):
            notify(self.cfg, f"[{self.cfg.strategy_name}] {msg}")

    def _vix(self) -> float:
        v = self.b.vix()
        self.r.vix_high = max(self.r.vix_high, v)
        self.r.vix_low = min(self.r.vix_low or v, v)
        return v

    # ---------------------------------------------------------------- main
    def run(self) -> SessionResult:
        cfg = self.cfg
        self._wait_until(cfg.market_open)
        if self._now().time() >= cfg.square_off:
            self.r.status = "skipped: started after session window"
            return self._finish({})

        symbols = self._select_contracts()
        tracks = {s: SymbolTrack(s) for s in symbols}
        exp = getattr(self.b, "expiry", None)
        if exp:
            self.r.expiry = str(exp)
            self.r.dte = (exp - self._now().date()).days
            self.event("info", f"Expiry {exp} ({self.r.dte} day(s) to expiry{' - EXPIRY DAY' if self.r.dte == 0 else ''})")
            if cfg.skip_expiry_day and self.r.dte == 0:
                self.r.status = "skipped: expiry day"
                return self._finish(tracks)
        if hasattr(self.b, "net_qty") and cfg.mode == "live":
            for s, tr in tracks.items():
                q = self.b.net_qty(s)
                if q:
                    tr.skip = f"existing position at broker ({q})"
                    self.event("risk", f"{s}: Groww already shows a net position of {q} - not trading this contract today")

        # --- pre-trade gates ---
        if self.signal is not None:
            self.event("info", f"Strategy '{cfg.strategy_name}': {STRATEGIES[cfg.strategy_name]}; entries "
                               f"{cfg.signal_start:%H:%M}-{cfg.entry_cutoff:%H:%M}, stop {cfg.signal_sl_pct:g}% of premium, "
                               f"time-stop={cfg.time_stop_min or 'off'}{'m' if cfg.time_stop_min else ''}")
        else:
            self.event("info", f"Strategy '{cfg.strategy_name}': spot-confirm={'on' if cfg.use_spot_confirm else 'off'}, "
                               f"hold={cfg.confirm_sec}s, OR width {cfg.or_min_width_pct:g}-{min(cfg.or_max_width_pct, 999):g}%, "
                               f"time-stop={cfg.time_stop_min or 'off'}{'m' if cfg.time_stop_min else ''}")
        self.event("info", f"Sentiment {self.sent.score:+.2f} ({self.sent.source})")
        if self.sent.score < cfg.sentiment_block_below:
            self.event("risk", "CIRCUIT BREAKER: sentiment below block threshold - no trading today")
            self.r.status = "blocked: negative sentiment"
            return self._finish(tracks)

        self.r.vix_open = self._vix() if cfg.use_vix else 0.0
        if cfg.use_vix:
            self.event("info", f"India VIX at open {self.r.vix_open:.2f} (x{vix_multiplier(cfg, self.r.vix_open):.2f} trail)")
        else:
            self.event("info", f"VIX not used for {cfg.underlying} ({cfg.exchange}) - trailing buffer x1.00")
        if cfg.use_vix and self.r.vix_open > cfg.max_vix_to_trade:
            self.event("risk", f"VIX {self.r.vix_open:.1f} above {cfg.max_vix_to_trade} - sitting out")
            self.r.status = "blocked: extreme VIX"
            return self._finish(tracks)

        lot = self.b.lot_size(symbols[0]) or cfg.lot_size_fallback
        self.r.lot_size = lot
        tk = getattr(self.b, "tick", None)
        if tk and tk != cfg.tick_size:
            self.cfg = cfg = replace(cfg, tick_size=tk)
        self.event("info", f"Lot size {lot}, tick ₹{cfg.tick_size:g}; tracking {', '.join(symbols)}")

        consecutive_errors = 0
        or_announced = False
        while self._now().time() < cfg.square_off:
            if cfg.kill_switch_file.exists():
                self.event("risk", "Kill-switch file detected - flattening and exiting")
                self._exit("kill switch")
                self.r.status = "halted: kill switch"
                break
            try:
                prices = self.b.ltps(symbols)  # one batched request per tick
                vix = self._vix() if (self.position and cfg.use_vix) else 0.0
                consecutive_errors = 0
            except Exception as e:
                consecutive_errors += 1
                self.r.errors += 1
                self.event("warn", f"Data error #{consecutive_errors}: {e}")
                if consecutive_errors >= 10 and self.position:
                    self._exit("data feed failure")
                    self.halted = True
                self.clock.sleep(min(5.0, cfg.poll_interval_sec * consecutive_errors))
                continue
            self._update_spot()
            self._stale_check(prices)
            if self.signal is not None and self.spot_px is not None \
                    and self.bars.update(self._now().timestamp(), self.spot_px):
                self._on_bar()

            now_t = self._now().time()
            in_or = now_t < cfg.opening_range_end
            for s, px in prices.items():
                tr = tracks[s]
                stop = self.stop if self.position and self.position.symbol == s else None
                tr.ticks.append([self._t(), px, stop])
                if in_or:
                    tr.or_high = max(tr.or_high, px)
                    tr.or_low = min(tr.or_low, px)
                    tr.or_ticks += 1

            if not or_announced and not in_or:
                or_announced = True
                self._close_opening_range(tracks)

            if self.position:
                self._manage(prices[self.position.symbol], vix)
            elif (not self.halted and not self.stale and now_t < cfg.entry_cutoff
                  and len(self.r.trades) < cfg.max_trades_per_day):
                if self.signal is None:
                    if not in_or:
                        for s, px in prices.items():
                            if self._entry_signal(tracks[s], px):
                                break
                elif self.pending:
                    side, why = self.pending
                    self.pending = None
                    tr = next((t for t in tracks.values() if t.side == side and not t.traded), None)
                    if tr:
                        px = prices[tr.symbol]
                        self._enter(tr, px, px, sl=round_to_tick(px * (1 - cfg.signal_sl_pct / 100), cfg.tick_size), why=why)
            elif self.halted:
                break  # nothing more to do today

            self.clock.sleep(cfg.poll_interval_sec)

        if self.position:
            self._exit("time square-off 10:00")
        if self.r.status == "pending":
            self.r.status = "traded" if self.r.trades else (
                "filtered: no valid setup" if all(t.skip for t in tracks.values())
                else "skipped: stop too wide for 1 lot" if self.size_skips
                else "no signal" if self.signal is not None else "no breakout")
        return self._finish(tracks)

    # ---------------------------------------------------------------- filters
    def _select_contracts(self) -> list[str]:
        cfg = self.cfg
        if cfg.auto_atm and hasattr(self.b, "atm_contracts"):
            try:
                pair = self.b.atm_contracts()
                if pair:
                    self.event("info", f"Auto-ATM selected {pair[0]} / {pair[1]}")
                    return list(pair)
            except Exception as e:
                self.event("warn", f"Auto-ATM failed ({e}); using configured symbols")
        return [s for s in (cfg.ce_symbol, cfg.pe_symbol) if s]

    def _update_spot(self) -> None:
        if not self.spot_ok:
            return
        try:
            px = self.b.spot()
        except Exception as e:
            px = None
            self.event("warn", f"Spot fetch failed: {e}")
        if px is None:
            if self.spot_px is None:  # never got a single spot print => disable filter, say so
                self.spot_ok = False
                self.event("warn", "Spot data unavailable - spot confirmation disabled today")
            return
        self.spot_px = px
        if self._now().time() < self.cfg.opening_range_end:
            self.spot_hi, self.spot_lo = max(self.spot_hi, px), min(self.spot_lo, px)

    def _on_bar(self) -> None:
        """A 1-min index bar just closed: ask the signal model for a direction."""
        cfg, now_t = self.cfg, self._now().time()
        if self.position or now_t < cfg.signal_start or now_t >= cfg.entry_cutoff:
            return
        if isinstance(self.signal, MLSignal):
            hi, lo = (self.spot_hi, self.spot_lo) if self.spot_lo != float("inf") else (0.0, 0.0)
            sig, why = self.signal.on_bar(self.bars, hi, lo)
        else:
            sig, why = self.signal.on_bar(self.bars)
        if sig:
            self.pending = (sig, why)
            self.event("info", f"SIGNAL {sig}: {why}")
        elif ("model" in why and "P(up" not in why) and len(self.bars.bars) == 6:
            self.event("warn", f"ML strategy idle: {why}")

    def _close_opening_range(self, tracks: dict[str, "SymbolTrack"]) -> None:
        cfg = self.cfg
        if self.spot_ok and self.spot_px is not None:
            self.r.spot = {"or_high": self.spot_hi, "or_low": self.spot_lo}
            self.event("info", f"Spot opening range {self.spot_lo:,.2f} - {self.spot_hi:,.2f}")
        for tr in tracks.values():
            width = (tr.or_high - tr.or_low) / tr.or_high * 100 if tr.or_high else 0.0
            msg = f"{tr.symbol} opening range {tr.or_low:.2f} - {tr.or_high:.2f} ({width:.1f}% wide, {tr.or_ticks} ticks)"
            if self.signal is not None:
                pass  # range filters are ORB-specific; signal models decide on index bars
            elif tr.or_ticks < cfg.min_or_ticks:
                tr.skip = "too few ticks"
            elif width < cfg.or_min_width_pct:
                tr.skip = f"range too narrow ({width:.1f}% < {cfg.or_min_width_pct:g}%)"
            elif width > cfg.or_max_width_pct:
                tr.skip = f"range too wide ({width:.1f}% > {cfg.or_max_width_pct:g}%)"
            self.event("risk" if tr.skip else "info", msg + (f" - SKIP: {tr.skip}" if tr.skip else ""))

    def _entry_signal(self, tr: "SymbolTrack", px: float) -> bool:
        cfg = self.cfg
        if tr.traded or tr.skip:
            return False
        trigger = tr.or_high * (1 + cfg.breakout_buffer_pct / 100.0)
        now = self._now().timestamp()
        if px < trigger:
            tr.above_since = 0.0
            return False
        if not tr.above_since:
            tr.above_since = now
        if now - tr.above_since < cfg.confirm_sec:
            return False  # still proving the breakout holds
        if self.spot_ok and self.spot_px is not None:
            ok = self.spot_px > self.spot_hi if tr.side == "CE" else self.spot_px < self.spot_lo
            if not ok:
                return False  # premium broke out but the index didn't confirm - keep waiting
        self._enter(tr, px, trigger)
        return True

    # ---------------------------------------------------------------- steps
    def _wait_until(self, t) -> None:
        while self._now().time() < t:
            self.clock.sleep(min(30.0, max(0.2, (datetime.combine(self._now().date(), t, self._now().tzinfo)
                                                 - self._now()).total_seconds())))

    def _enter(self, tr: SymbolTrack, px: float, trigger: float, sl: float | None = None, why: str = "") -> None:
        cfg = self.cfg
        sl = initial_stop(cfg, px, tr.or_low) if sl is None else sl
        size = size_position(cfg, self.sent.score, px, sl, self.r.lot_size)
        tr.traded = True  # one attempt per contract per day
        if size.lots == 0:
            self.size_skips += 1
            self.event("risk", f"{tr.symbol} entry @ {px:.2f} skipped: {size.reason}")
            return
        head = f"SIGNAL ENTRY {tr.symbol} @ {px:.2f} ({why}); " if why else f"BREAKOUT {tr.symbol} {px:.2f} > trigger {trigger:.2f}; "
        self.event("trade", head +
                            f"buying {size.lots} lot(s) = {size.qty} units, SL {sl:.2f}, "
                            f"risk ₹{size.rupee_risk_actual:,.0f} of ₹{size.rupee_risk_budget:,.0f} budget ({size.reason})")
        fill = self.b.buy(tr.symbol, size.qty, px)
        if not fill or fill.qty == 0:
            self.event("warn", f"Entry not filled for {tr.symbol} - breakout abandoned")
            return
        # Slippage widens risk (fill above signal price). Tighten the stop if needed so
        # actual rupee risk never exceeds the HARD ceiling (max_risk_pct of capital).
        ceiling = cfg.total_capital * cfg.max_risk_pct / 100.0
        if fill.qty * (fill.avg_price - sl) > ceiling:
            sl = round_to_tick(fill.avg_price - ceiling / fill.qty + cfg.tick_size, cfg.tick_size)
            self.event("risk", f"Slippage: stop tightened to {sl:.2f} to stay within ₹{ceiling:,.0f} risk ceiling")
        self.entry_epoch = self._now().timestamp()
        self.position = Trade(
            symbol=tr.symbol, lots=fill.qty // self.r.lot_size, qty=fill.qty, entry_time=self._t(),
            entry_price=fill.avg_price, initial_stop=sl, risk_pct=round(size.risk_pct, 3),
            risk_budget=round(size.rupee_risk_budget, 2),
            risk_actual=round(fill.qty * (fill.avg_price - sl), 2),
            peak=fill.avg_price, trough=fill.avg_price,
        )
        self.stop = sl
        self.event("trade", f"FILLED BUY {fill.qty} @ {fill.avg_price:.2f} (order {fill.order_id}"
                            f"{', ' + fill.note if fill.note else ''})")
        if self.use_broker_stop:
            try:
                self.stop_oid = self.b.place_stop(tr.symbol, fill.qty, sl)
                self.stop_sent, self.stop_sent_at = sl, self._now().timestamp()
                self.event("info", f"Protective SL-M placed at Groww: trigger {sl:.2f} (order {self.stop_oid})")
            except Exception as e:  # never hold an unprotected position
                self.event("risk", f"Could not place protective stop ({e}) - exiting immediately")
                self._exit("protective stop rejected")

    def _manage(self, px: float, vix: float) -> None:
        p, cfg = self.position, self.cfg
        if self.stop_oid and self._broker_stop_filled():
            return
        p.peak, p.trough = max(p.peak, px), min(p.trough, px)
        r_unit = p.entry_price - p.initial_stop
        new_stop = next_stop(cfg, self.stop, p.entry_price, p.peak, vix, r_unit)
        if new_stop > self.stop:
            if self.stop < p.entry_price <= new_stop:
                self.event("info", f"Stop moved to breakeven/profit {new_stop:.2f}")
            self.stop = new_stop
        self._sync_broker_stop(px)

        unreal = (px - p.entry_price) * p.qty
        if self.realized + unreal <= -cfg.daily_loss_limit:
            self.event("risk", f"DAILY LOSS LIMIT hit (₹{self.realized + unreal:,.0f}) - flattening, done for today")
            self._exit("daily loss limit")
            self.halted = True
        elif px <= self.stop:
            reason = "initial stop" if self.stop <= p.initial_stop else "trailing stop"
            self._exit(reason, ref=px)
        elif (cfg.time_stop_min and self._now().timestamp() - self.entry_epoch >= cfg.time_stop_min * 60
              and px - p.entry_price < cfg.time_stop_min_r * r_unit):
            self._exit(f"time stop ({cfg.time_stop_min}m, no follow-through)", ref=px)
        elif cfg.daily_profit_lock and self.realized + unreal >= cfg.daily_profit_lock:
            self._exit("daily profit lock")
            self.halted = True

    def _sync_broker_stop(self, px: float) -> None:
        """Trail the Groww SL-M: only on meaningful moves, rate-limited, never above market."""
        if not self.stop_oid:
            return
        cfg, now = self.cfg, self._now().timestamp()
        if (self.stop - self.stop_sent < cfg.stop_update_min_ticks * cfg.tick_size
                or now - self.stop_sent_at < cfg.stop_update_min_sec or self.stop >= px):
            return
        try:
            self.b.modify_stop(self.stop_oid, self.position.qty, self.stop)
            self.stop_sent, self.stop_sent_at = self.stop, now
        except Exception as e:  # old stop is still live at Groww - still protected
            self.event("warn", f"Could not trail broker stop to {self.stop:.2f}: {e}")
            self.stop_sent_at = now

    def _broker_stop_filled(self) -> bool:
        """Poll the Groww SL-M; if the exchange already triggered it, book the exit from that fill."""
        now = self._now().timestamp()
        if now - self.stop_polled_at < self.cfg.stop_poll_sec:
            return False
        self.stop_polled_at = now
        try:
            status, filled, avg = self.b.stop_state(self.stop_oid)
        except Exception as e:
            self.event("warn", f"Stop status check failed: {e}")
            return False
        if status in ("EXECUTED", "COMPLETED") and filled >= self.position.qty:
            self.event("risk", f"Groww protective stop executed @ {avg:.2f}")
            # Hand the KNOWN fill to _exit: no cancel, no sell - the position is already closed.
            self._exit("broker stop-loss", stop_fill=Fill(self.stop_oid, "SELL", filled, avg, 1, "broker SL-M"))
            return True
        if status in ("REJECTED", "FAILED", "CANCELLED"):
            self.event("risk", f"Protective stop became {status} - exiting to avoid an unprotected position")
            self.stop_oid = None
            self._exit("protective stop lost")
            return True
        return False

    def _stale_check(self, prices: dict[str, float]) -> None:
        sig = (tuple(prices.values()), self.spot_px)
        now = self._now().timestamp()
        if sig != self.last_sig or not self.last_change:
            self.last_sig, self.last_change = sig, now
            if self.stale:
                self.stale = False
                self.event("info", "Prices moving again - entries re-enabled")
        elif not self.stale and now - self.last_change >= self.cfg.stale_sec:
            self.stale = True
            self.event("warn", f"No price change for {self.cfg.stale_sec:.0f}s - feed may be stale; new entries blocked")

    def _exit(self, reason: str, ref: float | None = None, stop_fill: Fill | None = None) -> None:
        p = self.position
        if not p:
            return
        fill = stop_fill
        if fill is not None:
            self.stop_oid = None
        ref = ref if ref is not None else (fill.avg_price if fill else self.b.ltp(p.symbol))
        if fill is None and self.stop_oid:  # cancel the broker stop FIRST; if it already filled, that IS the exit
            oid, self.stop_oid = self.stop_oid, None
            try:
                _, filled, avg = self.b.cancel_stop(oid)
            except Exception as e:
                self.event("risk", f"Cancel of protective stop {oid} failed ({e}) - checking position before selling")
                filled, avg = 0, 0.0
                net = self.b.net_qty(p.symbol) if hasattr(self.b, "net_qty") else None
                if net is not None and net <= 0:
                    filled, avg, reason = p.qty, ref, reason + " (filled by broker stop)"
            if filled >= p.qty:
                fill = Fill(oid, "SELL", filled, avg or ref, 1, "broker SL-M")
                reason = "broker stop-loss" if "broker" not in reason else reason
            elif filled:  # partial SL fill: sell only the remainder
                rest = self.b.sell(p.symbol, p.qty - filled, ref)
                tot = filled + rest.qty
                fill = Fill(rest.order_id, "SELL", tot, (filled * (avg or ref) + rest.qty * rest.avg_price) / tot, 1, "SL-M + exit")
        if fill is None:
            fill = self.b.sell(p.symbol, p.qty, ref)
        ch = estimate_charges(self.cfg, p.entry_price * p.qty, fill.avg_price * fill.qty)
        p.exit_time, p.exit_price, p.exit_reason = self._t(), fill.avg_price, reason
        p.final_stop = self.stop
        p.gross_pnl = round((fill.avg_price - p.entry_price) * p.qty, 2)
        p.charges, p.charges_detail = round(ch.total, 2), asdict(ch)
        p.net_pnl = round(p.gross_pnl - p.charges, 2)
        r_unit = (p.entry_price - p.initial_stop) * p.qty
        p.r_multiple = round(p.gross_pnl / r_unit, 2) if r_unit else 0.0
        self.realized += p.net_pnl
        self.r.trades.append(asdict(p))
        self.event("trade", f"EXIT {p.symbol} {p.qty} @ {fill.avg_price:.2f} [{reason}] "
                            f"gross ₹{p.gross_pnl:,.0f}, charges ₹{p.charges:,.0f}, net ₹{p.net_pnl:,.0f} "
                            f"({p.r_multiple:+.2f}R)")
        self.position = None
        self.stop = 0.0

    def _finish(self, tracks: dict[str, SymbolTrack]) -> SessionResult:
        if hasattr(self.b, "net_qty") and self.cfg.mode == "live":
            for t in self.r.trades:
                q = self.b.net_qty(t["symbol"])
                if q:
                    self.event("risk", f"RECONCILIATION: Groww still shows {q} of {t['symbol']} - CLOSE MANUALLY")
                elif q == 0:
                    self.event("info", f"Reconciled: flat in {t['symbol']} at Groww")
        notify(self.cfg, f"{self.cfg.strategy_name} {self.r.date}: {self.r.status}, net ₹{self.r.net_pnl:,.0f} "
                         f"({len(self.r.trades)} trade(s))")
        for tr in tracks.values():
            d = asdict(tr)
            if d["or_low"] == float("inf"):
                d["or_low"] = 0.0
            self.r.tracks.append(d)
        self.event("info", f"Session closed: {self.r.status}; net P&L ₹{self.r.net_pnl:,.2f}")
        return self.r

# ==========================================================================
# BACKTEST
# ==========================================================================
CACHE = Path("data/cache")
NEUTRAL = SentimentResult(0.0, "backtest - no historical headlines (neutral)")


def trading_days(cfg: Config, start: date, end: date) -> list[date]:
    hol = set()
    if cfg.holidays_file.exists():
        hol = {ln.strip() for ln in cfg.holidays_file.read_text().splitlines() if ln.strip() and not ln.startswith("#")}
    out, d = [], start
    while d <= end:
        if d.weekday() < 5 and d.isoformat() not in hol:
            out.append(d)
        d += timedelta(days=1)
    return out


def session_start(d: date) -> SimClock:
    return SimClock(datetime.combine(d, datetime.min.time(), IST).replace(hour=9, minute=14, second=50))


# ------------------------------------------------------------------ real data
def _candles(resp) -> list[list]:
    rows = resp.get("candles", []) if isinstance(resp, dict) else (resp or [])
    out = []
    for r in rows:
        ts = r[0]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts = ts if ts.tzinfo else ts.replace(tzinfo=IST)
            ts = ts.timestamp()
        out.append([int(ts), float(r[1]), float(r[2]), float(r[3]), float(r[4])])
    return out


class GrowwHistory:
    def __init__(self, cfg: Config):
        from growwapi import GrowwAPI

        if cfg.totp_secret:
            import pyotp
            token = GrowwAPI.get_access_token(api_key=cfg.api_key, totp=pyotp.TOTP(cfg.totp_secret).now())
        else:
            token = GrowwAPI.get_access_token(api_key=cfg.api_key, secret=cfg.api_secret)
        self.g, self.cfg = GrowwAPI(token), cfg
        self.interval = getattr(self.g, "CANDLE_INTERVAL_MIN_1", None)
        if self.interval is None:
            raise RuntimeError("SDK has no CANDLE_INTERVAL_MIN_1 - check the Backtesting docs for the 1-min constant")

    def _hist(self, segment, groww_symbol: str, d: date, interval=None) -> list[list]:
        _time.sleep(0.25)  # stay polite with rate limits
        resp = self.g.get_historical_candles(
            exchange=self.cfg.exchange, segment=segment, groww_symbol=groww_symbol,
            start_time=f"{d} 09:15:00", end_time=f"{d} 10:00:00", candle_interval=interval or self.interval)
        return _candles(resp)

    def day(self, d: date) -> dict | None:

        path = CACHE / f"{self.cfg.exchange}-{self.cfg.underlying}-{d}.json"
        if path.exists():
            return json.loads(path.read_text())
        g, cfg = self.g, self.cfg
        spot = self._hist(getattr(g, f"SEGMENT_{cfg.spot_segment}"), f"{cfg.exchange}-{cfg.spot_symbol}", d)
        if not spot:
            log.warning("%s: no index candles (holiday?) - skipped", d)
            return None
        ce, pe, expiry, strike = atm_pair(g, cfg.exchange, cfg.underlying, d, spot[0][1], cfg.strike_step)
        data = {"date": d.isoformat(), "expiry": expiry.isoformat(), "strike": strike, "spot": spot,
                "ce": {"symbol": ce, "candles": self._hist(getattr(g, f"SEGMENT_{cfg.segment}"), ce, d)},
                "pe": {"symbol": pe, "candles": self._hist(getattr(g, f"SEGMENT_{cfg.segment}"), pe, d)},
                "vix": None}
        try:  # VIX open, if the index is available under this groww_symbol
            v = _candles(g.get_historical_candles(exchange=g.EXCHANGE_NSE, segment=g.SEGMENT_CASH,
                                                  groww_symbol=f"NSE-{cfg.vix_symbol}",
                                                  start_time=f"{d} 09:15:00", end_time=f"{d} 09:20:00",
                                                  candle_interval=self.interval))
            data["vix"] = v[0][1] if v else None
        except Exception as e:
            log.info("VIX history unavailable (%s)", e)
        CACHE.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
        return data


def replay_day(cfg: Config, data: dict) -> dict:
    d = date.fromisoformat(data["date"])
    as_bars = lambda rows: {r[0] // 60 * 60: tuple(r[1:5]) for r in rows}  # noqa: E731
    series = {"__spot__": as_bars(data["spot"]),
              data["ce"]["symbol"]: as_bars(data["ce"]["candles"]),
              data["pe"]["symbol"]: as_bars(data["pe"]["candles"])}
    c = replace(cfg, ce_symbol=data["ce"]["symbol"], pe_symbol=data["pe"]["symbol"], auto_atm=False,
                use_spot_confirm=cfg.use_spot_confirm and bool(data["spot"]))
    clock = session_start(d)
    broker = ReplayBroker(c, clock, series, data.get("vix") or cfg.vix_fallback, cfg.lot_size_fallback)
    broker.expiry = date.fromisoformat(data["expiry"]) if data.get("expiry") else None
    res = Engine(c, broker, clock, NEUTRAL).run().to_dict()
    res["events"].insert(0, ["09:15:00", "info", f"Backtest: ATM {data['strike']} expiry {data['expiry']}; "
                                                 f"VIX {'from history' if data.get('vix') else 'fallback'}"])
    return res


# ------------------------------------------------------------------ driver
def run_variants(variants: list[Config], days: list[date], loader) -> dict[str, list[dict]]:
    results: dict[str, list[dict]] = {v.strategy_name: [] for v in variants}
    for d in days:
        data = loader(d)
        if data is None:
            continue
        line = [str(d)]
        for v in variants:
            res = data(v) if callable(data) else replay_day(v, data)
            results[v.strategy_name].append(res)
            line.append(f"{v.strategy_name}: {report.inr(res['net_pnl'], True):>8}")
        print("  ".join(line))
    return results


def synthetic_loader(d: date):
    """Same seed per day => every strategy sees the identical synthetic price path."""
    def run_variant(v: Config) -> dict:
        clock = session_start(d)
        return Engine(v, SimBroker(v, clock, int(d.strftime("%Y%m%d"))), clock, NEUTRAL).run().to_dict()
    return run_variant


def backtest_main(a, base: Config, names: list[str]) -> int:
    base = replace(base, mode="backtest", kill_switch_file=Path("/nonexistent/STOP"))
    variants = [preset(base, n) for n in names]
    if a.synthetic:
        start = date.fromisoformat(a.start) if a.start else date(2026, 8, 3)
        days = trading_days(base, start, start + timedelta(days=a.days * 2))[: a.days]
        loader, title = synthetic_loader, "SYNTHETIC data - pipeline check only"
    else:
        base.validate()
        if not (a.start and a.end):
            raise SystemExit("--from and --to are required for a real backtest (or add --synthetic)")
        days = trading_days(base, date.fromisoformat(a.start), date.fromisoformat(a.end))
        loader, title = GrowwHistory(base).day, f"Groww 1-min candles {a.start} to {a.end}"
    results = run_variants(variants, days, loader)
    publish_all(base, variants, results, Path("reports/backtest"), title, synthetic=a.synthetic, fresh=True)
    return 0


def publish_all(base: Config, variants: list[Config], results: dict[str, list[dict]], out: Path,
                title: str, synthetic: bool = False, fresh: bool = False) -> Path:
    """Per-strategy daily + month reports, then the leaderboard across all strategies."""
    all_sessions = {}
    for v in variants:
        c = replace(v, reports_dir=out / v.strategy_name, journal_file=out / v.strategy_name / "journal.jsonl")
        if fresh and c.journal_file.exists():
            c.journal_file.unlink()
        for s in results.get(v.strategy_name, []):
            report.publish(s, c)
        all_sessions[v.strategy_name] = report.load_journal(c)
    path = out / "compare.html"
    path.write_text(report.render_compare(base, all_sessions, title, synthetic=synthetic, descriptions=STRATEGIES),
                    encoding="utf-8")
    return path


# ---- ML training --------------------------------------------------------
def synthetic_bars(d: date) -> list[list[float]]:
    clock = session_start(d)
    sb, bb = SimBroker(Config(mode="sim"), clock, int(d.strftime("%Y%m%d"))), BarBuilder()
    clock.sleep(10)
    while clock.now().time() < time(10, 0):
        bb.update(clock.now().timestamp(), sb.spot())
        clock.sleep(1)
    return bb.bars


def train_ml_main(a, cfg: Config) -> int:
    """Chronological split: train on the older 70% of days, report accuracy on the newest 30%."""
    if a.synthetic:
        start = date.fromisoformat(a.start) if a.start else date(2026, 1, 5)
        days = trading_days(cfg, start, start + timedelta(days=400))[:250]
        sessions = [(d, synthetic_bars(d)) for d in days]
        source = "synthetic"
    else:
        files = sorted(CACHE.glob(f"{cfg.exchange}-{cfg.underlying}-*.json"))
        sessions = []
        for f in files:
            dd = json.loads(f.read_text())
            if a.end and dd["date"] > a.end:
                continue  # --to: keep later days unseen for an honest backtest
            sessions.append((date.fromisoformat(dd["date"]), dd["spot"]))
        source = f"{len(files)} cached Groww day(s) in {CACHE}"
        if len(sessions) < 30:
            raise SystemExit(f"Need >= 30 cached days to train, found {len(sessions)}. Download history first:\n"
                             f"  python bot.py --backtest --from YYYY-MM-DD --to YYYY-MM-DD --strategies orb")
    cut = int(len(sessions) * 0.7)
    tr = [s for _, b in sessions[:cut] for s in day_samples(b, cfg.ml_horizon)]
    te = [s for _, b in sessions[cut:] for s in day_samples(b, cfg.ml_horizon)]
    if not tr or not te:
        raise SystemExit("Not enough bars to build training samples.")
    model = train_logreg(tr)

    def acc(samples, lo=0.5, hi=0.5):
        picks = [(predict(model, x), y) for x, y in samples]
        conf = [(p, y) for p, y in picks if p >= hi or p <= lo]
        hit = sum((p >= 0.5) == (y == 1) for p, y in conf)
        return (hit / len(conf) if conf else 0.0), len(conf)

    base_rate = max(sum(y for _, y in te), len(te) - sum(y for _, y in te)) / len(te)
    a_all, _ = acc(te)
    a_conf, n_conf = acc(te, cfg.ml_down, cfg.ml_up)
    model.update({"underlying": cfg.underlying, "trained_on": source, "train_days": cut, "test_days": len(sessions) - cut,
                  "test_accuracy": round(a_all, 4), "test_accuracy_when_trading": round(a_conf, 4),
                  "test_trade_signals": n_conf, "test_base_rate": round(base_rate, 4),
                  "last_train_day": sessions[cut - 1][0].isoformat(), "horizon_min": cfg.ml_horizon,
                  "created": datetime.now(IST).isoformat(timespec="seconds")})
    cfg.ml_model_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.ml_model_file.write_text(json.dumps(model, indent=1))
    print(f"Trained on {cut} days ({len(tr)} samples), tested on {len(sessions) - cut} unseen days ({len(te)} samples)")
    print(f"  accuracy, all bars          : {a_all:.1%}   (always-guess-majority: {base_rate:.1%})")
    print(f"  accuracy when model trades  : {a_conf:.1%}   on {n_conf} signals")
    print(f"  saved -> {cfg.ml_model_file}")
    # Overlapping 5-min-ahead labels => neighbouring samples are not independent. The split is by day
    # (no leakage), but the effective sample size is ~ samples / horizon, so intervals are wider.
    n_eff = max(1, n_conf // max(1, cfg.ml_horizon))
    half = 1.96 * math.sqrt(max(a_conf * (1 - a_conf), 1e-9) / n_eff)
    print(f"  95% interval (effective n={n_eff}): {a_conf - half:.1%} to {a_conf + half:.1%}")
    model.update({"test_accuracy_ci": [round(a_conf - half, 4), round(a_conf + half, 4)], "effective_n": n_eff})
    cfg.ml_model_file.write_text(json.dumps(model, indent=1))
    real_edge = (a_conf - half) > base_rate and n_eff >= 50
    print("  VERDICT:", "edge above the majority-guess baseline even at the low end of the interval - backtest it on "
          "LATER unseen days and judge by avg R after costs (accuracy alone doesn't pay)" if real_edge
          else "NO reliable edge (interval overlaps the majority-guess baseline) - do not trust its trades")
    return 0


# ==========================================================================
# CLI
# ==========================================================================
def setup_logging(cfg: Config, day: date, quiet: bool = False) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)s: %(message)s")
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    fh = logging.FileHandler(cfg.log_dir / f"{day.isoformat()}.log", encoding="utf-8")
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.WARNING if quiet else logging.INFO)
    for h in (fh, sh):
        h.setFormatter(fmt)
        root.addHandler(h)


def is_trading_day(cfg: Config, d: date) -> bool:
    return bool(trading_days(cfg, d, d))


class SharedFeed:
    """One market-data fetch per second shared by every strategy thread (paper mode).
    Keeps total Live Data usage ~120 req/min no matter how many strategies run."""

    def __init__(self, inner, ttl: float = 0.9, clock=None):
        import threading
        self.inner, self.ttl, self.lock = inner, ttl, threading.Lock()
        self.clock = clock or RealClock()  # TTL measured on the SAME clock the engines use
        self._c: dict = {}

    def _cached(self, key, fn):
        with self.lock:
            hit = self._c.get(key)
            now = self.clock.now().timestamp()
            if hit and (key[0] in ("atm", "lot") or now - hit[0] < self.ttl):
                return hit[1]
            val = fn()
            self._c[key] = (now, val)
            return val

    def ltps(self, symbols):
        return dict(self._cached(("ltps", tuple(symbols)), lambda: self.inner.ltps(symbols)))

    def ltp(self, symbol):
        return self.ltps([symbol])[symbol]

    def spot(self):
        return self._cached(("spot",), self.inner.spot)

    def vix(self):
        return self._cached(("vix",), self.inner.vix)

    def atm_contracts(self):
        return self._cached(("atm",), self.inner.atm_contracts)

    def lot_size(self, symbol):
        return self._cached(("lot", symbol), lambda: self.inner.lot_size(symbol))

    @property
    def expiry(self):
        return getattr(self.inner, "expiry", None)

    @property
    def tick(self):
        return getattr(self.inner, "tick", None)

    def buy(self, *a):
        return self.inner.buy(*a)

    def sell(self, *a):
        return self.inner.sell(*a)


def run_session(cfg: Config, names: list[str]) -> int:
    """Today's session. paper: all strategies in parallel threads. live: exactly one strategy."""
    import threading

    cfg.validate()
    clock = RealClock()
    today = clock.now().date()
    setup_logging(cfg, today)
    if not is_trading_day(cfg, today):
        log.info("Not a trading day (%s) - exiting.", today)
        return 0
    if cfg.mode == "live" and len(names) != 1:
        raise SystemExit("Live mode trades ONE strategy (one account, one margin). Use --strategies <name>.")
    sent = morning_sentiment(cfg.headlines_file, cfg.use_finbert)
    broker = GrowwBroker(cfg, clock) if cfg.mode == "live" else SharedFeed(PaperBroker(cfg, clock), clock=clock)
    variants = [preset(cfg, n) for n in names]
    results: dict[str, list[dict]] = {}

    def worker(v: Config) -> None:
        eng = Engine(v, broker, clock, sent)
        try:
            res = eng.run()
        except Exception as e:
            log.error("[%s] FATAL: %s\n%s", v.strategy_name, e, traceback.format_exc())
            if eng.position:
                try:
                    eng._exit("emergency flatten after crash")
                except Exception as e2:
                    log.critical("[%s] EMERGENCY EXIT FAILED - CLOSE POSITION MANUALLY IN GROWW: %s", v.strategy_name, e2)
            res = eng.r
            res.status = f"error: {e}"[:120]
            res.events.append([clock.now().strftime("%H:%M:%S"), "warn", f"Fatal error: {e}"])
        results[v.strategy_name] = [res.to_dict()]

    threads = [threading.Thread(target=worker, args=(v,), name=v.strategy_name) for v in variants]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    path = publish_all(cfg, variants, results, cfg.reports_dir / cfg.mode,
                       f"{cfg.mode.upper()} sessions - live Groww data")
    for n in names:
        r = results[n][0]
        log.info("%-12s %-32s net %s", n, r["status"], report.inr(r["net_pnl"], True))
    log.info("Leaderboard: %s", path.resolve())
    return 1 if any(results[n][0]["status"].startswith("error") for n in names) else 0


def run_simulation(cfg: Config, days: int, start: date, names: list[str]) -> None:
    cfg = replace(cfg, mode="sim", kill_switch_file=Path("/nonexistent/STOP"))
    setup_logging(cfg, date.today(), quiet=True)
    variants = [preset(cfg, n) for n in names]
    dd = trading_days(cfg, start, start + timedelta(days=days * 2))[:days]
    results = run_variants(variants, dd, synthetic_loader)
    path = publish_all(cfg, variants, results, Path("reports/sim"), "SYNTHETIC data - pipeline check only",
                       synthetic=True, fresh=True)
    print(f"\nLeaderboard: {path.resolve()}")


def rebuild(cfg: Config) -> None:
    for mode_dir in sorted(p for p in cfg.reports_dir.iterdir() if p.is_dir()) if cfg.reports_dir.exists() else []:
        variants, results = [], {}
        for sdir in sorted(p for p in mode_dir.iterdir() if p.is_dir() and p.name in STRATEGIES):
            v = preset(cfg, sdir.name)
            variants.append(v)
            results[v.strategy_name] = report.load_journal(replace(v, journal_file=sdir / "journal.jsonl"))
        if variants:
            publish_all(cfg, variants, results, mode_dir, f"{mode_dir.name} (rebuilt)",
                        synthetic=mode_dir.name == "sim", fresh=True)
            print(f"rebuilt {mode_dir}")


# ==========================================================================
# INTERACTIVE WIZARD  (python bot.py in a terminal; cron/non-TTY runs skip it)
# ==========================================================================
ENV_FILE = Path(".env")
SECRET_KEYS = ("GROWW_API_KEY", "GROWW_API_SECRET", "GROWW_TOTP_SECRET", "TELEGRAM_BOT_TOKEN")


class C:  # terminal colours (disabled when not a TTY or NO_COLOR is set)
    on = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    B, D, G, Y, R, X = (("\033[1m", "\033[2m", "\033[32m", "\033[33m", "\033[31m", "\033[0m") if on
                        else ("",) * 6)


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
    out = {}
    if path.exists():
        for ln in path.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#") and "=" in ln:
                k, v = ln.split("=", 1)
                out[k.strip()] = v.split(" #", 1)[0].strip().strip('"').strip("'")
    return out


def save_env_file(values: dict[str, str], path: Path = ENV_FILE) -> None:
    merged = {**load_env_file(path), **{k: v for k, v in values.items() if v is not None}}
    lines = ["# groww-orb-bot settings - written by the setup wizard. Keep this file private."]
    lines += [f"{k}={v}" for k, v in sorted(merged.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # owner read/write only
    except OSError:
        pass


def _mask(v: str) -> str:
    return (v[:3] + "•" * 6 + v[-2:]) if v and len(v) > 6 else ("•" * len(v) if v else "")


def ask(prompt: str, default=None, cast=str, check=None, secret: bool = False, help_: str = ""):
    """Prompt until a valid answer. Enter keeps the default."""
    import getpass
    if help_:
        print(f"  {C.D}{help_}{C.X}")
    shown = _mask(str(default)) if secret and default else default
    suffix = f" [{shown}]" if default not in (None, "") else ""
    while True:
        raw = (getpass.getpass if secret else input)(f"  {prompt}{suffix}: ").strip()
        if not raw and default not in (None, ""):
            raw = str(default)
        if not raw:
            print(f"  {C.R}Required.{C.X}")
            continue
        try:
            val = cast(raw)
        except ValueError:
            print(f"  {C.R}Please enter a valid {getattr(cast, '__name__', 'value')}.{C.X}")
            continue
        err = check(val) if check else None
        if err:
            print(f"  {C.R}{err}{C.X}")
            continue
        return val


def ask_yes(prompt: str, default: bool = False) -> bool:
    while True:
        raw = input(f"  {prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes", "n", "no"):
            return raw.startswith("y")


def menu(title: str, options: list[tuple[str, str]], default: int = 1) -> str:
    print(f"\n{C.B}{title}{C.X}")
    for i, (_, label) in enumerate(options, 1):
        print(f"  {i}) {label}")
    n = ask("Choose", default, int, lambda v: None if 1 <= v <= len(options) else f"Enter 1-{len(options)}")
    return options[n - 1][0]


def multi_select(title: str, options: list[tuple[str, str]], default: list[str]) -> list[str]:
    print(f"\n{C.B}{title}{C.X}")
    for i, (k, label) in enumerate(options, 1):
        print(f"  {i}) {k:<13}{C.D}{label}{C.X}")
    keys = [k for k, _ in options]
    dflt = ",".join(str(keys.index(k) + 1) for k in default if k in keys)

    def parse(raw: str) -> list[str]:
        if raw.strip().lower() == "all":
            return keys
        picked = [keys[int(p) - 1] for p in raw.replace(" ", "").split(",") if p]
        return list(dict.fromkeys(picked))
    return ask("Numbers separated by commas, or 'all'", dflt, parse,
               lambda v: None if v else "Pick at least one")


def _date(s: str) -> date:
    return date.fromisoformat(s)


def next_session_start(cfg: Config, now: datetime) -> datetime:
    d = now.date()
    if now.time() >= time(9, 5):
        d += timedelta(days=1)
    while not is_trading_day(cfg, d):
        d += timedelta(days=1)
    return datetime.combine(d, time(9, 5), IST)


def wizard_credentials(env: dict[str, str], saved: dict[str, str]) -> None:
    print(f"\n{C.B}Groww API login{C.X}  {C.D}(Groww web -> Profile -> Trade API / Groww Cloud -> API Keys){C.X}")
    env["GROWW_API_KEY"] = ask("GROWW_API_KEY", saved.get("GROWW_API_KEY"), secret=True)
    method = menu("Login method", [("totp", "TOTP secret  (recommended - no daily approval needed)"),
                                   ("secret", "API secret   (must be approved on Groww Cloud every day)")],
                  default=1 if saved.get("GROWW_TOTP_SECRET") or not saved.get("GROWW_API_SECRET") else 2)
    if method == "totp":
        env["GROWW_TOTP_SECRET"] = ask("GROWW_TOTP_SECRET", saved.get("GROWW_TOTP_SECRET"), secret=True)
        env["GROWW_API_SECRET"] = ""
    else:
        env["GROWW_API_SECRET"] = ask("GROWW_API_SECRET", saved.get("GROWW_API_SECRET"), secret=True)
        env["GROWW_TOTP_SECRET"] = ""
    if ask_yes("Test the login now?", True):
        try:
            os.environ.update(env)
            c = Config()
            from growwapi import GrowwAPI
            if c.totp_secret:
                import pyotp
                GrowwAPI.get_access_token(api_key=c.api_key, totp=pyotp.TOTP(c.totp_secret).now())
            else:
                GrowwAPI.get_access_token(api_key=c.api_key, secret=c.api_secret)
            print(f"  {C.G}✓ Login OK{C.X}")
        except ImportError as e:
            print(f"  {C.R}Missing package ({e.name}). Run: pip install -r requirements.txt{C.X}")
        except Exception as e:
            print(f"  {C.R}✕ Login failed: {e}{C.X}")
            if not ask_yes("Continue anyway?", False):
                raise SystemExit(1)


MARKETS = {  # name: (exchange, segment, spot_segment, spot_symbol, strike_step, lot, tick_rupees, use_vix, stt%, exch%)
    # Lots/ticks verified against Groww's instrument list (Nov 2026); the bot re-reads them live each session.
    "SENSEX": ("BSE", "FNO", "CASH", "SENSEX", 100, 20, 0.05, True, 0.1, 0.0325),
    "NIFTY": ("NSE", "FNO", "CASH", "NIFTY", 50, 65, 0.05, True, 0.1, 0.035),
    "CRUDEOILM": ("MCX", "COMMODITY", "COMMODITY", "AUTO", 50, 10, 0.05, False, 0.05, 0.05),
    "NATGASMINI": ("MCX", "COMMODITY", "COMMODITY", "AUTO", 5, 250, 0.05, False, 0.05, 0.05),
    "GOLDM": ("MCX", "COMMODITY", "COMMODITY", "AUTO", 100, 10, 0.5, False, 0.05, 0.05),
    "SILVERM": ("MCX", "COMMODITY", "COMMODITY", "AUTO", 250, 5, 0.5, False, 0.05, 0.05),
    "CRUDEOIL": ("MCX", "COMMODITY", "COMMODITY", "AUTO", 50, 100, 0.1, False, 0.05, 0.05),
    "NATURALGAS": ("MCX", "COMMODITY", "COMMODITY", "AUTO", 5, 1250, 0.05, False, 0.05, 0.05),
}


def wizard_market(env: dict[str, str], saved: dict[str, str]) -> None:
    names = list(MARKETS)
    labels = {"SENSEX": "SENSEX options (BSE, 09:15-15:30)", "NIFTY": "NIFTY options (NSE, 09:15-15:30)",
              "CRUDEOILM": "CRUDEOILM crude-mini options (MCX, lot 10)   <- best fit for ~₹1L",
              "NATGASMINI": "NATGASMINI natural-gas-mini options (MCX, lot 250)",
              "GOLDM": "GOLDM gold-mini options (MCX, lot 10)", "SILVERM": "SILVERM silver-mini options (MCX, lot 5)",
              "CRUDEOIL": "CRUDEOIL options (MCX, lot 100 - needs more capital)",
              "NATURALGAS": "NATURALGAS options (MCX, lot 1250 - needs more capital)"}
    cur = saved.get("ORB_UNDERLYING", "SENSEX")
    idx = menu("Market to trade", [(n, labels[n]) for n in names], default=names.index(cur) + 1 if cur in names else 1)
    exch, seg, sseg, ssym, step, lot, tick, vix, stt, exf = MARKETS[idx]
    env.update({"ORB_UNDERLYING": idx, "ORB_EXCHANGE": exch, "ORB_SEGMENT": seg, "ORB_SPOT_SEGMENT": sseg,
                "ORB_SPOT_SYMBOL": ssym, "ORB_STRIKE_STEP": str(step), "ORB_TICK_SIZE": str(tick),
                "ORB_USE_VIX": "1" if vix else "0",
                "ORB_STT_PCT": str(stt), "ORB_EXCH_PCT": str(exf)})
    if exch == "MCX":
        print(f"  {C.D}Commodity mode: underlying price = nearest {idx} FUTURES (auto from Groww's instrument list), "
              f"no India VIX, CTT {stt}% on sell. Lot size & strikes are read from Groww.{C.X}")
    same = cur == idx
    env["ORB_LOT_SIZE"] = str(ask("Lot size fallback", (saved.get("ORB_LOT_SIZE") if same else None) or str(lot),
                                  int, lambda v: None if v > 0 else "Must be > 0",
                                  help_="Used only if the live lot size can't be read from Groww."))
    env["ORB_AUTO_ATM"] = "1" if ask_yes("Auto-pick the ATM strike of the nearest expiry?",
                                         saved.get("ORB_AUTO_ATM", "1") == "1") else "0"
    if env["ORB_AUTO_ATM"] == "0":
        env["ORB_CE_SYMBOL"] = ask("CE trading symbol (as shown in Groww)", saved.get("ORB_CE_SYMBOL"), str.upper)
        env["ORB_PE_SYMBOL"] = ask("PE trading symbol", saved.get("ORB_PE_SYMBOL"), str.upper)
    wizard_session(env, saved, exch)


def wizard_session(env: dict[str, str], saved: dict[str, str], exch: str) -> None:
    now = datetime.now(IST)
    start = (now + timedelta(minutes=2)).replace(second=0, microsecond=0)
    opts = [("now", f"Start NOW: range {start:%H:%M}-{start + timedelta(minutes=15):%H:%M}, entries till "
                    f"{start + timedelta(minutes=30):%H:%M}, flat by {start + timedelta(minutes=45):%H:%M}  (for testing)")]
    if exch != "MCX":
        opts.append(("morning", "Morning session 09:15-10:00 (the strategy's design)"))
    else:
        opts.append(("evening", "MCX evening session 18:00-18:45 (US-hours momentum)"))
    opts.append(("custom", "Custom start time"))
    pick = menu("Session window", opts, default=1)
    if pick == "now":
        env["ORB_SESSION_OPEN"] = f"{start:%H:%M}"
        print(f"  {C.Y}Note: a mid-session window is fine for testing the pipeline, but it isn't a true opening range.{C.X}")
    elif pick == "morning":
        env["ORB_SESSION_OPEN"] = "09:15"
    elif pick == "evening":
        env["ORB_SESSION_OPEN"] = "18:00"
    else:
        env["ORB_SESSION_OPEN"] = ask("Start time HH:MM (IST)", saved.get("ORB_SESSION_OPEN", "09:15"),
                                      lambda v: time.fromisoformat(v).strftime("%H:%M"))
    env["ORB_OR_MIN"] = str(ask("Opening-range minutes", saved.get("ORB_OR_MIN", "15"), int,
                                lambda v: None if 5 <= v <= 60 else "5-60"))
    env["ORB_ENTRY_MIN"] = str(int(env["ORB_OR_MIN"]) + 15)
    env["ORB_SESSION_MIN"] = str(int(env["ORB_OR_MIN"]) + 30)


def wizard_risk(env: dict[str, str], saved: dict[str, str]) -> None:
    print(f"\n{C.B}Money & risk{C.X}")
    pos = lambda v: None if v > 0 else "Must be > 0"  # noqa: E731
    env["ORB_CAPITAL"] = str(ask("Capital (₹)", saved.get("ORB_CAPITAL", "100000"), float, pos))
    env["ORB_RISK_PCT"] = str(ask("Risk per trade (% of capital, 0.2-1.5)", saved.get("ORB_RISK_PCT", "1.0"), float,
                                  lambda v: None if 0.2 <= v <= 1.5 else "Between 0.2 and 1.5 (hard ceiling is 1.5%)"))
    env["ORB_DAILY_LOSS"] = str(ask("Daily loss limit (₹) - stop for the day after this", saved.get("ORB_DAILY_LOSS", "2000"), float, pos))
    env["ORB_MAX_TRADES"] = str(ask("Max trades per day per strategy", saved.get("ORB_MAX_TRADES", "2"), int,
                                    lambda v: None if 1 <= v <= 5 else "1-5"))
    env["ORB_MONTHLY_TARGET"] = str(ask("Monthly target for the report (₹)", saved.get("ORB_MONTHLY_TARGET", "50000"), float, pos))
    budget = float(env["ORB_CAPITAL"]) * float(env["ORB_RISK_PCT"]) / 100
    lot = int(env.get("ORB_LOT_SIZE") or saved.get("ORB_LOT_SIZE") or 20)
    name = env.get("ORB_UNDERLYING") or saved.get("ORB_UNDERLYING") or "SENSEX"
    print(f"  {C.D}=> max risk per trade ₹{budget:,.0f}. With 1 {name} lot ({lot}) that allows a stop ~₹{budget / lot:,.2f} "
          f"below the entry premium.{C.X}")


def wizard_headlines(env: dict[str, str], saved: dict[str, str], cfg: Config) -> None:
    if not ask_yes("Enter today's market headlines for news sentiment?", False):
        return
    print(f"  {C.D}Paste one headline per line; press Enter on an empty line to finish.{C.X}")
    heads = []
    while True:
        ln = input("  > ").strip()
        if not ln:
            break
        heads.append(ln)
    if heads:
        cfg.headlines_file.parent.mkdir(parents=True, exist_ok=True)
        cfg.headlines_file.write_text("\n".join(heads) + "\n", encoding="utf-8")
        print(f"  {C.G}✓ saved {len(heads)} headline(s) to {cfg.headlines_file}{C.X}")
        env["ORB_USE_FINBERT"] = "1" if ask_yes("Score them with FinBERT? (needs: pip install torch transformers, ~1 GB)",
                                                saved.get("ORB_USE_FINBERT") == "1") else "0"


def _summary(rows: list[tuple[str, str]]) -> None:
    print(f"\n{C.B}Summary{C.X}")
    for k, v in rows:
        print(f"  {k:<22}{v}")


def wizard_save(env: dict[str, str]) -> None:
    if not ask_yes("Save these settings to .env for next time (and for cron)?", True):
        return
    keep = dict(env)
    if not ask_yes("Also save API key/secret? (plain text, file readable only by you; needed for cron)", False):
        for k in SECRET_KEYS:
            keep.pop(k, None)
    keep.pop("ORB_CONFIRM_LIVE", None)  # live confirmation is never persisted
    save_env_file(keep)
    print(f"  {C.G}✓ saved {ENV_FILE.resolve()}{C.X}")


def wizard() -> int:
    saved = load_env_file()
    env: dict[str, str] = {}
    print(f"\n{C.B}══ Groww intraday options bot ══{C.X}  {C.D}paper by default · Ctrl+C to quit{C.X}")
    if saved:
        print(f"  {C.D}Loaded saved settings from {ENV_FILE} - press Enter to keep a value in [brackets].{C.X}")
    action = menu("What do you want to do?", [
        ("paper", "Paper trade  - live Groww prices, simulated orders, all strategies in parallel  (recommended)"),
        ("live", "Live trade   - REAL orders, one strategy"),
        ("simulate", "Simulate     - offline synthetic days, no Groww account needed"),
        ("backtest", "Backtest     - compare strategies on real Groww history (or synthetic)"),
        ("train", "Train ML     - train the 'ml' strategy on downloaded history"),
        ("rebuild", "Rebuild reports"),
        ("cron", "Show how to run automatically every morning (cron)"),
        ("quit", "Quit"),
    ])
    if action == "quit":
        return 0
    if action == "rebuild":
        rebuild(Config())
        return 0
    if action == "cron":
        here = Path.cwd()
        print(f"\n  Run {C.B}crontab -e{C.X} and add these two lines (needs keys saved in .env):\n")
        print("  CRON_TZ=Asia/Kolkata")
        print(f"  5 9 * * 1-5  cd {here} && {sys.executable} bot.py >> logs/cron.log 2>&1\n")
        return 0

    strat_opts = [(k, v) for k, v in STRATEGIES.items()]
    synthetic = action == "simulate"
    if action in ("backtest", "train"):
        synthetic = menu("Data source", [("real", "Real Groww 1-min history (needs API login)"),
                                         ("synthetic", "Synthetic offline data (pipeline test only)")]) == "synthetic"
    needs_api = action in ("paper", "live") or (action in ("backtest",) and not synthetic)

    if action in ("paper", "live", "backtest") or (action == "train" and not synthetic):
        wizard_market(env, saved)
    if needs_api:
        wizard_credentials(env, saved)
    if action in ("paper", "live", "simulate", "backtest"):
        wizard_risk(env, saved)

    saved_strats = [s for s in saved.get("ORB_STRATEGIES", DEFAULT_STRATEGIES).split(",") if s in STRATEGIES]
    if action == "live":
        print(f"\n  {C.Y}Live mode trades ONE strategy - pick the one that won your paper test.{C.X}")
        names = [menu("Strategy for LIVE trading", strat_opts[:4])]
    elif action in ("paper", "simulate", "backtest"):
        names = multi_select("Strategies to run", strat_opts, saved_strats or DEFAULT_STRATEGIES.split(","))
        env["ORB_STRATEGIES"] = ",".join(names)
    else:
        names = []

    if "ml" in names and not Config().ml_model_file.exists():
        print(f"  {C.Y}Note: 'ml' has no trained model yet - it will stay idle. Use 'Train ML' first.{C.X}")

    os.environ.update({k: v for k, v in env.items() if v is not None})
    ns = argparse.Namespace(synthetic=synthetic, start=None, end=None, days=22)

    if action in ("backtest", "train"):
        if synthetic:
            ns.days = ask("Number of synthetic days", 44 if action == "backtest" else 250, int,
                          lambda v: None if 5 <= v <= 400 else "5-400")
        else:
            today = datetime.now(IST).date()
            if action == "backtest":
                ns.start = str(ask("From date (YYYY-MM-DD)", str(today - timedelta(days=90)), _date))
                ns.end = str(ask("To date   (YYYY-MM-DD)", str(today - timedelta(days=1)), _date,
                                 lambda v: None if v >= date.fromisoformat(ns.start) else "Must be after the start date"))
            else:
                ns.end = str(ask("Train on cached days up to (YYYY-MM-DD) - keep later days for testing",
                                 str(today - timedelta(days=30)), _date))
    if action == "simulate":
        ns.days = ask("Number of synthetic days", 22, int, lambda v: None if 1 <= v <= 400 else "1-400")
    if action in ("paper", "live"):
        wizard_headlines(env, saved, Config())
        if ask_yes("Send Telegram alerts (trades, risk events, daily summary)?", bool(saved.get("TELEGRAM_BOT_TOKEN"))):
            print(f"  {C.D}Create a bot with @BotFather to get the token; send it a message, then get your chat id "
                  f"from https://api.telegram.org/bot<TOKEN>/getUpdates{C.X}")
            env["TELEGRAM_BOT_TOKEN"] = ask("TELEGRAM_BOT_TOKEN", saved.get("TELEGRAM_BOT_TOKEN"), secret=True)
            env["TELEGRAM_CHAT_ID"] = ask("TELEGRAM_CHAT_ID", saved.get("TELEGRAM_CHAT_ID"))

    cfg = replace(Config(), mode={"paper": "paper", "live": "live"}.get(action, Config().mode))
    rows = [("Action", action), ("Market", f"{cfg.underlying} ({cfg.exchange} {cfg.segment})"),
            ("Session", f"{cfg.market_open:%H:%M} open · range till {cfg.opening_range_end:%H:%M} · "
                        f"entries till {cfg.entry_cutoff:%H:%M} · flat {cfg.square_off:%H:%M}"),
            ("Capital / risk", f"₹{cfg.total_capital:,.0f} / {cfg.base_risk_pct:g}% per trade"),
            ("Daily loss limit", f"₹{cfg.daily_loss_limit:,.0f}")]
    if names:
        rows.append(("Strategies", ", ".join(names)))
    if needs_api:
        rows.append(("Login", "TOTP" if cfg.totp_secret else "API secret"))
    _summary(rows)

    if action == "live":
        print(f"\n  {C.R}{C.B}LIVE MODE places REAL orders with REAL money on your Groww account.{C.X}")
        print(f"  {C.D}Make sure: weeks of paper results, static IP registered with Groww, you know how to exit manually.{C.X}")
        if input("  Type  I UNDERSTAND  to continue: ").strip() != "I UNDERSTAND":
            print("  Cancelled.")
            return 0
        os.environ["ORB_CONFIRM_LIVE"] = "YES"
    elif not ask_yes("Start now?", True):
        wizard_save(env)
        return 0
    wizard_save(env)

    cfg = replace(Config(), mode=cfg.mode)  # re-read with everything applied
    if action in ("paper", "live"):
        now = datetime.now(IST)
        if not is_trading_day(cfg, now.date()) or now.time() >= cfg.square_off:
            nxt = next_session_start(cfg, now)
            print(f"\n  {C.Y}Today's 09:15-10:00 session is not available (holiday/weekend or already over).{C.X}")
            if not ask_yes(f"Keep this window open and run at {nxt:%a %d %b %H:%M} IST?", True):
                return 0
            print(f"  {C.D}Waiting... (Ctrl+C to cancel). Keep the computer awake.{C.X}")
            while datetime.now(IST) < nxt:
                _time.sleep(min(60.0, max(1.0, (nxt - datetime.now(IST)).total_seconds())))
        print(f"\n  {C.G}Starting {cfg.mode} session - reports will be in {cfg.reports_dir / cfg.mode}/{C.X}\n")
        return run_session(cfg, names)
    if action == "simulate":
        run_simulation(cfg, ns.days, datetime.now(IST).date().replace(day=1), names)
        return 0
    if action == "backtest":
        return backtest_main(ns, cfg, names)
    if action == "train":
        return train_ml_main(ns, cfg)
    return 0


def main() -> int:
    for k, v in load_env_file().items():  # .env auto-loaded; real environment variables win
        os.environ.setdefault(k, v)
    ap = argparse.ArgumentParser(description="Groww morning ORB + signal-model bot",
                                 formatter_class=argparse.RawDescriptionHelpFormatter,
                                 epilog="strategies:\n" + "\n".join(f"  {k:<13} {v}" for k, v in STRATEGIES.items()))
    ap.add_argument("--strategies", default=os.environ.get("ORB_STRATEGIES", DEFAULT_STRATEGIES),
                    help=f"comma list (default {DEFAULT_STRATEGIES})")
    ap.add_argument("--simulate", action="store_true", help="offline synthetic sessions")
    ap.add_argument("--backtest", action="store_true", help="compare strategies on the same data")
    ap.add_argument("--synthetic", action="store_true", help="with --backtest/--train-ml: synthetic data")
    ap.add_argument("--train-ml", action="store_true", help="train the ml strategy on cached history")
    ap.add_argument("--rebuild-reports", action="store_true")
    ap.add_argument("--days", type=int, default=22)
    ap.add_argument("--from", "--start", dest="start", help="YYYY-MM-DD")
    ap.add_argument("--wizard", action="store_true", help="interactive setup (default when run with no options in a terminal)")
    ap.add_argument("--to", dest="end", help="YYYY-MM-DD")
    a = ap.parse_args()
    if a.wizard or (len(sys.argv) == 1 and sys.stdin.isatty()):
        try:
            return wizard()
        except (KeyboardInterrupt, EOFError):
            print("\n  Cancelled.")
            return 130
    cfg = Config()
    names = [n.strip() for n in a.strategies.split(",") if n.strip()]
    for n in names:
        try:
            preset(cfg, n)  # validate names early
        except ValueError as e:
            raise SystemExit(str(e))
    if a.train_ml:
        return train_ml_main(a, cfg)
    if a.backtest:
        return backtest_main(a, cfg, names)
    if a.simulate:
        run_simulation(cfg, a.days, date.fromisoformat(a.start) if a.start else date.today().replace(day=1), names)
        return 0
    if a.rebuild_reports:
        rebuild(cfg)
        return 0
    return run_session(cfg, names)


if __name__ == "__main__":
    sys.exit(main())
