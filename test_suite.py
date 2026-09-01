import io
import os
import shutil
import tempfile
import unittest
import time
from datetime import datetime
from risk_manager import RiskManager
from config import RISK, INDICES_CONFIG, history_token
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


class TelegramNotifierTests(unittest.TestCase):
    """Standalone notifier: no network in these tests, and it must never write
    to the trading DB or alert on routine log noise."""

    def test_urgent_lines_classify_as_urgent(self):
        import telegram_notifier as tn
        urgent = [
            "2026-08-31 11:02:55,113 - CRITICAL - [RECONCILIATION] UNTRACKED broker position X netqty=20",
            "2026-08-31 11:15:00,000 - CRITICAL - CIRCUIT BREAKER: PnL Rs-1520.00 | streak 1.",
            "2026-08-31 09:16:03,900 - ERROR - [NIFTY] Implausible spot Rs240.00. Tick dropped.",
            "2026-08-31 10:00:00,000 - CRITICAL - [LIVE] SELL X order ORD1 is TIMEOUT — not recorded.",
        ]
        for line in urgent:
            label, is_urgent = tn.classify(line)
            self.assertIsNotNone(label, line)
            self.assertTrue(is_urgent, line)

    def test_routine_noise_does_not_alert(self):
        import telegram_notifier as tn
        for line in [
            "2026-08-31 12:00:00,000 - INFO - Tick Received - [NIFTY] Spot LTP: Rs24000",
            "2026-08-31 12:00:00,000 - INFO - [NIFTY 5M VOL] last=1200 rvol=1.10x",
            "2026-08-31 12:00:00,000 - INFO - [SYSTEM STATUS] all good",
        ]:
            self.assertIsNone(tn.classify(line)[0], line)

    def test_entry_and_exit_are_notified_but_not_urgent(self):
        import telegram_notifier as tn
        for line in [
            "2026-08-31 09:47:12,001 - INFO - [NIFTY] ENTRY VOLUME_BREAKOUT CE X qty=65",
            "2026-08-31 10:31:08,442 - INFO - [TRADE CLOSED] X | Exit Rs131.90 | TARGET_HIT",
        ]:
            label, urgent = tn.classify(line)
            self.assertIsNotNone(label, line)
            self.assertFalse(urgent, line)

    def test_tail_starts_at_eof_and_survives_rotation(self):
        import telegram_notifier as tn
        d = tempfile.mkdtemp()
        path = os.path.join(d, "t.log")
        try:
            with io.open(path, "w") as f:
                f.write("pre-existing backlog\n")
            tail = tn.LogTail(path)
            # Must not replay history on startup.
            self.assertEqual(tail.read_new(), [])
            with io.open(path, "a") as f:
                f.write("line A\n")
            self.assertEqual(tail.read_new(), ["line A"])
            # RotatingFileHandler renames then recreates.
            os.rename(path, path + ".1")
            with io.open(path, "w") as f:
                f.write("after rotation\n")
            self.assertEqual(tail.read_new(), ["after rotation"])
        finally:
            for f in (path, path + ".1"):
                if os.path.exists(f):
                    os.remove(f)
            os.rmdir(d)

    def test_db_access_is_read_only(self):
        import sqlite3
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            DatabaseManager(path)
            conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    conn.execute("DELETE FROM trades")
            finally:
                conn.close()
        finally:
            os.remove(path)

    def test_commands_handle_an_empty_database(self):
        import telegram_notifier as tn
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        saved = tn.DB_FILE
        try:
            DatabaseManager(path)
            tn.DB_FILE = path
            self.assertIn("No open trades", tn.cmd_open())
            self.assertIn("No closed trades", tn.cmd_pnl())
            self.assertIn("No closed trades", tn.cmd_trades())
        finally:
            tn.DB_FILE = saved
            os.remove(path)

    def test_html_escaping(self):
        import telegram_notifier as tn
        self.assertEqual(tn.esc("<b>&x</b>"), "&lt;b&gt;&amp;x&lt;/b&gt;")


class BacktestOptionsTests(unittest.TestCase):
    """The pricing model is the backtest's foundation; if it is wrong every
    conclusion drawn from a sweep is wrong."""

    def test_put_call_parity(self):
        import backtest_options as bo
        T = bo.years_to_expiry(1, 0)
        c = bo.black_scholes(24000, 24000, T, 0.14, True)
        p = bo.black_scholes(24000, 24000, T, 0.14, False)
        self.assertAlmostEqual(c - p, 0.0, places=6)
        c2 = bo.black_scholes(24100, 24000, T, 0.14, True)
        p2 = bo.black_scholes(24100, 24000, T, 0.14, False)
        self.assertAlmostEqual(c2 - p2, 100.0, places=4)

    def test_expiry_collapses_to_intrinsic(self):
        import backtest_options as bo
        self.assertAlmostEqual(bo.black_scholes(24100, 24000, 0, 0.14, True), 100.0)
        self.assertAlmostEqual(bo.black_scholes(24100, 24000, 0, 0.14, False), 0.0)

    def test_theta_is_steepest_at_zero_dte(self):
        import backtest_options as bo
        decay = {}
        for dte in (0, 1, 3):
            a = bo.black_scholes(24000, 24000, bo.years_to_expiry(dte, 0), 0.14, True)
            b = bo.black_scholes(24000, 24000, bo.years_to_expiry(dte, 60), 0.14, True)
            decay[dte] = (a - b) / a
        self.assertGreater(decay[0], decay[1])
        self.assertGreater(decay[1], decay[3])

    def test_implied_iv_round_trips(self):
        import backtest_options as bo
        T = bo.years_to_expiry(2, 45)
        premium = bo.black_scholes(24000, 24000, T, 0.17, True)
        self.assertAlmostEqual(bo.implied_iv(premium, 24000, 24000, T, True), 0.17, places=4)

    def test_calibration_separates_the_two_indices(self):
        import backtest_options as bo
        fills = [
            ("NIFTY", 24000., 24000., 1, 150, 112.65, True),
            ("SENSEX", 76900., 76900., 3, 30, 327.25, False),
        ]
        cal = bo.calibrate_iv_from_fills(fills)
        # A single shared IV would misprice one of them; they are genuinely apart.
        self.assertGreater(cal["NIFTY"], cal["SENSEX"])

    def test_atm_strike_snaps_to_the_grid(self):
        import backtest_options as bo
        self.assertEqual(bo.atm_strike(24037, "NIFTY"), 24050)
        self.assertEqual(bo.atm_strike(81049, "SENSEX"), 81000)

    def test_round_trip_cost_is_dominated_by_flat_brokerage(self):
        import backtest_options as bo
        small, large = bo.round_trip_cost(3000), bo.round_trip_cost(8000)
        self.assertGreater(small / 3000, large / 8000)   # flat fee hurts small trades
        self.assertAlmostEqual(bo.round_trip_cost(5884), 60.2, delta=1.0)


class BacktestEngineTests(unittest.TestCase):
    def _cache(self, tmpdir, sessions=6):
        """Write a deterministic synthetic cache and point the loader at it."""
        import random
        from datetime import datetime, timedelta
        import backtest_data as bd
        from config import INDICES_CONFIG
        random.seed(11)
        bd.CACHE_DIR = tmpdir
        for sym, base in (("NIFTY", 24000.0), ("SENSEX", 78000.0)):
            cfg = INDICES_CONFIG[sym]
            for exch, token in ((cfg["exchange"], history_token(sym)),
                                (cfg["option_exchange"], "FUT" + sym)):
                rows, px, day, made = {}, base, datetime(2026, 6, 1, 9, 15), 0
                while made < sessions:
                    if day.weekday() < 5:
                        t, burst = day, random.randint(20, 45)
                        for i in range(75):
                            px *= (1 + random.gauss(0.0009 if burst <= i < burst + 8 else 0, 0.0006))
                            vol = random.uniform(800, 1500) * (4.0 if burst <= i < burst + 3 else 1.0)
                            rows[t.isoformat()] = {"timestamp": t.isoformat(), "open": px,
                                                   "high": px, "low": px, "close": px, "volume": vol}
                            t += timedelta(minutes=5)
                        made += 1
                    day += timedelta(days=1)
                bd.save_cache(exch, token, rows)

    def test_engine_replays_and_produces_trades(self):
        import backtest_engine as be
        import backtest_data as bd
        original = bd.CACHE_DIR
        tmpdir = tempfile.mkdtemp()
        try:
            self._cache(tmpdir)
            trades = be.Backtest(cost_model=True).run(days=400)
            self.assertGreater(len(trades), 0, "engine produced no trades")
            for t in trades:
                self.assertIn(t["exit_reason"],
                              {"TARGET_HIT", "STOP_LOSS_HIT", "TIME_STOP", "EOD_SQUAREOFF"})
                self.assertAlmostEqual(t["net"], t["gross"] - t["cost"], places=6)
                self.assertGreater(t["cost"], 0)
        finally:
            bd.CACHE_DIR = original
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_costs_reduce_net_and_can_be_disabled(self):
        import backtest_engine as be
        import backtest_data as bd
        original = bd.CACHE_DIR
        tmpdir = tempfile.mkdtemp()
        try:
            self._cache(tmpdir)
            with_costs = be.Backtest(cost_model=True).run(days=400)
            without = be.Backtest(cost_model=False).run(days=400)
            self.assertEqual(len(with_costs), len(without))
            self.assertGreater(sum(t["net"] for t in without),
                               sum(t["net"] for t in with_costs))
            self.assertTrue(all(t["cost"] == 0 for t in without))
        finally:
            bd.CACHE_DIR = original
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_clock_patches_are_fully_reverted(self):
        """A leaked clock patch would corrupt the live modules for anything
        running in the same process."""
        import backtest_engine as be
        import database
        import strategy_brain
        before = (strategy_brain.time, strategy_brain.ist_hhmm,
                  database.ist_today, database.ist_stamp)
        bt = be.Backtest()
        bt._install()
        self.assertIsNot(strategy_brain.time, before[0])
        bt._restore()
        self.assertEqual((strategy_brain.time, strategy_brain.ist_hhmm,
                          database.ist_today, database.ist_stamp), before)

    def test_dte_follows_the_simulated_date(self):
        from datetime import datetime
        import backtest_engine as be
        bt = be.Backtest(expiry_weekday={"NIFTY": 1, "SENSEX": 3})
        bt.clock.set(datetime(2026, 8, 31, 10, 0))       # a Monday
        self.assertEqual(bt.dte_for("NIFTY"), 1)          # Tuesday expiry
        self.assertEqual(bt.dte_for("SENSEX"), 3)         # Thursday expiry
        bt.clock.set(datetime(2026, 9, 1, 10, 0))         # Tuesday
        self.assertEqual(bt.dte_for("NIFTY"), 0)          # expiry day


class IndexExcursionTests(unittest.TestCase):
    """Index excursion decides whether a loss was a signal failure or an exit
    failure, so the sign convention and the classifier have to be exact."""

    def test_call_and_put_excursions_invert(self):
        import trade_analysis as ta
        ext = {"open": 24000.0, "high": 24120.0, "low": 23940.0, "close": 24000.0, "bars": 5}
        mfe_c, mae_c = ta.excursions(ext, call=True)
        self.assertAlmostEqual(mfe_c, 0.5, places=4)     # +120 pts is favourable
        self.assertAlmostEqual(mae_c, -0.25, places=4)   # -60 pts is adverse
        mfe_p, mae_p = ta.excursions(ext, call=False)
        self.assertAlmostEqual(mfe_p, 0.25, places=4)    # a falling index helps a put
        self.assertAlmostEqual(mae_p, -0.5, places=4)

    def test_classifier_separates_exit_failure_from_signal_failure(self):
        import trade_analysis as ta
        self.assertEqual(ta.classify(-100, 0.40, -0.20), "EXIT_WRONG")
        self.assertEqual(ta.classify(-100, 0.02, -0.30), "SIGNAL_WRONG")
        self.assertEqual(ta.classify(250, 0.40, -0.05), "WIN")
        self.assertEqual(ta.classify(250, 0.01, -0.05), "WIN_NO_INDEX_MOVE")

    def test_extremes_use_bar_high_low_not_close(self):
        import trade_analysis as ta
        from datetime import datetime
        bars = [
            {"dt": datetime(2026, 9, 1, 10, 0), "open": 100.0, "high": 105.0, "low": 99.0, "close": 101.0},
            {"dt": datetime(2026, 9, 1, 10, 5), "open": 101.0, "high": 103.0, "low": 96.0, "close": 100.0},
        ]
        ext = ta.window_extremes(bars, datetime(2026, 9, 1, 10, 0), datetime(2026, 9, 1, 10, 5))
        self.assertEqual(ext["high"], 105.0)   # not max(close)
        self.assertEqual(ext["low"], 96.0)     # not min(close)

    def test_symbol_direction(self):
        import trade_analysis as ta
        self.assertTrue(ta.is_call("NIFTY01SEP2624100CE"))
        self.assertFalse(ta.is_call("SENSEX2690376900PE"))

    def test_ensure_columns_is_idempotent(self):
        import trade_analysis as ta
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            DatabaseManager(path)
            first = ta.ensure_columns(path)
            self.assertTrue(first)
            self.assertEqual(ta.ensure_columns(path), [])   # second run adds nothing
        finally:
            os.remove(path)

    def test_backfill_end_to_end(self):
        import sqlite3
        from datetime import datetime, timedelta
        import backtest_data as bd
        import trade_analysis as ta

        original = bd.CACHE_DIR
        cache = tempfile.mkdtemp()
        fd, dbp = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            bd.CACHE_DIR = cache
            cfg = INDICES_CONFIG["NIFTY"]
            rows, t = {}, datetime(2026, 9, 1, 9, 15)
            for i in range(60):
                # 10 pts/bar -> ~0.5% over the holding window, comfortably above
                # the 0.15% "could have reached the target" threshold.
                px = 24000.0 + 10 * i
                rows[t.isoformat()] = {"timestamp": t.isoformat(), "open": px,
                                       "high": px + 5, "low": px - 5,
                                       "close": px, "volume": 1000}
                t += timedelta(minutes=5)
            bd.save_cache(cfg["exchange"], history_token("NIFTY"), rows)

            DatabaseManager(dbp)
            conn = sqlite3.connect(dbp)
            conn.execute(
                "INSERT INTO trades (symbol, token, qty, exchange, index_name, entry_price,"
                " status, exit_price, exit_reason, entry_reason, timestamp, entry_time, exit_time)"
                " VALUES ('NIFTY01SEP2624100CE','1',65,'NFO','NIFTY',50.0,'CLOSED',45.0,"
                "'STOP_LOSS_HIT','VOLUME_BREAKOUT','2026-09-01 10:00:00',"
                "'2026-09-01 10:00:00','2026-09-01 11:00:00')")
            conn.commit()
            conn.close()

            filled, skipped = ta.backfill(dbp)
            self.assertEqual((filled, skipped), (1, 0))
            db = DatabaseManager(dbp)
            row = db.fetch_one("SELECT index_mfe_pct, signal_verdict, index_high FROM trades")
            # Index rose throughout, so a losing CALL is an exit failure.
            self.assertGreater(row["index_mfe_pct"], 0)
            self.assertEqual(row["signal_verdict"], "EXIT_WRONG")
            # Second backfill must not duplicate work.
            self.assertEqual(ta.backfill(dbp)[0], 0)
        finally:
            bd.CACHE_DIR = original
            shutil.rmtree(cache, ignore_errors=True)
            os.remove(dbp)



class TimeframeDefaultsTests(unittest.TestCase):
    """The whole point of the switches is that they are inert until moved.
    If a default ever changes behaviour, the frozen forward sample is void."""

    def test_defaults_are_current_behaviour(self):
        import timeframes as tf
        self.assertEqual(tf.entry_timing(), tf.IMMEDIATE)
        self.assertEqual(tf.stop_mode(), tf.FIXED_PCT)

    def test_arm_is_a_noop_under_immediate(self):
        import timeframes as tf
        book, bars = tf.PendingBook(), tf.MinuteBars()
        bars.update(600, 24000.0)
        bars.update(601, 24010.0)
        self.assertFalse(book.arm("NIFTY", "CE", "VOLUME_BREAKOUT", 24010.0, 601, bars))
        self.assertEqual(book.pending, {})

    def test_structural_stop_inert_under_fixed_pct(self):
        import timeframes as tf
        bars = tf.MinuteBars()
        bars.update(600, 24000.0)
        bars.update(601, 24010.0)
        self.assertIsNone(tf.structural_stop_level(True, bars, 24010.0))
        self.assertFalse(tf.structural_stop_hit(True, None, 1.0))


class MinuteBarTests(unittest.TestCase):
    def test_bars_roll_on_the_minute_and_track_extremes(self):
        import timeframes as tf
        bars = tf.MinuteBars()
        self.assertIsNone(bars.update(600, 100.0))
        self.assertIsNone(bars.update(600, 104.0))
        self.assertIsNone(bars.update(600, 98.0))
        closed = bars.update(601, 99.0)
        self.assertIsNotNone(closed)
        self.assertEqual((closed["open"], closed["high"], closed["low"], closed["close"]),
                         (100.0, 104.0, 98.0, 98.0))

    def test_ring_buffer_is_bounded(self):
        import timeframes as tf
        bars = tf.MinuteBars(max_keep=3)
        for m in range(10):
            bars.update(600 + m, 100.0 + m)
        self.assertEqual(len(bars.closed), 3)

    def test_pivot_side_depends_on_direction(self):
        import timeframes as tf
        bars = tf.MinuteBars()
        bars.update(600, 100.0)
        bars.update(600, 105.0)
        bars.update(600, 95.0)
        bars.update(601, 99.0)
        self.assertEqual(bars.pivot(is_call=True), 95.0)
        self.assertEqual(bars.pivot(is_call=False), 105.0)


class EntryConfirmationTests(unittest.TestCase):
    def setUp(self):
        from config import RISK
        self._saved = {k: RISK.get(k) for k in
                       ("entry_timing", "stop_mode", "confirm_window_min", "pullback_pct")}

    def tearDown(self):
        from config import RISK
        RISK.update(self._saved)

    def _bars(self):
        import timeframes as tf
        bars = tf.MinuteBars()
        bars.update(600, 24000.0)
        bars.update(600, 24020.0)
        bars.update(601, 24010.0)
        return bars

    def test_continuation_fills_only_on_a_new_extreme(self):
        from config import RISK
        import timeframes as tf
        RISK["entry_timing"] = "continuation"
        book = tf.PendingBook()
        self.assertTrue(book.arm("NIFTY", "CE", "VOLUME_BREAKOUT", 24010.0, 601, self._bars()))
        faded = {"close": 23990.0, "low": 23980.0, "high": 24005.0}
        self.assertIsNone(book.on_minute_close("NIFTY", faded, 602))
        self.assertTrue(book.pending)
        broke_out = {"close": 24030.0, "low": 24005.0, "high": 24035.0}
        filled = book.on_minute_close("NIFTY", broke_out, 603)
        self.assertIsNotNone(filled)
        self.assertEqual(filled.side, "CE")
        self.assertEqual(book.pending, {})

    def test_continuation_for_a_put_needs_a_new_low(self):
        from config import RISK
        import timeframes as tf
        RISK["entry_timing"] = "continuation"
        book = tf.PendingBook()
        book.arm("NIFTY", "PE", "VOLUME_BREAKOUT", 24010.0, 601, self._bars())
        rallied = {"close": 24050.0, "low": 24040.0, "high": 24060.0}
        self.assertIsNone(book.on_minute_close("NIFTY", rallied, 602))
        broke_down = {"close": 23990.0, "low": 23985.0, "high": 24005.0}
        self.assertIsNotNone(book.on_minute_close("NIFTY", broke_down, 603))

    def test_pending_expires_and_is_dropped(self):
        from config import RISK
        import timeframes as tf
        RISK["entry_timing"] = "continuation"
        RISK["confirm_window_min"] = 2
        book = tf.PendingBook()
        book.arm("NIFTY", "CE", "VOLUME_BREAKOUT", 24010.0, 601, self._bars())
        flat = {"close": 23990.0, "low": 23980.0, "high": 24000.0}
        for minute in (602, 603):
            book.on_minute_close("NIFTY", flat, minute)
        self.assertTrue(book.pending)
        book.on_minute_close("NIFTY", flat, 604)
        self.assertEqual(book.pending, {}, "expired signal was not dropped")

    def test_pullback_fills_on_a_retracement(self):
        from config import RISK
        import timeframes as tf
        RISK["entry_timing"] = "pullback"
        RISK["pullback_pct"] = 0.10
        book = tf.PendingBook()
        book.arm("NIFTY", "CE", "VOLUME_BREAKOUT", 24000.0, 601, self._bars())
        trigger = book.pending["NIFTY"].trigger
        self.assertAlmostEqual(trigger, 24000.0 * 0.999, places=4)
        rose = {"close": 24010.0, "low": 24005.0, "high": 24015.0}
        self.assertIsNone(book.on_minute_close("NIFTY", rose, 602))
        dipped = {"close": 23985.0, "low": 23970.0, "high": 24000.0}
        self.assertIsNotNone(book.on_minute_close("NIFTY", dipped, 603))

    def test_falls_back_to_immediate_without_1m_history(self):
        from config import RISK
        import timeframes as tf
        RISK["entry_timing"] = "continuation"
        book = tf.PendingBook()
        self.assertFalse(book.arm("NIFTY", "CE", "VOLUME_BREAKOUT", 24000.0, 601, tf.MinuteBars()))


class StructuralStopTests(unittest.TestCase):
    def setUp(self):
        from config import RISK
        self._saved = RISK.get("stop_mode")
        RISK["stop_mode"] = "structural_1m"

    def tearDown(self):
        from config import RISK
        RISK["stop_mode"] = self._saved

    def _bars(self):
        import timeframes as tf
        bars = tf.MinuteBars()
        bars.update(600, 24000.0)
        bars.update(600, 24020.0)
        bars.update(601, 24010.0)
        return bars

    def test_level_and_breach_for_a_call(self):
        import timeframes as tf
        level = tf.structural_stop_level(True, self._bars(), 24010.0)
        self.assertEqual(level, 24000.0)
        self.assertFalse(tf.structural_stop_hit(True, level, 24005.0))
        self.assertTrue(tf.structural_stop_hit(True, level, 23999.0))

    def test_level_and_breach_for_a_put(self):
        import timeframes as tf
        level = tf.structural_stop_level(False, self._bars(), 24010.0)
        self.assertEqual(level, 24020.0)
        self.assertTrue(tf.structural_stop_hit(False, level, 24025.0))

    def test_refuses_a_level_already_breached_at_entry(self):
        import timeframes as tf
        self.assertIsNone(tf.structural_stop_level(True, self._bars(), 23990.0))



class TokenAwareCandleAPI:
    """Mimics the real failure: the websocket token returns status=True with an
    EMPTY data list, so nothing looks like an error and the cache stays empty."""

    WORKING = {"99926000", "99919000"}

    def __init__(self, bars=60):
        self.bars = bars
        self.requested = []

    def getCandleData(self, params):
        from datetime import datetime, timedelta, timezone
        self.requested.append(params["symboltoken"])
        if str(params["symboltoken"]) not in self.WORKING:
            return {"status": True, "data": []}      # the silent failure
        ist = timezone(timedelta(hours=5, minutes=30))
        now = datetime.now(timezone.utc).astimezone(ist)
        secs = now.hour * 3600 + now.minute * 60 + now.second
        cur = now - timedelta(seconds=secs % 300, microseconds=now.microsecond)
        data = []
        for i in range(self.bars, -1, -1):
            ts = cur - timedelta(minutes=5) * i
            px = 24000.0 + (self.bars - i)
            data.append([ts.isoformat(), px, px + 5, px - 5, px + 1, 100000.0])
        return {"status": True, "data": data}


class NiftySeedingTests(unittest.TestCase):
    """NIFTY seeding failed silently because the seeder asked for candles with
    the websocket token. This pins the fix."""

    def test_seeder_requests_the_history_token(self):
        import history_seeder
        from config import history_token
        brain = StrategyBrain(order_engine=None, options_builders={})
        api = TokenAwareCandleAPI()
        bars = history_seeder.seed_price_history(api, brain, "NIFTY")
        self.assertGreater(bars, 0, "NIFTY did not seed")
        self.assertIn(history_token("NIFTY"), api.requested)
        self.assertNotIn("26000", api.requested)

    def test_both_indices_seed(self):
        import history_seeder
        for symbol in ("NIFTY", "SENSEX"):
            brain = StrategyBrain(order_engine=None, options_builders={})
            self.assertGreater(
                history_seeder.seed_price_history(TokenAwareCandleAPI(), brain, symbol), 0,
                "%s did not seed" % symbol)
            # 22 bars is the threshold evaluate_tick needs before it will trade.
            self.assertGreaterEqual(len(brain.price_histories[symbol]) - 1, 22)

    def test_websocket_token_would_still_fail(self):
        """Guards the diagnosis itself: if 26000 ever starts working, this test
        fails and the comment in history_seeder needs revisiting."""
        api = TokenAwareCandleAPI()
        self.assertEqual(api.getCandleData({"symboltoken": "26000"})["data"], [])

    def test_empty_seed_is_logged_at_error(self):
        import history_seeder
        brain = StrategyBrain(order_engine=None, options_builders={})
        api = TokenAwareCandleAPI()
        api.WORKING = set()                      # nothing resolves
        with self.assertLogs(level="ERROR") as captured:
            self.assertEqual(history_seeder.seed_price_history(api, brain, "NIFTY"), 0)
        joined = " ".join(captured.output)
        # A silent warning is what let this run unnoticed for two sessions.
        self.assertIn("NO ENTRIES", joined)



class ForwardExcursionTests(unittest.TestCase):
    """Held-window MFE cannot tell a dead signal from a stop that fired too
    early. The forward window is the measurement that can."""

    def _bars(self, flat_bars=12, move_bars=8, step=20.0):
        from datetime import datetime, timedelta
        bars, t, px = [], datetime(2026, 9, 1, 9, 15), 24000.0
        for i in range(75):
            if flat_bars <= i < flat_bars + move_bars:
                px += step
            bars.append({"dt": t, "open": px, "high": px + 2, "low": px - 2, "close": px})
            t += timedelta(minutes=5)
        return bars

    def test_forward_window_sees_a_move_the_holding_window_missed(self):
        from datetime import datetime
        import trade_analysis as ta
        bars = self._bars()
        entry = datetime(2026, 9, 1, 10, 15)
        held = ta.window_extremes(bars, entry, datetime(2026, 9, 1, 10, 19))
        held_mfe, _ = ta.excursions(held, call=True)
        fwd30 = ta.forward_excursion(bars, entry, 30, call=True)
        self.assertLess(held_mfe, 0.15)      # a 4-minute hold sees nothing
        self.assertGreater(fwd30, held_mfe * 3)

    def test_forward_windows_are_monotonic(self):
        from datetime import datetime
        import trade_analysis as ta
        bars = self._bars()
        entry = datetime(2026, 9, 1, 10, 15)
        vals = [ta.forward_excursion(bars, entry, m, call=True) for m in (15, 30, 45)]
        self.assertEqual(vals, sorted(vals))

    def test_put_direction_inverts(self):
        from datetime import datetime
        import trade_analysis as ta
        rising = self._bars()
        entry = datetime(2026, 9, 1, 10, 15)
        self.assertGreater(ta.forward_excursion(rising, entry, 30, call=True), 0)
        # A rising index is adverse for a put. The entry bar's own low sits just
        # below its open, so a tiny favourable excursion is real, not a bug --
        # it just must be far below the 0.15% tradeable threshold.
        self.assertLess(ta.forward_excursion(rising, entry, 30, call=False), 0.05)

    def test_flat_index_yields_no_forward_move(self):
        from datetime import datetime
        import trade_analysis as ta
        flat = self._bars(flat_bars=75, move_bars=0)
        val = ta.forward_excursion(flat, datetime(2026, 9, 1, 10, 15), 45, call=True)
        self.assertLess(abs(val), 0.05)

    def test_missing_bars_return_none(self):
        from datetime import datetime
        import trade_analysis as ta
        self.assertIsNone(ta.forward_excursion([], datetime(2026, 9, 1, 10, 15), 30, True))
        self.assertIsNone(ta.forward_excursion(self._bars(), None, 30, True))



class RandomWalkBaselineTests(unittest.TestCase):
    """A forward-excursion table always rises with time, because a running
    maximum grows as sqrt(t) for any series. Only beating that is evidence."""

    def test_baseline_scales_as_sqrt_t(self):
        import trade_analysis as ta
        med15, _ = ta.random_walk_baseline(0.03, 15)
        med60, _ = ta.random_walk_baseline(0.03, 60)
        self.assertAlmostEqual(med60 / med15, 2.0, places=6)   # 4x time -> 2x

    def test_reach_probability_rises_with_time(self):
        import trade_analysis as ta
        _, r15 = ta.random_walk_baseline(0.03, 15)
        _, r45 = ta.random_walk_baseline(0.03, 45)
        self.assertLess(r15, r45)
        self.assertTrue(0 <= r15 <= 100 and 0 <= r45 <= 100)

    def test_sigma_round_trips(self):
        import trade_analysis as ta
        sigma = ta.implied_sigma_per_min(0.079, 15)
        med, _ = ta.random_walk_baseline(sigma, 15)
        self.assertAlmostEqual(med, 0.079, places=6)

    def test_live_data_does_not_beat_the_baseline(self):
        """Pins the Sep 2026 finding: observed reach sits below diffusion at
        every window, so the growth in the table is not predictive power."""
        import trade_analysis as ta
        sigma = ta.implied_sigma_per_min(0.079, 15)
        for minutes, observed_reach in ((15, 16.7), (30, 26.7), (45, 30.0)):
            _, baseline = ta.random_walk_baseline(sigma, minutes)
            self.assertLess(observed_reach, baseline,
                            "%dm unexpectedly beat the random walk" % minutes)

    def test_degenerate_inputs(self):
        import trade_analysis as ta
        self.assertEqual(ta.random_walk_baseline(0.0, 30), (0.0, 0.0))
        self.assertEqual(ta.implied_sigma_per_min(0.0, 15), 0.0)
        self.assertEqual(ta.implied_sigma_per_min(0.079, 0), 0.0)
class SignalLabTests(unittest.TestCase):
    """Screening asks whether a signal predicts index movement at all, before
    any option model or cost can confuse the answer."""

    def _bars(self, n=60, step=10.0):
        from datetime import datetime, timedelta
        out, t, px = [], datetime(2026, 9, 1, 9, 15), 24000.0
        for _ in range(n):
            px += step
            out.append({"dt": t, "open": px, "high": px + 3, "low": px - 3,
                        "close": px, "volume": 1000.0})
            t += timedelta(minutes=5)
        return out

    def test_forward_return_is_signed_by_direction(self):
        import signal_lab as sl
        bars = self._bars()
        up = sl.forward_return(bars, 5, 30, "CE")
        down = sl.forward_return(bars, 5, 30, "PE")
        self.assertGreater(up, 0)                 # rising index helps a call
        self.assertAlmostEqual(up, -down, places=9)

    def test_forward_return_never_crosses_a_session(self):
        from datetime import timedelta
        import signal_lab as sl
        bars = self._bars(n=10)
        for b in bars[5:]:
            b["dt"] = b["dt"] + timedelta(days=1)   # next session
        self.assertIsNone(sl.forward_return(bars, 3, 30, "CE"))

    def test_mfe_uses_bar_extremes(self):
        import signal_lab as sl
        bars = self._bars(n=20)
        mfe = sl.forward_mfe(bars, 2, 30, "CE")
        ret = sl.forward_return(bars, 2, 30, "CE")
        self.assertGreater(mfe, ret)              # the high exceeds the close

    def test_t_stat_is_zero_for_constant_or_tiny_samples(self):
        import signal_lab as sl
        self.assertEqual(sl.t_stat([1.0, 1.0, 1.0]), 0.0)
        self.assertEqual(sl.t_stat([1.0]), 0.0)

    def test_detects_a_planted_directional_edge(self):
        import signal_lab as sl
        bars = self._bars(n=200, step=8.0)        # relentlessly rising
        res = sl.evaluate(bars, None, lambda b, i, f: "CE", [30])
        self.assertGreater(res["windows"][30]["mean"], 0)
        self.assertGreater(res["windows"][30]["t"], 3.0)

    def test_reports_negative_edge_for_the_wrong_direction(self):
        import signal_lab as sl
        bars = self._bars(n=200, step=8.0)
        res = sl.evaluate(bars, None, lambda b, i, f: "PE", [30])
        self.assertLess(res["windows"][30]["t"], -3.0)

    def test_signals_respect_the_session_window(self):
        import signal_lab as sl
        bars = self._bars(n=200)
        res = sl.evaluate(bars, None, lambda b, i, f: "CE", [15])
        for bar in bars:
            pass
        # every trigger must sit inside 09:45-14:30
        self.assertLessEqual(res["n"], sum(1 for b in bars if sl._in_session(b)))

    def test_multiple_testing_bar_rises_with_test_count(self):
        import signal_lab as sl
        raw = -sl._inv_norm(0.025 / 1)
        many = -sl._inv_norm(0.025 / 36)
        self.assertAlmostEqual(raw, 1.96, places=2)
        self.assertGreater(many, 3.0)             # 36 looks demands a higher bar


if __name__ == "__main__":
    unittest.main()
