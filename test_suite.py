import os
import tempfile
import unittest
from datetime import datetime
from risk_manager import RiskManager
from config import RISK, INDICES_CONFIG
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
        gate.closed_volumes["NIFTY"] = [100] * 19 + [200]
        self.assertTrue(gate.allows_entry("NIFTY"))
        # Average volume is enough for RSI hook; 1.2x is only required for breakout.
        gate.closed_volumes["NIFTY"] = [100] * 20
        self.assertTrue(gate.allows_entry("NIFTY"))
        self.assertFalse(volume_expanded(gate.closed_volumes["NIFTY"], RISK["volume_mult"], 20))
        gate.closed_volumes["NIFTY"] = [100] * 19 + [50]
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
        self.assertEqual(RISK["volume_sma_bars"], 20)
        self.assertEqual(RISK["volume_mult"], 1.2)
        self.assertEqual(RISK["volume_hook_mult"], 1.0)
        self.assertGreaterEqual(RISK["volume_ok_hold_sec"], 60)

    def test_volume_gate_ltq_fallback_and_sticky_hold(self):
        import time as time_mod

        gate = VolumeExpansionGate()
        gate.mark_subscribed("NIFTY", True)
        now = time_mod.time()
        gate.last_bar_time["NIFTY"] = now
        gate.last_bar_minute["NIFTY"] = int(now // 60)
        gate.last_session_vol["NIFTY"] = 1000.0
        gate.on_fut_tick("NIFTY", volume_traded_today=1000.0, last_traded_qty=12.0)
        self.assertEqual(gate.forming_vol["NIFTY"], 12.0)

        gate.forming_vol["NIFTY"] = 80.0
        gate.last_bar_time["NIFTY"] = now - 61
        gate.last_bar_minute["NIFTY"] = int(now // 60) - 1
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
        skipped = brain._try_volume_breakout("NIFTY", 101.0, 100.0, "CHOPPY", cfg)
        self.assertFalse(skipped)
        self.assertEqual(len(om.calls), 1)
        self.assertIsNone(gate.breakout_event["NIFTY"])

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


if __name__ == "__main__":
    unittest.main()
