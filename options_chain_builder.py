import logging
import json
import os
from datetime import datetime, timedelta
from config import FALLBACK_LOT_SIZE, RISK
from ist_time import ist_now


class DynamicOptionsChainBuilder:
    def __init__(self, index_name="NIFTY", smart_api=None):
        self.index_name = index_name.upper()
        self.smart_api = smart_api
        self.nfo_contracts = []
        self.scrip_master_data = []
        self.option_exchange = "BFO" if self.index_name == "SENSEX" else "NFO"

    def _contract_name(self, item):
        return str(item.get("name") or item.get("Name") or "").upper().strip()

    def _is_index_contract(self, item):
        name = self._contract_name(item)
        if name:
            return name == self.index_name
        sym = str(item.get("symbol") or "").upper()
        collisions = ("BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYIT")
        if self.index_name == "NIFTY":
            return sym.startswith("NIFTY") and not any(sym.startswith(c) for c in collisions)
        return sym.startswith(self.index_name)

    def _lotsize(self, item):
        raw = item.get("lotsize") or item.get("lot_size") or FALLBACK_LOT_SIZE.get(self.index_name, 0)
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return int(FALLBACK_LOT_SIZE.get(self.index_name, 0))

    @staticmethod
    def _ist_midnight():
        return ist_now().replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)

    def _pack(self, candidate, expiry_date):
        return {
            "symbol": candidate.get("symbol"),
            "token": candidate.get("token") or candidate.get("symboltoken"),
            "strike": candidate.get("parsed_strike"),
            "expiry": expiry_date.strftime("%d%b%Y").upper(),
            "dte": (expiry_date - self._ist_midnight()).days,
            "lotsize": self._lotsize(candidate),
            "exchange": self.option_exchange,
        }

    def load_scrip_master(self, scrip_data=None):
        try:
            if scrip_data:
                self.scrip_master_data = scrip_data
            elif os.path.exists("scrip_master.json"):
                with open("scrip_master.json", "r", encoding="utf-8") as f:
                    self.scrip_master_data = json.load(f)

            self.nfo_contracts = []
            for item in self.scrip_master_data:
                if not self._is_index_contract(item):
                    continue
                if item.get("instrumenttype", "") not in ["OPTIDX", "OPTSTK"]:
                    continue
                exch = str(item.get("exch_seg") or item.get("exchange") or "").upper()
                if exch and exch not in (self.option_exchange, ""):
                    continue
                self.nfo_contracts.append(item)

            logging.info(
                f"[ChainBuilder-{self.index_name}] Loaded {len(self.nfo_contracts)} {self.option_exchange} option contracts."
            )
        except Exception as e:
            logging.error(f"❌ Error loading scrip master for {self.index_name}: {e}")

    def _parse_expiry(self, exp_str):
        if not exp_str:
            return None
        for fmt in ("%d-%b-%Y", "%d%b%Y", "%Y-%m-%d", "%d-%b-%y"):
            try:
                return datetime.strptime(exp_str, fmt)
            except ValueError:
                continue
        return None

    def get_nearest_expiry_future(self):
        """Nearest unexpired FUTIDX for this index. Used for volume expansion."""
        try:
            source = self.scrip_master_data or []
            if not source and os.path.exists("scrip_master.json"):
                with open("scrip_master.json", "r", encoding="utf-8") as f:
                    source = json.load(f)
                    self.scrip_master_data = source
            today = self._ist_midnight()
            candidates = []
            for item in source:
                if str(item.get("instrumenttype") or "").upper() != "FUTIDX":
                    continue
                if not self._is_index_contract(item):
                    continue
                exch = str(item.get("exch_seg") or item.get("exchange") or "").upper()
                if exch and exch not in (self.option_exchange, ""):
                    continue
                parsed = self._parse_expiry(item.get("expiry"))
                if parsed and parsed >= today:
                    candidates.append((parsed, item))
            if not candidates:
                logging.warning(f"[{self.index_name}] No FUTIDX found for volume gate.")
                return None
            candidates.sort(key=lambda x: x[0])
            exp, item = candidates[0]
            packed = {
                "symbol": item.get("symbol"),
                "token": str(item.get("token") or item.get("symboltoken") or ""),
                "expiry": exp.strftime("%d%b%Y").upper(),
                "exchange": self.option_exchange,
                "exchange_type": 4 if self.option_exchange == "BFO" else 2,
            }
            if not packed["token"] or not packed["symbol"]:
                return None
            logging.info(
                f"[ChainBuilder-{self.index_name}] Future {packed['symbol']} token={packed['token']} exp={packed['expiry']}"
            )
            return packed
        except Exception as e:
            logging.error(f"❌ Error resolving future for {self.index_name}: {e}")
            return None

    @staticmethod
    def _level_price(levels):
        if not levels or not isinstance(levels, list):
            return None
        row = levels[0]
        raw = row.get("price") if isinstance(row, dict) else row
        try:
            px = float(raw)
            return px if px > 0 else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _quote_row(q_resp):
        payload = (q_resp or {}).get("data") if isinstance(q_resp, dict) else None
        if isinstance(payload, dict):
            fetched = payload.get("fetched")
            if isinstance(fetched, list) and fetched:
                return fetched[0] if isinstance(fetched[0], dict) else {}
            return payload
        if isinstance(payload, list) and payload:
            return payload[0] if isinstance(payload[0], dict) else {}
        return {}

    @staticmethod
    def _bid_ask(q_data, ltp):
        depth = q_data.get("depth") if isinstance(q_data.get("depth"), dict) else {}
        bid = DynamicOptionsChainBuilder._level_price(depth.get("buy") or depth.get("Buy"))
        ask = DynamicOptionsChainBuilder._level_price(depth.get("sell") or depth.get("Sell"))
        if bid is None:
            for key in ("bestBidPrice", "bidPrice", "best_bid_price"):
                raw = q_data.get(key)
                if raw not in (None, "", 0, "0"):
                    bid = float(raw)
                    break
        if ask is None:
            for key in ("bestAskPrice", "askPrice", "best_ask_price"):
                raw = q_data.get(key)
                if raw not in (None, "", 0, "0"):
                    ask = float(raw)
                    break
        return bid, ask

    @staticmethod
    def _session_volume(q_data):
        for key in ("tradeVolume", "volume", "opVolume", "totQtyTraded", "lastTradeQty"):
            raw = q_data.get(key)
            if raw not in (None, ""):
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    continue
        return 0.0

    def _liquidity_ok(self, q_data, ltp, symbol):
        min_vol = float(RISK.get("min_option_volume", 500.0))
        max_spread = float(RISK.get("max_option_spread_pct", 3.0))
        volume = self._session_volume(q_data)
        bid, ask = self._bid_ask(q_data, ltp)
        if volume < min_vol:
            logging.warning(
                f"[LIQUIDITY REJECTED] {symbol} (Vol: {volume}, Spread: n/a) — below min volume"
            )
            return False
        if bid and ask and ltp > 0 and ask >= bid:
            spread_pct = ((ask - bid) / ltp) * 100.0
            if spread_pct <= max_spread:
                logging.info(
                    f"[LIQUIDITY PASSED] {symbol} | Vol: {volume} | Spread: {spread_pct:.2f}% "
                    f"| bid {bid} ask {ask}"
                )
                return True
            logging.warning(
                f"[LIQUIDITY REJECTED] {symbol} (Vol: {volume}, Spread: {spread_pct:.2f}%)"
            )
            return False
        # SmartAPI FULL often has no bestBidPrice; do not invent ltp±1% (that is always 2%).
        logging.info(
            f"[LIQUIDITY PASSED] {symbol} | Vol: {volume} | Spread: n/a (no depth, volume-only)"
        )
        return True

    def get_nearest_expiry_contract(self, spot_price, instrument_type="CE"):
        """Nearest-expiry ATM option. Returns None if liquidity cannot be verified."""
        try:
            if not self.nfo_contracts:
                self.load_scrip_master()
                if not self.nfo_contracts:
                    return None

            valid_contracts = []
            today = self._ist_midnight()
            min_dte = int(RISK.get("min_dte", 0))
            cutoff = today + timedelta(days=min_dte)

            for c in self.nfo_contracts:
                exp_str = c.get("expiry")
                if not exp_str:
                    continue
                parsed_date = self._parse_expiry(exp_str)
                if parsed_date and parsed_date >= cutoff:
                    c["parsed_expiry_date"] = parsed_date
                    valid_contracts.append(c)

            if not valid_contracts:
                logging.warning(
                    f"[{self.index_name}] No contract with DTE >= {min_dte}."
                )
                return None

            earliest_expiry_date = min(valid_contracts, key=lambda x: x["parsed_expiry_date"])["parsed_expiry_date"]
            matching_contracts = []
            for c in valid_contracts:
                if c["parsed_expiry_date"] != earliest_expiry_date:
                    continue
                sym = str(c.get("symbol", "")).upper()
                if not sym.endswith(instrument_type.upper()):
                    continue
                try:
                    raw_strike = float(c.get("strike", 0))
                    actual_strike = raw_strike / 100.0 if raw_strike > 100000 else raw_strike
                    c["parsed_strike"] = actual_strike
                    matching_contracts.append(c)
                except ValueError:
                    continue

            if not matching_contracts:
                return None

            sorted_candidates = sorted(
                matching_contracts, key=lambda x: abs(x["parsed_strike"] - spot_price)
            )

            if not self.smart_api:
                packed = self._pack(sorted_candidates[0], earliest_expiry_date)
                if packed["lotsize"] <= 0:
                    return None
                return packed

            for candidate in sorted_candidates[:5]:
                packed = self._pack(candidate, earliest_expiry_date)
                contract_symbol = packed["symbol"]
                contract_token = packed["token"]
                if packed["lotsize"] <= 0 or not contract_symbol or not contract_token:
                    continue
                try:
                    exchange = packed["exchange"]
                    resp = self.smart_api.ltpData(exchange, contract_symbol, contract_token)
                    if not (resp and resp.get("status")):
                        continue
                    data = resp.get("data", {}) or {}
                    ltp = float(data.get("ltp", 0) or 0)
                    q_resp = self.smart_api.getMarketData(
                        mode="FULL", exchangeTokens={exchange: [str(contract_token)]}
                    )
                    if not (q_resp and q_resp.get("status")):
                        if ltp > 0:
                            logging.info(
                                f"[LIQUIDITY PASSED] {contract_symbol} | LTP-only (quote fetch failed)"
                            )
                            return packed
                        continue
                    q_data = self._quote_row(q_resp)
                    if self._liquidity_ok(q_data, ltp, contract_symbol):
                        if packed["lotsize"] <= 0:
                            continue
                        return packed
                except Exception as ex:
                    logging.warning(f"Could not verify depth for {contract_symbol}: {ex}")
                    continue

            logging.warning(f"[{self.index_name}] No liquid ATM {instrument_type} — skipping entry.")
            return None
        except Exception as e:
            logging.error(f"❌ Error getting contract for {self.index_name}: {e}")
            return None
