import io
import os
import tempfile
import unittest
import time
from datetime import datetime
from risk_manager import RiskManager
from config import RISK, INDICES_CONFIG
from ist_time import ist_today
from database import DatabaseManager
from order_execution import OrderExecutionEngine
from indicators import wilder_rsi, sma_rsi, volume_expanded, TechnicalIndicators
from scorecard import summarize_closed, trade_pnl, heartbeat_line
from options_chain_builder import DynamicOptionsChainBuilder
from strategy_brain import VolumeExpansionGate, StrategyBrain


class TestAlgoEngineCore(unittest.TestCase):
    def test_paise_conversion(self):
        raw_paise_tick = 9965.0
        parsed_price = raw_paise_tick / 100.0 if raw_paise_tick > 500 else float(raw_paise_tick)
        self.assertEqual(parsed_price, 99.65)

    def test_risk_manager_limit(self):
        risk = RiskManager(max_daily_loss_inr=5000.0, max_consecutive_losses=3)
        risk.register_trade_result(-2000.0)
        self.assertEqual(risk.trading_halted, False)
        risk.register_trade_result(-3500.0)
        self.assertEqual(risk.trading_halted, True)

    def test_min_loss_caps_are_tighter_than_old_defaults(self):
        self.assertLessEqual(RISK["max_daily_loss_inr"], 2000.0)
        self.assertLessEqual(RISK["max_consecutive_losses"], 3)
        self.assertFalse(RISK["enable_choppy_entries"])
        self.assertLessEqual(RISK["entry_cutoff_hhmm"], 1430)

    def test_paper_exit_updates_open_row_and_does_not_insert(self):
        class DummyAPI:
            def ltpData(self, *a, **k):
                return {"status": True, "data": {"ltp": 120.0}}

            def placeOrder(self, *a, **k):
                raise AssertionError("live order should not run in paper")

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = DatabaseManager(path)
            eng = OrderExecutionEngine(DummyAPI(), db, paper_trading=True)
            oid = eng.execute_entry(
                "NIFTY26AUG24500CE", "123", 65, "NFO", 120.0, 146.4, 108.0, index_name="NIFTY"
            )
            self.assertTrue(oid)
            self.assertEqual(db.count_open_trades(), 1)
            row = db.fetch_one("SELECT id, qty, status FROM trades WHERE status = 'OPEN'")
            self.assertEqual(int(row["qty"]), 65)
            ok = eng.execute_exit(row["id"], "NIFTY26AUG24500CE", "123", 65, "NFO", 125.0, reason="TARGET_HIT")
            self.assertTrue(ok)
            self.assertEqual(db.count_open_trades(), 0)
            eng.execute_order("X", "1", 65, trans_type="SELL", exchange="NFO", price=1)
            self.assertEqual(db.fetch_one("SELECT COUNT(*) FROM trades")[0], 1)
        finally:
            os.remove(path)

    def test_wilder_rsi_differs_from_sma_and_matches_recursive_seed(self):
        closes = [
            44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42,
            45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00,
            46.03, 46.41, 46.22, 45.64,
        ]
        self.assertEqual(wilder_rsi(closes[:14]), 50.0)
        w = wilder_rsi(closes)
        s = sma_rsi(closes)
        self.assertNotAlmostEqual(w, s, places=4)
        self.assertEqual(wilder_rsi(list(range(1, 30))), 100.0)
        self.assertEqual(wilder_rsi([10.0] * 20), 50.0)
        self.assertEqual(TechnicalIndicators.calculate_rsi(closes), w)

        period = 14
        gains, losses = [], []
        for i in range(1, len(closes)):
            ch = closes[i] - closes[i - 1]
            gains.append(max(ch, 0.0))
            losses.append(max(-ch, 0.0))
        avg_g = sum(gains[:period]) / period
        avg_l = sum(losses[:period]) / period
        for i in range(period, len(gains)):
            avg_g = (avg_g * (period - 1) + gains[i]) / period
            avg_l = (avg_l * (period - 1) + losses[i]) / period
        expected = 100.0 - (100.0 / (1.0 + avg_g / avg_l))
        self.assertAlmostEqual(w, expected, places=8)

    def test_volume_expansion_needs_20_bars_and_mult(self):
        self.assertFalse(volume_expanded([100] * 19, 1.5, 20))
        self.assertFalse(volume_expanded([100] * 20, 1.5, 20))
        self.assertTrue(volume_expanded([100] * 19 + [151], 1.5, 20))
        self.assertFalse(volume_expanded([100] * 19 + [149], 1.5, 20))
        self.assertTrue(volume_expanded([100] * 19 + [120], 1.2, 20))
        self.assertFalse(volume_expanded([100] * 19 + [119], 1.2, 20))

    def test_volume_gate_fail_closed_until_subscribe_and_bars(self):
        gate = VolumeExpansionGate()
        self.assertFalse(gate.allows_entry("NIFTY"))
        gate.mark_subscribed("NIFTY", True)
        self.assertFalse(gate.allows_entry("NIFTY"))
        gate.closed_volumes["NIFTY"] = [100] * 7 + [200]
        self.assertTrue(gate.allows_entry("NIFTY"))
        # Average volume is enough for RSI hook; 1.2x is only required for breakout.
        gate.closed_volumes["NIFTY"] = [100] * 8
        self.assertTrue(gate.allows_entry("NIFTY"))
        self.assertFalse(volume_expanded(gate.closed_volumes["NIFTY"], RISK["volume_mult"], 8))
        gate.closed_volumes["NIFTY"] = [100] * 7 + [50]
        self.assertFalse(gate.allows_entry("NIFTY"))

    def test_scorecard_pnl_uses_stored_qty(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = DatabaseManager(path)
            tid = db.log_trade("NIFTY24CE", "1", 100.0, 122.0, 90.0, qty=65, exchange="NFO", index_name="NIFTY")
            db.close_trade(tid, 110.0, "TARGET_HIT")
            tid2 = db.log_trade("SENSEX24PE", "2", 200.0, 244.0, 180.0, qty=20, exchange="BFO", index_name="SENSEX")
            db.close_trade(tid2, 180.0, "STOP_LOSS_HIT")
            rows = db.fetch_all("SELECT * FROM trades ORDER BY id")
            self.assertEqual(trade_pnl(rows[0]), 10.0 * 65)
            self.assertEqual(trade_pnl(rows[1]), -20.0 * 20)
            stats = summarize_closed(rows)
            self.assertEqual(stats["trades"], 2)
            self.assertEqual(stats["wins"], 1)
            self.assertEqual(stats["losses"], 1)
            self.assertAlmostEqual(stats["total_pnl"], 650 - 400)
            line = heartbeat_line(db)
            self.assertIn("PnL", line)
            self.assertIn("open 0", line)
        finally:
            os.remove(path)

    def test_nearest_future_resolution(self):
        builder = DynamicOptionsChainBuilder(index_name="NIFTY", smart_api=None)
        today = datetime.now().strftime("%d%b%Y").upper()
        builder.scrip_master_data = [
            {
                "name": "NIFTY",
                "instrumenttype": "FUTIDX",
                "exch_seg": "NFO",
                "symbol": "NIFTYFUT",
                "token": "99999",
                "expiry": today,
            },
            {
                "name": "BANKNIFTY",
                "instrumenttype": "FUTIDX",
                "exch_seg": "NFO",
                "symbol": "BANKNIFTYFUT",
                "token": "111",
                "expiry": today,
            },
        ]
        fut = builder.get_nearest_expiry_future()
        self.assertIsNotNone(fut)
        self.assertEqual(fut["token"], "99999")
        self.assertEqual(fut["exchange_type"], 2)

    def test_volume_config_flags_present(self):
        self.assertTrue(RISK["require_volume_expansion"])
        self.assertTrue(RISK["enable_volume_breakout"])
        self.assertTrue(RISK["enable_volume_breakout_in_chop"])
        self.assertEqual(RISK["volume_sma_bars"], 8)
        self.assertEqual(RISK["volume_mult"], 1.2)
        self.assertEqual(RISK["volume_hook_mult"], 1.0)
        self.assertGreaterEqual(RISK["volume_ok_hold_sec"], 300)
        self.assertEqual(RISK["signal_bar_sec"], 300)
        self.assertGreaterEqual(RISK["breakout_max_age_sec"], 600)
        self.assertTrue(RISK["trend_cont_requires_expansion"])
        self.assertGreaterEqual(RISK["paper_max_daily_entries"], 12)
        self.assertLessEqual(RISK["max_trend_entries_per_day"], RISK["paper_max_daily_entries"])
        from config import PAPER_TRADING, daily_entry_cap, signal_bar_sec
        self.assertEqual(signal_bar_sec(), 300)
        if PAPER_TRADING:
            self.assertEqual(daily_entry_cap(), RISK["paper_max_daily_entries"])
        else:
            self.assertEqual(daily_entry_cap(), RISK["max_daily_entries"])

    def test_volume_gate_ltq_fallback_and_sticky_hold(self):
        import time as time_mod
        from config import signal_bar_sec

        gate = VolumeExpansionGate()
        gate.mark_subscribed("NIFTY", True)
        now = time_mod.time()
        bar = signal_bar_sec()
        gate.last_bar_time["NIFTY"] = now
        gate.last_bar_bucket["NIFTY"] = 100
        gate.last_session_vol["NIFTY"] = 1000.0
        gate.on_fut_tick("NIFTY", volume_traded_today=1000.0, last_traded_qty=12.0)
        self.assertEqual(gate.forming_vol["NIFTY"], 12.0)

        gate.forming_vol["NIFTY"] = 80.0
        gate.last_bar_time["NIFTY"] = now - (bar + 1)
        gate.last_bar_bucket["NIFTY"] = 99
        gate.on_fut_tick("NIFTY", volume_traded_today=1000.0, last_traded_qty=4.0)
        self.assertEqual(gate.closed_volumes["NIFTY"][-1], 80.0)

        gate.volume_ok_until["NIFTY"] = time_mod.time() + 60
        gate.closed_volumes["NIFTY"] = [100] * 19 + [50]
        self.assertTrue(gate.allows_entry("NIFTY"))

    def test_volume_breakout_consume_once_and_skip_choppy(self):
        import time as time_mod

        class FakeAPI:
            def ltpData(self, *a, **k):
                return {"status": True, "data": {"ltp": 100.0}}

        class FakeOM:
            def __init__(self):
                self.calls = []
                self.smart_api = FakeAPI()

            def execute_entry(self, **kwargs):
                self.calls.append(kwargs)
                return "oid"

        class FakeBuilder:
            def get_nearest_expiry_contract(self, spot, instrument_type="CE"):
                return {
                    "symbol": "NIFTYCE",
                    "token": "1",
                    "lotsize": 65,
                    "exchange": "NFO",
                }

        om = FakeOM()
        brain = StrategyBrain(
            order_engine=om,
            options_builders={"26000": FakeBuilder()},
            db_manager=None,
        )
        brain.volume_gate.mark_subscribed("NIFTY", True)
        cfg = INDICES_CONFIG["NIFTY"]

        gate = brain.volume_gate
        gate.breakout_event["NIFTY"] = time_mod.time()
        self.assertTrue(gate.consume_breakout("NIFTY"))
        self.assertFalse(gate.consume_breakout("NIFTY"))

        gate.breakout_event["NIFTY"] = time_mod.time()
        fired = brain._try_volume_breakout("NIFTY", 101.0, 100.0, "BULLISH", cfg)
        self.assertTrue(fired)
        self.assertEqual(om.calls[0]["entry_reason"], "VOLUME_BREAKOUT")
        self.assertFalse(brain._try_volume_breakout("NIFTY", 101.0, 100.0, "BULLISH", cfg))
        self.assertEqual(len(om.calls), 1)

        gate.breakout_event["NIFTY"] = time_mod.time()
        skipped = brain._try_volume_breakout("NIFTY", 100.0, 100.0, "CHOPPY", cfg)
        self.assertFalse(skipped)
        self.assertEqual(len(om.calls), 1)

        gate.breakout_event["NIFTY"] = time_mod.time()
        chop_up = brain._try_volume_breakout("NIFTY", 101.0, 100.0, "CHOPPY", cfg)
        self.assertTrue(chop_up)
        self.assertEqual(om.calls[-1]["entry_reason"], "VOLUME_BREAKOUT")
        self.assertEqual(len(om.calls), 2)

        class EmptyBuilder:
            def get_nearest_expiry_contract(self, spot, instrument_type="CE"):
                return None

        brain.options_builders = {"26000": EmptyBuilder()}
        gate.breakout_event["NIFTY"] = time_mod.time()
        missed = brain._try_volume_breakout("NIFTY", 99.0, 100.0, "CHOPPY", cfg)
        self.assertFalse(missed)
        self.assertIsNotNone(gate.breakout_event["NIFTY"])

    def test_liquidity_does_not_invent_2pct_spread(self):
        builder = DynamicOptionsChainBuilder(index_name="NIFTY", smart_api=None)
        q_data = {"tradeVolume": 202909525.0}
        self.assertTrue(builder._liquidity_ok(q_data, 80.0, "NIFTY25AUG2624250PE"))
        deep = {
            "tradeVolume": 10000,
            "depth": {
                "buy": [{"price": 79.5, "quantity": 10}],
                "sell": [{"price": 80.5, "quantity": 10}],
            },
        }
        self.assertTrue(builder._liquidity_ok(deep, 80.0, "NIFTYPE"))
        wide = {
            "tradeVolume": 10000,
            "depth": {
                "buy": [{"price": 70.0}],
                "sell": [{"price": 90.0}],
            },
        }
        self.assertFalse(builder._liquidity_ok(wide, 80.0, "NIFTYPE"))
        bid, ask = builder._bid_ask(deep, 80.0)
        self.assertEqual(bid, 79.5)
        self.assertEqual(ask, 80.5)

    def test_scorecard_by_entry_reason(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = DatabaseManager(path)
            tid = db.log_trade(
                "NIFTY24CE", "1", 100.0, 122.0, 90.0,
                qty=65, exchange="NFO", index_name="NIFTY", entry_reason="VOLUME_BREAKOUT",
            )
            db.close_trade(tid, 110.0, "TARGET_HIT")
            rows = db.fetch_all("SELECT * FROM trades")
            stats = summarize_closed(rows)
            self.assertIn("VOLUME_BREAKOUT", stats["by_entry"])
            self.assertEqual(stats["by_entry"]["VOLUME_BREAKOUT"]["n"], 1)
        finally:
            os.remove(path)

    def test_regime_uses_rolling_mean_not_full_session(self):
        brain = StrategyBrain(order_engine=None, options_builders={}, db_manager=None)
        cfg = INDICES_CONFIG["NIFTY"]
        # Long flat session then a lift: old full-session mean would stay CHOPPY.
        flat = [24250.0] * 80
        lift = [24250.0 + i * 1.5 for i in range(1, 25)]
        closed = flat + lift
        trend, ema9, ema21, loc = brain._classify_regime(closed, cfg)
        self.assertEqual(trend, "BULLISH")
        self.assertGreater(ema9, ema21)
        self.assertGreater(closed[-1], loc)

        drop = [24280.0 - i * 1.5 for i in range(1, 25)]
        closed_dn = [24280.0] * 80 + drop
        trend_dn, _, _, _ = brain._classify_regime(closed_dn, cfg)
        self.assertEqual(trend_dn, "BEARISH")

        chop = [24250.0 + (8 if i % 2 == 0 else -8) for i in range(40)]
        trend_ch, _, _, _ = brain._classify_regime(chop, cfg)
        self.assertEqual(trend_ch, "CHOPPY")

        # Live 11:09 IST: ema spread 2.8 used to miss the 4.0 min and stay CHOPPY.
        grind = [24259.0 + i * 0.35 for i in range(30)]
        grind[-1] = 24265.35
        live_trend, live_e9, live_e21, _ = brain._classify_regime(grind, cfg)
        self.assertGreater(live_e9, live_e21)
        self.assertEqual(live_trend, "BULLISH")

    def test_trend_cont_buys_ce_when_rsi_already_above_50(self):
        class FakeAPI:
            def ltpData(self, *a, **k):
                return {"status": True, "data": {"ltp": 100.0}}

        class FakeOM:
            def __init__(self):
                self.calls = []
                self.smart_api = FakeAPI()

            def execute_entry(self, **kwargs):
                self.calls.append(kwargs)
                return "oid"

        class FakeBuilder:
            def get_nearest_expiry_contract(self, spot, instrument_type="CE"):
                return {"symbol": "NIFTYCE", "token": "1", "lotsize": 65, "exchange": "NFO"}

        om = FakeOM()
        brain = StrategyBrain(
            order_engine=om,
            options_builders={"26000": FakeBuilder()},
            db_manager=None,
        )
        brain.volume_gate.mark_subscribed("NIFTY", True)
        # Flat average volume is enough for old hook gate, but TREND_CONT now needs expansion.
        brain.volume_gate.closed_volumes["NIFTY"] = [100] * 8
        cfg = INDICES_CONFIG["NIFTY"]
        blocked = brain._try_trend_entries(
            "NIFTY", 24265.0, 24262.0, "BULLISH", cfg, last_rsi=61.0, current_rsi=61.5
        )
        self.assertFalse(blocked)
        self.assertEqual(len(om.calls), 0)

        brain.volume_gate.closed_volumes["NIFTY"] = [100] * 7 + [120]
        fired = brain._try_trend_entries(
            "NIFTY", 24265.0, 24262.0, "BULLISH", cfg, last_rsi=61.0, current_rsi=61.5
        )
        self.assertTrue(fired)
        self.assertEqual(om.calls[0]["entry_reason"], "TREND_CONT")

    def test_signal_bars_close_on_5min_not_1min(self):
        import time as time_mod
        from config import signal_bar_bucket, signal_bar_sec

        brain = StrategyBrain(order_engine=None, options_builders={}, db_manager=None)
        now = time_mod.time()
        cur_bucket = signal_bar_bucket(now)
        brain.last_candle_times["NIFTY"] = now
        brain.last_signal_buckets["NIFTY"] = cur_bucket
        brain.price_histories["NIFTY"] = [24200.0 + i for i in range(25)]
        # Same signal bucket: only updates forming close
        before = list(brain.price_histories["NIFTY"])
        brain.evaluate_tick("NIFTY", 24300.0)
        self.assertEqual(len(brain.price_histories["NIFTY"]), len(before))
        self.assertEqual(brain.price_histories["NIFTY"][-1], 24300.0)

        # Force a signal-bar roll via elapsed time + prior bucket
        brain.last_candle_times["NIFTY"] = now - (signal_bar_sec() + 1)
        brain.last_signal_buckets["NIFTY"] = cur_bucket - 1
        n = len(brain.price_histories["NIFTY"])
        brain.evaluate_tick("NIFTY", 24310.0)
        self.assertEqual(len(brain.price_histories["NIFTY"]), n + 1)
        self.assertEqual(brain.price_histories["NIFTY"][-1], 24310.0)

    def test_trend_soft_cap_still_allows_volume_breakout(self):
        class FakeAPI:
            def ltpData(self, *a, **k):
                return {"status": True, "data": {"ltp": 100.0}}

        class FakeOM:
            def __init__(self):
                self.calls = []
                self.smart_api = FakeAPI()

            def execute_entry(self, **kwargs):
                self.calls.append(kwargs)
                return "oid"

        class FakeBuilder:
            def get_nearest_expiry_contract(self, spot, instrument_type="CE"):
                return {"symbol": "NIFTYCE", "token": "1", "lotsize": 65, "exchange": "NFO"}

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = DatabaseManager(path)
            today = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for i in range(RISK["max_trend_entries_per_day"]):
                tid = db.log_trade(
                    f"NIFTYCE{i}", str(i), 100.0, 122.0, 90.0,
                    qty=65, exchange="NFO", index_name="NIFTY", entry_reason="TREND_CONT",
                )
                db.close_trade(tid, 100.0, "TIME_STOP")  # flat: avoid circuit-breaker halt
            self.assertEqual(
                db.count_entries_today(entry_reasons=("TREND_CONT", "RSI_HOOK")),
                RISK["max_trend_entries_per_day"],
            )

            risk = RiskManager(db_manager=db)
            blocked = risk.assess_order_safety(
                {"qty": 65, "index_name": "NIFTY", "symbol": "X", "entry_reason": "TREND_CONT"},
                estimated_premium=100.0,
            )
            self.assertFalse(blocked)

            allowed = risk.assess_order_safety(
                {"qty": 65, "index_name": "NIFTY", "symbol": "Y", "entry_reason": "VOLUME_BREAKOUT"},
                estimated_premium=100.0,
            )
            self.assertTrue(allowed)

            om = FakeOM()
            brain = StrategyBrain(
                order_engine=om,
                options_builders={"26000": FakeBuilder()},
                db_manager=db,
                risk_manager=risk,
            )
            brain.volume_gate.mark_subscribed("NIFTY", True)
            import time as time_mod
            brain.volume_gate.breakout_event["NIFTY"] = time_mod.time()
            cfg = INDICES_CONFIG["NIFTY"]
            fired = brain._try_volume_breakout("NIFTY", 101.0, 100.0, "BULLISH", cfg)
            self.assertTrue(fired)
            self.assertEqual(om.calls[0]["entry_reason"], "VOLUME_BREAKOUT")
        finally:
            os.remove(path)

    def test_duplicate_fut_ticks_do_not_inflate_volume(self):
        gate = VolumeExpansionGate()
        gate.mark_subscribed("NIFTY", True)
        gate.last_bar_time["NIFTY"] = 1.0
        gate.last_bar_bucket["NIFTY"] = 1
        gate.last_session_vol["NIFTY"] = 1000.0
        gate.on_fut_tick("NIFTY", volume_traded_today=1000.0, last_traded_qty=12.0, sequence_number=10)
        gate.on_fut_tick("NIFTY", volume_traded_today=1000.0, last_traded_qty=12.0, sequence_number=10)
        gate.on_fut_tick("NIFTY", volume_traded_today=1000.0, last_traded_qty=12.0)
        self.assertEqual(gate.forming_vol["NIFTY"], 12.0)
        gate.on_fut_tick("NIFTY", volume_traded_today=1000.0, last_traded_qty=12.0, sequence_number=11)
        self.assertEqual(gate.forming_vol["NIFTY"], 24.0)

    def test_paper_exit_refuses_zero_fill(self):
        class DummyAPI:
            def ltpData(self, *a, **k):
                return {"status": True, "data": {"ltp": 0.0}}

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = DatabaseManager(path)
            eng = OrderExecutionEngine(DummyAPI(), db, paper_trading=True)
            oid = eng.execute_entry(
                "NIFTY26AUG24500CE", "123", 65, "NFO", 120.0, 146.4, 108.0, index_name="NIFTY"
            )
            self.assertTrue(oid)
            row = db.fetch_one("SELECT id FROM trades WHERE status = 'OPEN'")
            ok = eng.execute_exit(row["id"], "NIFTY26AUG24500CE", "123", 65, "NFO", 0.0, reason="EOD_SQUAREOFF")
            self.assertFalse(ok)
            self.assertEqual(db.count_open_trades(), 1)
        finally:
            os.remove(path)


class FakeCandleAPI:
    """Emits synthetic 5-min candles ending at the last closed IST bucket."""

    def __init__(self, bars=60, base=24000.0, vol=100000.0):
        self.bars = bars
        self.base = base
        self.vol = vol
        self.calls = []

    def getCandleData(self, params):
        from datetime import datetime, timedelta, timezone

        self.calls.append(params)
        ist = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(timezone.utc).astimezone(ist)
        bar = timedelta(seconds=300)
        # Align to the current (still forming) bucket start.
        secs = now.hour * 3600 + now.minute * 60 + now.second
        cur_start = now - timedelta(seconds=secs % 300, microseconds=now.microsecond)
        data = []
        for i in range(self.bars, -1, -1):  # includes the forming bar
            ts = cur_start - bar * i
            px = self.base + (self.bars - i)
            data.append([ts.isoformat(), px, px + 5, px - 5, px + 1, self.vol])
        return {"status": True, "data": data}


class HistorySeedTests(unittest.TestCase):
    def test_seed_fills_price_and_volume_history(self):
        import history_seeder
        from config import signal_bar_bucket

        brain = StrategyBrain(order_engine=None, options_builders={})
        api = FakeCandleAPI()
        futs = {"NIFTY": {"token": "5001", "exchange": "NFO", "symbol": "NIFTY26AUGFUT"}}
        history_seeder.seed_all(api, brain, futs, symbols=["NIFTY"])

        hist = brain.price_histories["NIFTY"]
        # 60 closed bars + 1 placeholder for the forming bar.
        self.assertEqual(len(hist), 61)
        self.assertGreaterEqual(len(hist) - 1, 22)
        self.assertEqual(hist[-1], hist[-2])
        self.assertEqual(len(brain.closed_rsi["NIFTY"]), 5)
        self.assertNotEqual(brain.last_closed_rsi["NIFTY"], 50.0)

        gate = brain.volume_gate
        self.assertGreaterEqual(len(gate.closed_volumes["NIFTY"]), int(RISK["volume_sma_bars"]))
        self.assertIsNone(gate.last_session_vol["NIFTY"])
        # Seeded history must never carry a stale breakout into the live session.
        self.assertIsNone(gate.breakout_event["NIFTY"])
        self.assertFalse(gate.has_fresh_breakout("NIFTY"))
        # Bar clock parked on the current bucket so the first tick is not a close.
        self.assertEqual(brain.last_signal_buckets["NIFTY"], signal_bar_bucket(time.time()))

    def test_forming_bar_is_excluded(self):
        import history_seeder

        api = FakeCandleAPI(bars=30)
        rows = history_seeder.fetch_candles(api, "NSE", "26000", "FIVE_MINUTE")
        closed = history_seeder._drop_forming_bar(rows)
        self.assertEqual(len(closed), len(rows) - 1)

    def test_partial_first_volume_bar_is_discarded(self):
        gate = VolumeExpansionGate()
        gate.mark_subscribed("NIFTY", True)
        gate.on_fut_tick("NIFTY", volume_traded_today=1000.0)
        gate.on_fut_tick("NIFTY", volume_traded_today=5000.0)
        gate.last_bar_time["NIFTY"] = time.time() - 400
        gate.on_fut_tick("NIFTY", volume_traded_today=6000.0)
        self.assertEqual(gate.closed_volumes["NIFTY"], [])
        self.assertFalse(gate.partial_first_bar["NIFTY"])


class PerIndexCapTests(unittest.TestCase):
    def _db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return DatabaseManager(path), path

    def test_per_index_daily_cap_blocks_one_index_only(self):
        from config import index_daily_entry_cap

        db, path = self._db()
        try:
            cap = index_daily_entry_cap()
            for i in range(cap):
                tid = db.log_trade(f"NIFTY26AUG245{i}0CE", "1", 100.0, 122.0, 90.0,
                                   qty=65, exchange="NFO", index_name="NIFTY",
                                   entry_reason="VOLUME_BREAKOUT")
                # Close them flat so open-trade caps and the loss breaker stay clear
                # and only the per-index daily cap is under test.
                db.close_trade(tid, 100.0, "TARGET_HIT")
            self.assertEqual(db.count_entries_today(index_name="NIFTY"), cap)
            self.assertEqual(db.count_entries_today(index_name="SENSEX"), 0)

            rm = RiskManager(db_manager=db)
            # NIFTY is at its per-index cap...
            self.assertFalse(rm.assess_order_safety(
                {"qty": 65, "index_name": "NIFTY", "symbol": "X", "entry_reason": "VOLUME_BREAKOUT"},
                estimated_premium=100.0,
            ))
            # ...but SENSEX still has its own budget.
            self.assertTrue(rm.assess_order_safety(
                {"qty": 20, "index_name": "SENSEX", "symbol": "Y", "entry_reason": "VOLUME_BREAKOUT"},
                estimated_premium=100.0,
            ))
        finally:
            os.remove(path)

    def test_index_activity_today_reports_per_index(self):
        db, path = self._db()
        try:
            tid = db.log_trade("NIFTY26AUG24500CE", "1", 100.0, 122.0, 90.0,
                               qty=65, exchange="NFO", index_name="NIFTY")
            db.close_trade(tid, 110.0, "TARGET_HIT")
            db.log_trade("SENSEX26AUG81000PE", "2", 200.0, 244.0, 180.0,
                         qty=20, exchange="BFO", index_name="SENSEX")
            act = db.index_activity_today()
            self.assertEqual(act["NIFTY"]["entries"], 1)
            self.assertEqual(act["NIFTY"]["closed"], 1)
            self.assertAlmostEqual(act["NIFTY"]["pnl"], 10.0 * 65)
            self.assertEqual(act["SENSEX"]["open"], 1)
            self.assertAlmostEqual(act["SENSEX"]["pnl"], 0.0)
        finally:
            os.remove(path)


class FeedGapTests(unittest.TestCase):
    def test_volume_gap_drops_partial_bar_and_sticky_state(self):
        gate = VolumeExpansionGate()
        gate.mark_subscribed("NIFTY", True)
        gate.closed_volumes["NIFTY"] = [100.0] * 8
        gate.last_bar_time["NIFTY"] = time.time() - 1800  # 6 bars of silence
        gate.last_bar_bucket["NIFTY"] = -1
        gate.volume_ok_until["NIFTY"] = time.time() + 600
        gate.breakout_event["NIFTY"] = time.time()

        gate.on_fut_tick("NIFTY", volume_traded_today=1000.0)
        # No phantom/partial bar appended.
        self.assertEqual(len(gate.closed_volumes["NIFTY"]), 8)
        # Sticky expansion from before the gap must not survive it.
        self.assertFalse(gate.has_fresh_breakout("NIFTY"))
        self.assertEqual(gate.volume_ok_until["NIFTY"], 0.0)

    def test_absurd_gap_is_reported_as_a_clock_reset(self):
        import logging
        gate = VolumeExpansionGate()
        gate.mark_subscribed("NIFTY", True)
        gate.last_bar_time["NIFTY"] = 1.0   # epoch -> millions of "bars"
        gate.last_bar_bucket["NIFTY"] = 1
        with self.assertLogs(level="WARNING") as captured:
            gate.on_fut_tick("NIFTY", volume_traded_today=1000.0)
        joined = " ".join(captured.output)
        self.assertIn("clock reset", joined)
        self.assertNotIn("59600", joined)

    def test_price_gap_pauses_entries(self):
        brain = StrategyBrain(order_engine=None, options_builders={})
        brain.price_histories["NIFTY"] = [24000.0 + i for i in range(30)]
        brain.last_candle_times["NIFTY"] = time.time() - 1800  # 6 bars missed
        brain.last_signal_buckets["NIFTY"] = -1
        brain.evaluate_tick("NIFTY", 24100.0)
        self.assertGreater(brain.stale_bars["NIFTY"], 0)


class ISTTimeTests(unittest.TestCase):
    def test_ist_helpers_are_offset_from_utc(self):
        from datetime import datetime, timezone
        from ist_time import ist_now, ist_today, ist_hhmm

        utc = datetime.now(timezone.utc)
        delta = (ist_now().replace(tzinfo=None) - utc.replace(tzinfo=None)).total_seconds()
        self.assertAlmostEqual(delta, 5.5 * 3600, delta=5)
        self.assertEqual(len(ist_today()), 10)
        self.assertTrue(0 <= ist_hhmm() <= 2359)

    def test_db_stamps_trades_in_ist(self):
        from ist_time import ist_today

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            db = DatabaseManager(path)
            db.log_trade("NIFTY26AUG24500CE", "1", 100.0, 122.0, 90.0,
                         qty=65, exchange="NFO", index_name="NIFTY")
            row = db.fetch_one("SELECT entry_time FROM trades")
            self.assertTrue(str(row[0]).startswith(ist_today()))
            self.assertEqual(db.count_entries_today(), 1)
        finally:
            os.remove(path)


class TrailLadderTests(unittest.TestCase):
    """The ladder must never turn a winner into a loser, which is the whole
    reason the two lowest tiers were left at +4% / +8%."""

    def setUp(self):
        from risk_monitors import _trailed_stop
        self.f = _trailed_stop
        self.E = 100.0
        self.INIT_SL = 90.0

    def test_low_tiers_unchanged_from_previous_behaviour(self):
        # Below +4% nothing moves; the -10% stop stands.
        self.assertEqual(self.f(self.E, 103.0, 103.0, self.INIT_SL), self.INIT_SL)
        # +4% -> breakeven, +8% -> entry * 1.02, exactly as before.
        self.assertAlmostEqual(self.f(self.E, 104.0, 104.0, self.INIT_SL), 100.0)
        self.assertAlmostEqual(self.f(self.E, 108.0, 108.0, self.INIT_SL), 102.0)

    def test_high_tiers_capture_more_of_the_peak(self):
        # Old rule locked a flat 50% of peak gain all the way up.
        old_at_30 = self.E + 0.50 * (130.0 - self.E)
        new_at_30 = self.f(self.E, 130.0, 130.0, self.INIT_SL)
        self.assertGreater(new_at_30, old_at_30)
        self.assertAlmostEqual(new_at_30, 122.5)  # 75% of a +30% peak

    def test_never_lowers_an_existing_stop(self):
        self.assertEqual(self.f(self.E, 130.0, 130.0, 125.0), 125.0)

    def test_monotonic_in_peak(self):
        prev = 0.0
        for peak in range(100, 145):
            sl = self.f(self.E, float(peak), float(peak), self.INIT_SL)
            self.assertGreaterEqual(sl, prev)
            prev = sl

    def test_tier_jump_between_polls_takes_the_best_lock(self):
        # A gap-up straight from entry to +30% must not stop at the +4% tier.
        self.assertAlmostEqual(self.f(self.E, 130.0, 130.0, self.INIT_SL), 122.5)

    def test_locked_gain_never_exceeds_the_peak(self):
        for peak in range(101, 160):
            self.assertLess(self.f(self.E, float(peak), float(peak), self.INIT_SL), float(peak))


class TargetConfigTests(unittest.TestCase):
    def test_target_and_stop_give_at_least_3to1(self):
        for symbol, cfg in INDICES_CONFIG.items():
            reward = cfg["trending_target_mult"] - 1.0
            risk = 1.0 - cfg["trending_sl_mult"]
            self.assertGreaterEqual(reward / risk, 3.0, f"{symbol} R:R below 3:1")

    def test_top_trail_tier_sits_below_the_target(self):
        # If the last tier triggered at or above the target the target would
        # always fire first and the tier would be dead code.
        from config import RISK
        top = max(float(t["at"]) for t in RISK["trail_tiers"])
        for symbol, cfg in INDICES_CONFIG.items():
            self.assertLess(top, cfg["trending_target_mult"], f"{symbol} top tier >= target")


class DocsMatchConfigTests(unittest.TestCase):
    """Docs that disagree with config are worse than no docs. Pin the numbers
    a reader would actually act on."""

    @staticmethod
    def _readme():
        with io.open("README.md", encoding="utf-8") as f:
            return f.read()

    def test_readme_documents_current_target_and_stop(self):
        readme = self._readme()
        for symbol, cfg in INDICES_CONFIG.items():
            tgt = int(round((cfg["trending_target_mult"] - 1) * 100))
            sl = int(round((1 - cfg["trending_sl_mult"]) * 100))
            self.assertIn(f"+{tgt}%", readme, f"{symbol} target not in README")
            self.assertIn(f"−{sl}%", readme, f"{symbol} stop not in README")

    def test_readme_documents_every_trail_tier(self):
        readme = self._readme()
        for tier in RISK["trail_tiers"]:
            trigger = f"+{int(round((float(tier['at']) - 1) * 100))}%"
            self.assertIn(trigger, readme, f"trail tier {trigger} not in README")

    def test_readme_documents_session_and_caps(self):
        readme = self._readme()
        for value in (
            RISK["session_start_hhmm"],
            RISK["entry_cutoff_hhmm"],
            RISK["eod_squareoff_hhmm"],
            RISK["max_daily_entries_per_index"],
            RISK["paper_max_daily_entries_per_index"],
        ):
            self.assertIn(str(value), readme, f"{value} not documented in README")


class OrderBookAPI:
    """Broker stub whose orderBook() replays a scripted status sequence."""

    def __init__(self, statuses, filled_qty=65, avg_price=101.0, place_ok=True):
        self.statuses = list(statuses)
        self.filled_qty = filled_qty
        self.avg_price = avg_price
        self.place_ok = place_ok
        self.placed = []

    def placeOrder(self, params):
        self.placed.append(params)
        return "ORD1" if self.place_ok else None

    def orderBook(self):
        status = self.statuses.pop(0) if self.statuses else "open"
        return {"status": True, "data": [{
            "orderid": "ORD1", "orderstatus": status,
            "filledshares": self.filled_qty if status == "complete" else 0,
            "averageprice": self.avg_price if status == "complete" else 0,
        }]}

    def ltpData(self, exchange, symbol, token):
        return {"status": True, "data": {"ltp": 100.0}}


class FillConfirmationTests(unittest.TestCase):
    def _db(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        return DatabaseManager(path), path

    def test_rejected_order_opens_no_position(self):
        from broker_orders import confirm_fill
        api = OrderBookAPI(["rejected"] * 4)
        self.assertFalse(confirm_fill(api, "ORD1", timeout_sec=0.2, poll_sec=0.05).is_filled)
        db, path = self._db()
        try:
            eng = OrderExecutionEngine(api, db, paper_trading=False)
            self.assertIsNone(eng.execute_entry(
                "NIFTY26AUG24500CE", "1", 65, "NFO", 100.0, 130.0, 90.0, index_name="NIFTY"))
            self.assertEqual(db.count_open_trades(), 0)
        finally:
            os.remove(path)

    def test_pending_order_reports_timeout_not_a_fill(self):
        from broker_orders import confirm_fill
        # An order that never reaches a terminal state must be UNKNOWN:
        # not filled (do not open a position) and not dead (it may still execute).
        api = OrderBookAPI(["open"] * 20)
        result = confirm_fill(api, "ORD1", timeout_sec=0.2, poll_sec=0.05)
        self.assertEqual(result.status, "timeout")
        self.assertFalse(result.is_filled)
        self.assertFalse(result.is_dead)

    def test_unknown_order_id_is_not_a_fill(self):
        from broker_orders import confirm_fill
        api = OrderBookAPI(["complete"])
        result = confirm_fill(api, "SOME_OTHER_ID", timeout_sec=0.2, poll_sec=0.05)
        self.assertFalse(result.is_filled)

    def test_fill_uses_broker_price_and_records_slippage(self):
        db, path = self._db()
        try:
            # Intended 100.0, broker fills at 103.0 -> 3.0 slippage.
            api = OrderBookAPI(["complete"], filled_qty=65, avg_price=103.0)
            eng = OrderExecutionEngine(api, db, paper_trading=False)
            self.assertTrue(eng.execute_entry(
                "NIFTY26AUG24500CE", "1", 65, "NFO", 100.0, 130.0, 90.0, index_name="NIFTY"))
            row = db.fetch_one(
                "SELECT entry_price, intended_price, slippage, target_price, stop_loss_price "
                "FROM trades WHERE status='OPEN'")
            self.assertAlmostEqual(row["entry_price"], 103.0)
            self.assertAlmostEqual(row["intended_price"], 100.0)
            self.assertAlmostEqual(row["slippage"], 3.0)
            # Targets must be re-derived from the real fill, not the intended price.
            self.assertAlmostEqual(row["target_price"], 133.9, places=1)
            self.assertAlmostEqual(row["stop_loss_price"], 92.7, places=1)
        finally:
            os.remove(path)

    def test_partial_exit_leaves_trade_open(self):
        db, path = self._db()
        try:
            api = OrderBookAPI(["complete"], filled_qty=65, avg_price=100.0)
            eng = OrderExecutionEngine(api, db, paper_trading=False)
            eng.execute_entry("NIFTY26AUG24500CE", "1", 65, "NFO", 100.0, 130.0, 90.0,
                              index_name="NIFTY")
            tid = db.fetch_one("SELECT id FROM trades WHERE status='OPEN'")["id"]
            # Exit fills only 30 of 65 -> must NOT be booked as closed.
            api.statuses = ["complete"]
            api.filled_qty = 30
            self.assertFalse(eng.execute_exit(tid, "NIFTY26AUG24500CE", "1", 65, "NFO",
                                              110.0, reason="TARGET_HIT"))
            self.assertEqual(db.count_open_trades(), 1)
        finally:
            os.remove(path)

    def test_paper_mode_records_zero_slippage(self):
        db, path = self._db()
        try:
            eng = OrderExecutionEngine(OrderBookAPI([]), db, paper_trading=True)
            eng.execute_entry("NIFTY26AUG24500CE", "1", 65, "NFO", 120.0, 156.0, 108.0,
                              index_name="NIFTY", bid=119.0, ask=121.0, spread_pct=1.67)
            row = db.fetch_one("SELECT slippage, entry_spread_pct FROM trades")
            self.assertAlmostEqual(row["slippage"], 0.0)
            self.assertAlmostEqual(row["entry_spread_pct"], 1.67)
        finally:
            os.remove(path)


class SessionKeeperTests(unittest.TestCase):
    def test_relogin_only_on_auth_errors(self):
        from broker_health import SessionKeeper, looks_like_auth_failure
        self.assertTrue(looks_like_auth_failure("Invalid Token"))
        self.assertFalse(looks_like_auth_failure("rate limit exceeded"))
        calls = []
        keeper = SessionKeeper(lambda: calls.append(1) or f"api{len(calls)}",
                               min_interval_sec=0)
        self.assertEqual(keeper.ensure(), "api1")
        self.assertEqual(keeper.ensure(), "api1")          # cached
        keeper.handle_error("rate limit exceeded")
        self.assertEqual(len(calls), 1)                     # no re-login
        keeper.handle_error("Invalid Token")
        self.assertEqual(len(calls), 2)                     # re-login

    def test_failed_relogin_keeps_old_handle(self):
        from broker_health import SessionKeeper
        keeper = SessionKeeper(lambda: "good", min_interval_sec=0)
        keeper.ensure()
        keeper.login_fn = lambda: None
        self.assertEqual(keeper.handle_error("session expired"), "good")


class ViewCompletedTests(unittest.TestCase):
    """The viewer was today-only and host-local; ranges must work and be IST."""

    def setUp(self):
        import sqlite3
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        DatabaseManager(self.path)
        conn = sqlite3.connect(self.path)
        rows = [
            ("2026-08-24", "NIFTY26AUG24500CE", "NIFTY", 65, 100.0, 130.0, "TARGET_HIT"),
            ("2026-08-25", "SENSEX26AUG81000PE", "SENSEX", 20, 200.0, 180.0, "STOP_LOSS_HIT"),
            ("2026-08-26", "NIFTY26AUG24600CE", "NIFTY", 65, 80.0, 104.0, "TARGET_HIT"),
        ]
        for day, sym, idx, qty, ep, xp, xr in rows:
            conn.execute(
                "INSERT INTO trades (symbol, token, qty, exchange, index_name, entry_price,"
                " status, exit_price, exit_reason, entry_reason, timestamp, entry_time)"
                " VALUES (?,?,?,?,?,?,'CLOSED',?,?,'VOLUME_BREAKOUT',?,?)",
                (sym, "1", qty, "NFO", idx, ep, xp, xr, day + " 10:15:00", day + " 10:15:00"),
            )
        conn.execute(
            "INSERT INTO trades (symbol, token, qty, exchange, index_name, entry_price,"
            " status, entry_reason, timestamp, entry_time)"
            " VALUES ('NIFTY26SEP24700CE','9',65,'NFO','NIFTY',90.0,'OPEN','TREND_CONT',"
            "'2026-08-29 10:00:00','2026-08-29 10:00:00')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        os.remove(self.path)

    def _fetch(self, argv):
        import sqlite3
        import view_completed as vc
        args = vc.parse_args(argv + ["--db", self.path])
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            rows, label = vc.fetch(conn, args)
            return rows, label
        finally:
            conn.close()

    def test_all_returns_every_closed_trade(self):
        rows, label = self._fetch(["--all"])
        self.assertEqual(len(rows), 3)
        self.assertEqual(label, "all time")

    def test_single_date(self):
        rows, _ = self._fetch(["--date", "2026-08-25"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "SENSEX26AUG81000PE")

    def test_since_until_is_inclusive_both_ends(self):
        rows, _ = self._fetch(["--since", "2026-08-24", "--until", "2026-08-26"])
        self.assertEqual(len(rows), 3)
        rows, _ = self._fetch(["--since", "2026-08-25", "--until", "2026-08-25"])
        self.assertEqual(len(rows), 1)

    def test_index_filter(self):
        rows, _ = self._fetch(["--all", "--index", "NIFTY"])
        self.assertEqual(len(rows), 2)

    def test_exit_reason_filter(self):
        rows, _ = self._fetch(["--all", "--reason", "TARGET_HIT"])
        self.assertEqual(len(rows), 2)

    def test_open_flag_shows_only_open(self):
        rows, _ = self._fetch(["--all", "--open"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "OPEN")

    def test_default_window_is_seven_ist_days(self):
        import view_completed as vc
        _, _, label = vc.build_query(vc.parse_args([]))
        self.assertIn("last 7 day(s)", label)
        self.assertIn(ist_today(), label)

    def test_newest_first(self):
        rows, _ = self._fetch(["--all"])
        stamps = [r["entry_time"] for r in rows]
        self.assertEqual(stamps, sorted(stamps, reverse=True))

    def test_pnl_uses_stored_qty(self):
        import view_completed as vc
        rows, _ = self._fetch(["--date", "2026-08-24"])
        self.assertAlmostEqual(vc._pnl(rows[0]), (130.0 - 100.0) * 65)


class PreflightTests(unittest.TestCase):
    """The old preflight could never pass: it required TRADING_PIN (nothing reads
    it) and queried a table named trade_history (the table is `trades`)."""

    def test_credentials_accept_the_names_main_actually_reads(self):
        import preflight_check as pf
        saved = {k: os.environ.get(k) for k in
                 ("SMART_API_KEY", "SMARTAPI_KEY", "CLIENT_ID", "PASSWORD", "PIN", "TOTP_SECRET")}
        try:
            for k in saved:
                os.environ.pop(k, None)
            os.environ.update({"SMARTAPI_KEY": "k", "CLIENT_ID": "c",
                               "PIN": "p", "TOTP_SECRET": "t"})
            rep = pf.Report()
            pf.check_credentials(rep)
            self.assertEqual(rep.rows[0][0], pf.PASS, rep.rows[0][2])
        finally:
            for k, v in saved.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v

    def test_missing_credentials_fail(self):
        import preflight_check as pf
        saved = {k: os.environ.get(k) for k in
                 ("SMART_API_KEY", "SMARTAPI_KEY", "CLIENT_ID", "PASSWORD", "PIN", "TOTP_SECRET")}
        try:
            for k in saved:
                os.environ.pop(k, None)
            rep = pf.Report()
            pf.check_credentials(rep)
            self.assertEqual(rep.rows[0][0], pf.FAIL)
        finally:
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_live_path_modules_all_import(self):
        import preflight_check as pf
        rep = pf.Report()
        pf.check_imports(rep)
        self.assertEqual(rep.rows[0][0], pf.PASS, rep.rows[0][2])

    def test_risk_config_passes_current_settings(self):
        import preflight_check as pf
        rep = pf.Report()
        pf.check_risk_config(rep, capital=75000)
        self.assertEqual(rep.failures, [], [r[2] for r in rep.failures])

    def test_capital_too_small_is_a_failure(self):
        import preflight_check as pf
        rep = pf.Report()
        # Rs1500/day against Rs10k is 15% -- not survivable.
        pf.check_risk_config(rep, capital=10000)
        self.assertTrue(rep.failures)

    def test_report_exit_semantics(self):
        import preflight_check as pf
        rep = pf.Report()
        rep.ok("a")
        self.assertFalse(rep.failures)
        rep.warn("b")
        self.assertFalse(rep.failures)   # warnings do not block
        rep.fail("c")
        self.assertTrue(rep.failures)


class ExecutionCostReportTests(unittest.TestCase):
    def test_scorecard_reports_slippage_and_spread(self):
        import sqlite3
        from scorecard import load_trades, summarize_closed
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            DatabaseManager(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO trades (symbol, token, qty, exchange, index_name, entry_price,"
                " status, exit_price, exit_reason, entry_reason, timestamp, entry_time,"
                " intended_price, slippage, entry_spread_pct)"
                " VALUES ('NIFTY26SEP24500CE','1',65,'NFO','NIFTY',101.5,'CLOSED',131.9,"
                "'TARGET_HIT','VOLUME_BREAKOUT','2026-08-29 10:15:00','2026-08-29 10:15:00',"
                "100.0,1.5,1.4)"
            )
            conn.commit()
            conn.close()
            s = summarize_closed(load_trades(DatabaseManager(path)))
            self.assertEqual(s["slippage_n"], 1)
            # Slippage is per unit; the cost is per-unit x qty.
            self.assertAlmostEqual(s["slippage_total"], 1.5 * 65)
            self.assertAlmostEqual(s["avg_spread_pct"], 1.4)
        finally:
            os.remove(path)

    def test_missing_execution_columns_report_none(self):
        import sqlite3
        from scorecard import load_trades, summarize_closed
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            DatabaseManager(path)
            conn = sqlite3.connect(path)
            conn.execute(
                "INSERT INTO trades (symbol, token, qty, exchange, index_name, entry_price,"
                " status, exit_price, exit_reason, entry_reason, timestamp, entry_time)"
                " VALUES ('NIFTY26AUG24500CE','1',65,'NFO','NIFTY',100.0,'CLOSED',110.0,"
                "'TARGET_HIT','VOLUME_BREAKOUT','2026-08-29 10:15:00','2026-08-29 10:15:00')"
            )
            conn.commit()
            conn.close()
            s = summarize_closed(load_trades(DatabaseManager(path)))
            self.assertEqual(s["slippage_n"], 0)
            self.assertIsNone(s["slippage_total"])
        finally:
            os.remove(path)


if __name__ == "__main__":
    unittest.main()
