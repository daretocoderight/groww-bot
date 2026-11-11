import pytest  # noqa: F401

import bot
from bot import Config, GrowwBroker


cfg = Config(mode="sim")


def test_zero_lots_when_budget_too_small():
    # ₹1,000 budget, ₹60 risk/unit, lot 20 => 16 units < 1 lot => 0 (no forced minimum)
    d = bot.size_position(cfg, 0.0, entry=300, stop=240, lot_size=20)
    assert d.lots == 0


def test_risk_never_exceeds_budget():
    d = bot.size_position(cfg, 0.0, entry=300, stop=285, lot_size=20)
    assert d.lots == 3 and d.rupee_risk_actual <= d.rupee_risk_budget


def test_sentiment_clamped_to_hard_ceiling():
    assert bot.sentiment_adjusted_risk_pct(cfg, 0.9) == cfg.max_risk_pct
    assert bot.sentiment_adjusted_risk_pct(cfg, -0.95) == cfg.min_risk_pct


def test_capital_cap():
    d = bot.size_position(cfg, 0.0, entry=1500, stop=1499, lot_size=20)
    assert d.capital_needed <= cfg.total_capital * cfg.max_capital_deploy_pct / 100


def test_vix_multiplier():
    assert bot.vix_multiplier(cfg, 15) == 1.0
    assert abs(bot.vix_multiplier(cfg, 23) - 1.5) < 1e-9


def test_stop_only_ratchets_up():
    s = bot.next_stop(cfg, current_stop=280, entry=300, peak=301, vix=30, r_unit=20)
    assert s == 280


def test_breakeven_after_1r():
    s = bot.next_stop(cfg, current_stop=280, entry=300, peak=320, vix=15, r_unit=20)
    assert s >= 300


def test_charges_positive():
    c = bot.estimate_charges(cfg, 30000, 31000)
    assert c.total > 40 and c.stt == 31.0



class FakeGroww:
    SEGMENT_FNO = "FNO"

    def __init__(self, resp):
        self.resp, self.calls = resp, []

    def get_ltp(self, segment, exchange_trading_symbols):
        self.calls.append((segment, exchange_trading_symbols))
        return self.resp


def make(resp):
    b = GrowwBroker.__new__(GrowwBroker)  # skip login
    b.cfg, b.g = Config(mode="sim", exchange="BSE"), FakeGroww(resp)
    b.seg = "FNO"
    return b


def test_batched_ltp_single_request():
    b = make({"BSE_SENSEXCE": 410.5, "BSE_SENSEXPE": 222.0})
    assert b.ltps(["SENSEXCE", "SENSEXPE"]) == {"SENSEXCE": 410.5, "SENSEXPE": 222.0}
    assert b.g.calls == [("FNO", ("BSE_SENSEXCE", "BSE_SENSEXPE"))]


def test_missing_symbol_raises():
    with pytest.raises(RuntimeError):
        make({"BSE_SENSEXCE": 410.5}).ltps(["SENSEXCE", "SENSEXPE"])


# ---------------------------------------------------------------- signal models
def _bars(closes):
    return [[i * 60, c, c + 1, c - 1, c] for i, c in enumerate(closes)]


def test_bar_builder_completes_minute_bars():
    bb = bot.BarBuilder()
    done = [bb.update(t, 100 + t / 60) for t in range(0, 181, 1)]
    assert sum(done) == 3 and len(bb.bars) == 3 and bb.bars[0][0] == 0


def test_momentum_signals_up_cross():
    sig = bot.MomentumSignal(Config(mode="sim"))
    bb = bot.BarBuilder()
    bb.bars = _bars([100 - i * 0.5 for i in range(14)] + [96, 99, 103])
    bb.twap_sum, bb.twap_n = 97 * 10, 10
    seen = []
    for k in range(14, len(bb.bars) + 1):
        b2 = bot.BarBuilder()
        b2.bars, b2.twap_sum, b2.twap_n = bb.bars[:k], bb.twap_sum, bb.twap_n
        seen.append(sig.on_bar(b2)[0])
    assert "CE" in seen and "PE" not in seen


def test_supertrend_flips_on_reversal():
    d = bot.supertrend(_bars([100 + i for i in range(15)] + [114 - 3 * i for i in range(12)]), 7, 2.5)
    assert 1 in d and -1 in d


def test_logreg_learns_separable_data():
    import random
    rng = random.Random(1)
    samples = [([x, rng.gauss(0, 1)], int(x > 0)) for x in (rng.gauss(0, 1) for _ in range(400))]
    m = bot.train_logreg(samples, epochs=200)
    acc = sum((bot.predict(m, x) >= 0.5) == bool(y) for x, y in samples) / len(samples)
    assert acc > 0.9


def test_shared_feed_fetches_once_per_ttl():
    class Inner:
        n = 0
        def ltps(self, s):
            Inner.n += 1
            return {x: 1.0 for x in s}
    f = bot.SharedFeed(Inner(), ttl=10, clock=bot.SimClock(bot.datetime(2026, 1, 1, tzinfo=bot.IST)))
    for _ in range(4):  # four strategy threads asking in the same second
        f.ltps(["A", "B"])
    assert Inner.n == 1


def test_secrets_never_in_report_params():
    import os
    os.environ["GROWW_TOTP_SECRET"] = "SUPERSECRET"
    try:
        c = Config(mode="sim")
        clock = bot.SimClock(bot.datetime(2026, 11, 2, 9, 59, tzinfo=bot.IST))
        e = bot.Engine(c, bot.SimBroker(c, clock, 1), clock, bot.NEUTRAL)
        assert "SUPERSECRET" not in str(e.r.params)
    finally:
        del os.environ["GROWW_TOTP_SECRET"]


def test_paper_refuses_synthetic_ml_model(tmp_path):
    import json
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"trained_on": "synthetic", "w": [], "b": 0, "mu": [], "sd": []}))
    assert bot.MLSignal(Config(mode="paper", ml_model_file=f)).model is None
    assert bot.MLSignal(Config(mode="sim", ml_model_file=f)).model is not None


# ---------------------------------------------------------------- wizard
def test_env_file_roundtrip_and_permissions(tmp_path):
    import os
    p = tmp_path / ".env"
    bot.save_env_file({"ORB_CAPITAL": "150000", "ORB_STRATEGIES": "orb,ml"}, p)
    bot.save_env_file({"ORB_RISK_PCT": "1.2"}, p)            # merges, keeps earlier keys
    assert bot.load_env_file(p) == {"ORB_CAPITAL": "150000", "ORB_STRATEGIES": "orb,ml", "ORB_RISK_PCT": "1.2"}
    assert oct(os.stat(p).st_mode & 0o777) == "0o600"


def test_wizard_simulate_flow(monkeypatch, tmp_path):
    answers = iter(["3", "", "", "", "", "", "2,3", "2", "y", "n"])  # simulate, defaults, momentum+supertrend, 2 days
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot.os, "environ", dict(bot.os.environ))  # keep wizard env changes inside the test
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    ran = {}
    monkeypatch.setattr(bot, "run_simulation", lambda cfg, days, start, names: ran.update(days=days, names=names))
    assert bot.wizard() == 0
    assert ran == {"days": 2, "names": ["momentum", "supertrend"]}


def test_wizard_live_requires_typed_confirmation(monkeypatch, tmp_path):
    answers = iter(["2", "1", "", "y", "2", "", "1", "n", "", "", "", "", "", "3", "n", "n", "yes"])  # live, SENSEX, lot, atm, morning, OR, totp, no test, risk x5, strategy, no headlines, no telegram, wrong confirm
    secrets = iter(["KEY123456", "TOTP12345"])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(bot.os, "environ", dict(bot.os.environ))  # keep wizard env changes inside the test
    monkeypatch.setattr("builtins.input", lambda *_: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_: next(secrets))
    called = []
    monkeypatch.setattr(bot, "run_session", lambda *a: called.append(a))
    assert bot.wizard() == 0 and not called          # "yes" != "I UNDERSTAND" => cancelled


# ---------------------------------------------------------------- broker-side stop (live safety)
class FakeLive(bot.SimBroker):
    """SimBroker prices + a recording fake of the Groww stop-order API."""
    supports_broker_stop = True

    def __init__(self, cfg, clock, stop_status="OPEN", cancel_result=("CANCELLED", 0, 0.0), place_fails=False):
        super().__init__(cfg, clock, 7)
        self.calls, self.sells = [], []
        self.stop_status, self.cancel_result, self.place_fails = stop_status, cancel_result, place_fails

    def place_stop(self, symbol, qty, trigger):
        self.calls.append(("place", qty, trigger))
        if self.place_fails:
            raise RuntimeError("rejected")
        return "SL1"

    def modify_stop(self, oid, qty, trigger):
        self.calls.append(("modify", qty, trigger))

    def stop_state(self, oid):
        return (self.stop_status, 20, 95.0) if self.stop_status == "EXECUTED" else (self.stop_status, 0, 0.0)

    def cancel_stop(self, oid):
        self.calls.append(("cancel",))
        return self.cancel_result

    def sell(self, symbol, qty, ref):
        self.sells.append(qty)
        return super().sell(symbol, qty, ref)


def _live_engine(**kw):
    c = Config(mode="live", base_risk_pct=0.2, broker_stop=True, stop_update_min_sec=0, stop_poll_sec=0)  # => 1 lot
    clock = bot.SimClock(bot.datetime(2026, 11, 12, 9, 35, tzinfo=bot.IST))
    b = FakeLive(c, clock, **kw)
    e = bot.Engine(c, b, clock, bot.NEUTRAL)
    e.r.lot_size = 20
    tr = bot.SymbolTrack("XCE")
    tr.or_low = 90.0
    e._enter(tr, 100.0, 100.0, sl=92.0)
    return e, b, clock


def test_protective_stop_placed_and_trailed_then_cancelled_on_exit():
    e, b, clock = _live_engine()
    assert b.calls[0][0] == "place" and e.stop_oid == "SL1"
    for px in (110, 120, 130):              # price runs up -> stop trails -> modify_order
        clock.sleep(1)
        e._manage(px, 15.0)
    assert any(c[0] == "modify" for c in b.calls)
    e._exit("time square-off 10:00", ref=128.0)
    assert ("cancel",) in b.calls and b.sells == [20]   # cancelled first, then exactly one sell


def test_no_double_sell_when_broker_stop_already_executed():
    e, b, _ = _live_engine(stop_status="EXECUTED")
    e._manage(96.0, 15.0)                    # poll finds the SL-M filled at Groww
    assert e.position is None and b.sells == []
    assert e.r.trades[-1]["exit_reason"] == "broker stop-loss" and e.r.trades[-1]["exit_price"] == 95.0


def test_no_double_sell_when_stop_fills_during_cancel():
    e, b, _ = _live_engine(cancel_result=("EXECUTED", 20, 94.5))
    e._exit("time square-off 10:00", ref=101.0)
    assert b.sells == [] and e.r.trades[-1]["exit_price"] == 94.5


def test_partial_stop_fill_sells_only_remainder():
    e, b, _ = _live_engine(cancel_result=("CANCELLED", 15, 94.0))
    e._exit("time square-off 10:00", ref=101.0)
    assert b.sells == [5]


def test_rejected_protective_stop_exits_immediately():
    e, b, _ = _live_engine(place_fails=True)
    assert e.position is None and b.sells == [20]
    assert e.r.trades[-1]["exit_reason"] == "protective stop rejected"


def test_paper_mode_never_uses_broker_stop():
    c = Config(mode="paper")
    clock = bot.SimClock(bot.datetime(2026, 11, 12, 9, 35, tzinfo=bot.IST))
    assert bot.Engine(c, FakeLive(c, clock), clock, bot.NEUTRAL).use_broker_stop is False


# ---------------------------------------------------------------- realism + controls
def test_spread_model_buys_above_and_sells_below():
    c = Config(mode="sim", paper_slippage_pct=0.0)
    clock = bot.SimClock(bot.datetime(2026, 11, 12, 9, 40, tzinfo=bot.IST))
    sb = bot.SimBroker(c, clock, 1)
    assert sb.buy("X", 20, 400.0).avg_price > 400.0 > sb.sell("X", 20, 400.0).avg_price
    clock2 = bot.SimClock(bot.datetime(2026, 11, 12, 9, 16, tzinfo=bot.IST))   # opening minutes: wider
    wide = bot.SimBroker(c, clock2, 1).buy("X", 20, 400.0).avg_price
    assert wide > sb.buy("X", 20, 400.0).avg_price


def test_stale_feed_blocks_entries():
    c = Config(mode="sim", stale_sec=30)
    clock = bot.SimClock(bot.datetime(2026, 11, 12, 9, 35, tzinfo=bot.IST))
    e = bot.Engine(c, bot.SimBroker(c, clock, 1), clock, bot.NEUTRAL)
    for _ in range(31):
        e._stale_check({"A": 100.0})
        clock.sleep(1)
    assert e.stale
    e._stale_check({"A": 100.5})
    assert not e.stale


def test_random_control_is_reproducible_per_day():
    def draws():
        sig, bb = bot.RandomSignal(Config(mode="sim")), bot.BarBuilder()
        bb.bars = [[bot.datetime(2026, 11, 12, 9, 15, tzinfo=bot.IST).timestamp(), 1, 1, 1, 1]]
        return [sig.on_bar(bb)[0] for _ in range(50)]
    assert draws() == draws() and any(draws())


# ---------------------------------------------------------------- commodities / sessions
def test_session_window_from_env(monkeypatch):
    monkeypatch.setenv("ORB_SESSION_OPEN", "18:00")
    monkeypatch.setenv("ORB_OR_MIN", "10")
    c = Config(mode="sim")
    assert (c.market_open, c.opening_range_end, c.entry_cutoff, c.square_off, c.signal_start) == (
        bot.time(18, 0), bot.time(18, 10), bot.time(18, 30), bot.time(18, 45), bot.time(18, 5))


def test_mcx_market_profile_sets_commodity_segment(monkeypatch):
    for k, v in zip(("ORB_EXCHANGE", "ORB_SEGMENT", "ORB_SPOT_SEGMENT", "ORB_USE_VIX", "ORB_STT_PCT"),
                    ("MCX", "COMMODITY", "COMMODITY", "0", "0.05")):
        monkeypatch.setenv(k, v)
    c = Config(mode="sim")
    assert (c.exchange, c.segment, c.spot_segment, c.use_vix, c.stt_sell_pct) == ("MCX", "COMMODITY", "COMMODITY", False, 0.05)


def test_commodity_session_runs_without_vix(monkeypatch):
    monkeypatch.setenv("ORB_SESSION_OPEN", "18:00")
    monkeypatch.setenv("ORB_USE_VIX", "0")
    c = Config(mode="sim", strategy_name="supertrend", use_spot_confirm=True)
    clock = bot.SimClock(bot.datetime(2026, 11, 11, 17, 59, 50, tzinfo=bot.IST))
    r = bot.Engine(c, bot.SimBroker(c, clock, 3), clock, bot.NEUTRAL).run()
    assert r.vix_open == 0.0 and r.status != "pending"
    assert any("VIX not used" in e[2] for e in r.events)
    assert r.events[-1][0] >= "18:44:00"   # ran through the configured window


def test_ml_model_refused_for_other_underlying(tmp_path):
    import json
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"underlying": "SENSEX", "trained_on": "x", "w": [], "b": 0, "mu": [], "sd": []}))
    assert bot.MLSignal(Config(mode="sim", ml_model_file=f, underlying="CRUDEOIL")).model is None
