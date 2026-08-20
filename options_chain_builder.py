import logging
import json
import os
from datetime import datetime
from config import FALLBACK_LOT_SIZE


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

    def _pack(self, candidate, expiry_date):
        return {
            "symbol": candidate.get("symbol"),
            "token": candidate.get("token") or candidate.get("symboltoken"),
            "strike": candidate.get("parsed_strike"),
            "expiry": expiry_date.strftime("%d%b%Y").upper(),
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
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
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

    def get_nearest_expiry_contract(self, spot_price, instrument_type="CE"):
        """Nearest-expiry ATM option. Returns None if liquidity cannot be verified."""
        try:
            if not self.nfo_contracts:
                self.load_scrip_master()
                if not self.nfo_contracts:
                    return None

            valid_contracts = []
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

            for c in self.nfo_contracts:
                exp_str = c.get("expiry")
                if not exp_str:
                    continue
                parsed_date = self._parse_expiry(exp_str)
                if parsed_date and parsed_date >= today:
                    c["parsed_expiry_date"] = parsed_date
                    valid_contracts.append(c)

            if not valid_contracts:
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
                        continue
                    q_data = (q_resp.get("data") or {}).get("fetched", [{}])[0]
                    volume = float(q_data.get("tradeVolume", 0) or 0)
                    best_bid = float(q_data.get("bestBidPrice") or ltp * 0.99)
                    best_ask = float(q_data.get("bestAskPrice") or ltp * 1.01)
                    spread_pct = ((best_ask - best_bid) / ltp) * 100 if ltp > 0 else 99.0
                    if volume >= 500 and spread_pct <= 1.5:
                        logging.info(
                            f"[LIQUIDITY PASSED] {contract_symbol} | Vol: {volume} | Spread: {spread_pct:.2f}% | lot {packed['lotsize']}"
                        )
                        return packed
                    logging.warning(
                        f"[LIQUIDITY REJECTED] {contract_symbol} (Vol: {volume}, Spread: {spread_pct:.2f}%)"
                    )
                except Exception as ex:
                    logging.warning(f"Could not verify depth for {contract_symbol}: {ex}")
                    continue

            logging.warning(f"[{self.index_name}] No liquid ATM {instrument_type} — skipping entry.")
            return None
        except Exception as e:
            logging.error(f"❌ Error getting contract for {self.index_name}: {e}")
            return None
