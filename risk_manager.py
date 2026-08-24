import logging
from config import FALLBACK_LOT_SIZE, RISK, daily_entry_cap


class RiskManager:
    def __init__(
        self,
        db_manager=None,
        max_risk_per_trade_pct: float = 3.5,
        max_daily_loss_inr: float = None,
        max_consecutive_losses: int = None,
    ):
        self.db = db_manager
        self.max_risk_per_trade_pct = max_risk_per_trade_pct
        self.max_daily_loss_inr = (
            max_daily_loss_inr if max_daily_loss_inr is not None else RISK["max_daily_loss_inr"]
        )
        self.max_consecutive_losses = (
            max_consecutive_losses
            if max_consecutive_losses is not None
            else RISK["max_consecutive_losses"]
        )
        self.consecutive_losses = 0
        self.daily_pnl = 0.0
        self.trading_halted = False

    def _qty_for(self, symbol, qty):
        if qty:
            return int(qty)
        upper = (symbol or "").upper()
        if "SENSEX" in upper:
            return FALLBACK_LOT_SIZE["SENSEX"]
        if "BANKNIFTY" in upper:
            return FALLBACK_LOT_SIZE["BANKNIFTY"]
        return FALLBACK_LOT_SIZE["NIFTY"]

    def refresh_from_db(self):
        if not self.db:
            return
        trades = self.db.fetch_closed_today()
        daily_pnl = 0.0
        consecutive = 0
        for trade in trades:
            symbol = trade["symbol"] if hasattr(trade, "keys") else trade[0]
            qty = trade["qty"] if hasattr(trade, "keys") else trade[1]
            entry = trade["entry_price"] if hasattr(trade, "keys") else trade[2]
            exit_p = trade["exit_price"] if hasattr(trade, "keys") else trade[3]
            if entry is None or exit_p is None:
                continue
            pnl = (float(exit_p) - float(entry)) * self._qty_for(symbol, qty)
            daily_pnl += pnl
            if pnl < 0:
                consecutive += 1
            else:
                consecutive = 0
        self.daily_pnl = daily_pnl
        self.consecutive_losses = consecutive
        if daily_pnl <= -abs(self.max_daily_loss_inr) or consecutive >= self.max_consecutive_losses:
            if not self.trading_halted:
                logging.critical(
                    f"CIRCUIT BREAKER: PnL ₹{daily_pnl:.2f} | streak {consecutive}. Halting entries."
                )
            self.trading_halted = True

    def register_trade_result(self, pnl_rupees: float):
        self.daily_pnl += pnl_rupees
        if pnl_rupees < 0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0
        if self.consecutive_losses >= self.max_consecutive_losses:
            self.trading_halted = True
        elif self.daily_pnl <= -abs(self.max_daily_loss_inr):
            self.trading_halted = True

    def assess_order_safety(self, order_proposal: dict, estimated_premium: float = 0.0) -> bool:
        self.refresh_from_db()
        if self.trading_halted:
            logging.warning("RiskManager blocked entry: trading halted.")
            return False

        if not self.db:
            return True

        index_name = order_proposal.get("index_name")
        if self.db.count_open_trades() >= RISK["max_open_total"]:
            logging.info("RiskManager blocked entry: max open trades reached.")
            return False
        if index_name and self.db.count_open_trades(index_name) >= RISK["max_open_per_index"]:
            logging.info(f"RiskManager blocked entry: {index_name} already has an OPEN trade.")
            return False
        if self.db.count_entries_today() >= daily_entry_cap():
            logging.info(
                f"RiskManager blocked entry: daily entry cap reached "
                f"({self.db.count_entries_today()}/{daily_entry_cap()})."
            )
            return False

        reason = str(order_proposal.get("entry_reason") or "").upper()
        trend_reasons = ("TREND_CONT", "RSI_HOOK")
        if reason in trend_reasons:
            trend_cap = int(RISK.get("max_trend_entries_per_day", daily_entry_cap()))
            trend_n = self.db.count_entries_today(entry_reasons=trend_reasons)
            if trend_n >= trend_cap:
                logging.info(
                    f"RiskManager blocked {reason}: trend soft-cap "
                    f"{trend_n}/{trend_cap} (VOLUME_BREAKOUT still allowed)."
                )
                return False

        qty = int(order_proposal.get("qty") or 0)
        premium = float(estimated_premium or 0.0)
        if premium < RISK["min_option_premium"]:
            logging.info(f"RiskManager blocked entry: premium ₹{premium} below min.")
            return False
        if qty > 0 and premium * qty > RISK["max_premium_risk_inr"]:
            logging.info(
                f"RiskManager blocked entry: notional ₹{premium * qty:.0f} exceeds cap."
            )
            return False
        return True
