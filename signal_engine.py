"""
IADSS Signal Engine
-------------------
Tracks signals from TradingView for three IADSS models per symbol.

Entry rules (STATE-BASED — no time window):
  LONG:  Conf=BUY + any of (MR=BUY, Trend=BUY, OT=BUY) currently active
  SHORT: Conf=SELL + any of (MR=SELL, Trend=SELL, OT=SELL) currently active
  All 4 signals hold their state until the OPPOSITE fires (blue stays blue
  until orange; green stays green until red). Matches TradingView visual exactly.
  (stocks only for shorts; crypto is long-only on Alpaca)

Exit rules (TIME-BOUNDED — SIGNAL_WINDOW_SEC freshness required):
  LONG exit fast:  fresh Trend=SELL OR OT=SELL, only if unrealized P&L >= +$3
  LONG exit full:  fresh Conf=SELL + any fresh (MR/Trend/OT)=SELL — always exits
  SHORT exit:      fresh Conf=BUY + any fresh (MR/Trend/OT)=BUY — cover
  SL/TP:           risk manager checks every 30s independently

Entry guards:
  - Stock entries blocked within OPEN_BUFFER_SEC of 9:30 AM ET (default 30 min)
  - OT macro is advisory only — logged but never blocks a trade
"""
import time
import datetime
import logging
from collections import deque
from threading import Lock

logger = logging.getLogger(__name__)

MODEL_CONF  = "conf"
MODEL_MR    = "mr"
MODEL_TREND = "trend"
MODEL_OT    = "ot"

SIGNAL_BUY  = "buy"
SIGNAL_SELL = "sell"
SIGNAL_UP   = "up"
SIGNAL_DOWN = "down"


class _SymbolState:
    """Per-symbol signal state."""

    def __init__(self, window: int):
        self.window = window
        self.conf:  dict = {}
        self.mr:    dict = {}
        self.trend: dict = {}
        self.ot:    dict = {}   # Optimised Trend — state-tracked same as Trend
        self._last_entry: float = 0.0

    def update(self, model: str, signal: str, strength: str):
        # Normalise OT "up"/"down" to buy/sell so _active() checks are uniform
        normalised = SIGNAL_BUY if signal in (SIGNAL_BUY, SIGNAL_UP) else SIGNAL_SELL
        data = {"signal": normalised, "strength": strength, "ts": time.time()}
        if model == MODEL_CONF:
            self.conf = data
        elif model == MODEL_MR:
            self.mr = data
        elif model == MODEL_TREND:
            self.trend = data
        elif model == MODEL_OT:
            self.ot = data

    @property
    def macro_bias(self) -> str:
        """Derived from OT state for backward-compat display."""
        return "up" if self._active(self.ot, SIGNAL_BUY) else "down"

    def _fresh(self, d: dict, signal_val: str) -> bool:
        """Time-bounded: signal matches AND arrived within SIGNAL_WINDOW_SEC. Used for exits."""
        return bool(d) and d.get("signal") == signal_val and (time.time() - d["ts"]) <= self.window

    def _active(self, d: dict, signal_val: str) -> bool:
        """State-based: signal matches regardless of age. Valid until opposite fires.
        IADSS indicators hold state across bars — blue stays blue until orange fires."""
        return bool(d) and d.get("signal") == signal_val

    def has_buy_confluence(self) -> bool:
        """Entry (state-based): Conf=BUY + any of (MR=BUY, Trend=BUY, OT=BUY) active.
        Matches TradingView visual — all indicators green = valid entry."""
        conf_buy = self._active(self.conf, SIGNAL_BUY)
        secondary = (
            self._active(self.mr,    SIGNAL_BUY) or
            self._active(self.trend, SIGNAL_BUY) or
            self._active(self.ot,    SIGNAL_BUY)
        )
        return conf_buy and secondary

    def has_short_confluence(self) -> bool:
        """Entry (state-based): Conf=SELL + any of (MR=SELL, Trend=SELL, OT=SELL) active."""
        conf_sell = self._active(self.conf, SIGNAL_SELL)
        secondary = (
            self._active(self.mr,    SIGNAL_SELL) or
            self._active(self.trend, SIGNAL_SELL) or
            self._active(self.ot,    SIGNAL_SELL)
        )
        return conf_sell and secondary

    def has_trend_sell_signal(self) -> bool:
        """Exit (time-bounded): fresh Trend=SELL OR fresh OT=SELL. Profit gate at SignalEngine."""
        return self._fresh(self.trend, SIGNAL_SELL) or self._fresh(self.ot, SIGNAL_SELL)

    def has_full_exit_signal(self) -> bool:
        """Exit (time-bounded): fresh Conf=SELL + any fresh secondary SELL — always exits."""
        conf_ok = self._fresh(self.conf, SIGNAL_SELL)
        secondary_ok = (
            self._fresh(self.mr,    SIGNAL_SELL) or
            self._fresh(self.trend, SIGNAL_SELL) or
            self._fresh(self.ot,    SIGNAL_SELL)
        )
        return conf_ok and secondary_ok

    def in_cooldown(self, cooldown: int) -> bool:
        return (time.time() - self._last_entry) < cooldown

    def mark_entry(self):
        self._last_entry = time.time()
        self.conf  = {}
        self.mr    = {}
        self.trend = {}
        self.ot    = {}


class SignalEngine:
    def __init__(self, config, position_mgr, broker, alerter, trade_logger=None):
        self.config       = config
        self.position_mgr = position_mgr
        self.broker       = broker
        self.alerter      = alerter
        self.trade_logger = trade_logger
        self._lock  = Lock()
        self._states: dict[str, _SymbolState] = {
            sym: _SymbolState(config.SIGNAL_WINDOW_SEC) for sym in config.ALL_SYMBOLS
        }
        self._webhook_log: deque = deque(maxlen=200)

    def process_signal(self, symbol: str, model: str, signal: str, price: float, strength: str = "confirmed"):
        raw_symbol = symbol
        symbol = self.config.normalize_symbol(symbol)
        ignored = symbol not in self._states

        self._webhook_log.append({
            "ts":       time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
            "symbol":   symbol,
            "raw":      raw_symbol if raw_symbol.upper() != symbol else "",
            "model":    model,
            "signal":   signal,
            "price":    round(price, 4) if price else 0,
            "strength": strength,
            "ignored":  ignored,
        })

        if ignored:
            logger.warning(f"Ignored unknown symbol: {symbol}")
            return

        if model == MODEL_CONF and strength == "unconfirmed":
            logger.debug(f"Unconfirmed confluence {symbol} – skipped")
            return

        logger.info(f"[{symbol}] {model} → {signal} ({strength}) @ ${price:.4f}")

        with self._lock:
            state = self._states[symbol]
            state.update(model, signal, strength)

            has_long  = self.position_mgr.has_long_position(symbol)
            has_short = self.position_mgr.has_short_position(symbol)

            # Log macro bias (advisory only — no longer blocks trades)
            if model == MODEL_OT:
                logger.info(f"[{symbol}] OT macro → {state.macro_bias} (advisory)")

            # Exit LONG on sell signal
            if has_long:
                if state.has_full_exit_signal():
                    self._exit_position(symbol, f"IADSS sell (conf+mr)")
                    return
                if state.has_trend_sell_signal():
                    pnl = self._get_unrealized_pnl(symbol)
                    if pnl >= self.config.TREND_EXIT_MIN_PROFIT_USD:
                        self._exit_position(symbol, f"IADSS sell (trend) @ +${pnl:.2f}")
                        return

            # Exit SHORT on buy confluence
            if has_short and state.has_buy_confluence():
                self._exit_position(symbol, f"IADSS buy ({model}) — cover short")
                return

            # Enter LONG
            if not has_long and not has_short and self._can_enter_long(symbol, state):
                self._enter_long(symbol, price, state)
                return

            # Enter SHORT (stocks only; Alpaca crypto is long-only)
            if (not has_long and not has_short
                    and self.config.ALLOW_SHORTS
                    and not self.config.is_crypto(symbol)
                    and self._can_enter_short(symbol, state)):
                self._enter_short(symbol, price, state)

    # ── P&L helper ─────────────────────────────────────────────────────────────

    def _get_unrealized_pnl(self, symbol: str) -> float:
        """Sum unrealized P&L across all open lots for symbol."""
        lots = self.position_mgr.get_lots(symbol)
        if not lots:
            return 0.0
        current_price = self.broker.get_price(symbol)
        if not current_price:
            return 0.0
        total = 0.0
        for lot in lots:
            if lot.direction == "long":
                total += (current_price - lot.entry_price) * lot.qty
            else:
                total += (lot.entry_price - current_price) * lot.qty
        return total

    # ── Entry gates ────────────────────────────────────────────────────────────

    def _in_open_buffer(self) -> bool:
        """True if within OPEN_BUFFER_SEC of 9:30 AM ET (stocks only)."""
        try:
            from zoneinfo import ZoneInfo
            now_et = datetime.datetime.now(tz=ZoneInfo("America/New_York"))
        except Exception:
            # zoneinfo unavailable — skip buffer
            return False
        open_et = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        elapsed = (now_et - open_et).total_seconds()
        return 0 <= elapsed < self.config.OPEN_BUFFER_SEC

    def _can_enter_long(self, symbol: str, state: _SymbolState) -> bool:
        if not state.has_buy_confluence():
            return False
        if self.position_mgr.daily_loss_limit_reached():
            logger.warning(f"{symbol}: daily loss limit reached")
            return False
        if self.position_mgr.lot_count(symbol) >= self.config.MAX_LOTS_PER_SYMBOL:
            return False
        if state.in_cooldown(self.config.ENTRY_COOLDOWN_SEC):
            logger.info(f"{symbol}: cooldown active")
            return False
        if not self.config.is_crypto(symbol) and not self.broker.is_market_open():
            return False
        if not self.config.is_crypto(symbol) and self._in_open_buffer():
            logger.info(f"{symbol}: market-open buffer active ({self.config.OPEN_BUFFER_SEC}s)")
            return False
        return True

    def _can_enter_short(self, symbol: str, state: _SymbolState) -> bool:
        if not state.has_short_confluence():
            return False
        if self.position_mgr.daily_loss_limit_reached():
            return False
        if self.position_mgr.lot_count(symbol) >= self.config.MAX_LOTS_PER_SYMBOL:
            return False
        if state.in_cooldown(self.config.ENTRY_COOLDOWN_SEC):
            return False
        if not self.broker.is_market_open():
            return False
        if self._in_open_buffer():
            logger.info(f"{symbol}: market-open buffer active ({self.config.OPEN_BUFFER_SEC}s)")
            return False
        return True

    # ── Entry execution ────────────────────────────────────────────────────────

    def _enter_long(self, symbol: str, signal_price: float, state: _SymbolState):
        lot_usd = self.config.LOT_SIZE_USD
        logger.info(f"{symbol}: attempting LONG buy ${lot_usd:.0f} @ signal ${signal_price:.4f}")
        result = self.broker.buy(symbol, lot_usd)
        if not result:
            msg = f"⚠️ {symbol}: LONG buy order FAILED (Alpaca rejected or timed out)"
            logger.error(msg)
            self.alerter.send(msg)
            return

        _fp = result.get("fill_price")
        _fq = result.get("fill_qty")
        fill_price = _fp if _fp else (self.broker.get_price(symbol) or signal_price)
        fill_qty   = _fq if _fq else (lot_usd / fill_price)
        lot = self.position_mgr.add_lot(symbol, fill_qty, fill_price, direction="long")
        state.mark_entry()

        msg = (
            f"🟢 <b>LONG ENTRY</b> {symbol}\n"
            f"Lot: {lot.lot_id} | ${lot_usd:.0f}\n"
            f"Fill: ${fill_price:.4f} × {fill_qty:.6f}\n"
            f"SL: ${lot.stop_price:.4f} (−{self.config.STOP_LOSS_PCT}%)\n"
            f"TP: ${lot.take_profit_price:.4f} (+{self.config.TAKE_PROFIT_PCT}%)\n"
            f"OT macro: {state.macro_bias.upper()}"
        )
        logger.info(msg.replace("<b>", "").replace("</b>", ""))
        self.alerter.send(msg)

    def _enter_short(self, symbol: str, signal_price: float, state: _SymbolState):
        lot_usd = self.config.LOT_SIZE_USD
        logger.info(f"{symbol}: attempting SHORT sell ${lot_usd:.0f} @ signal ${signal_price:.4f}")
        result = self.broker.short(symbol, lot_usd)
        if not result:
            msg = f"⚠️ {symbol}: SHORT order FAILED (Alpaca rejected or timed out)"
            logger.error(msg)
            self.alerter.send(msg)
            return

        _fp = result.get("fill_price")
        _fq = result.get("fill_qty")
        fill_price = _fp if _fp else (self.broker.get_price(symbol) or signal_price)
        fill_qty   = _fq if _fq else (lot_usd / fill_price)
        lot = self.position_mgr.add_lot(symbol, fill_qty, fill_price, direction="short")
        state.mark_entry()

        msg = (
            f"🔴 <b>SHORT ENTRY</b> {symbol}\n"
            f"Lot: {lot.lot_id} | ${lot_usd:.0f}\n"
            f"Fill: ${fill_price:.4f} × {fill_qty:.6f}\n"
            f"SL: ${lot.stop_price:.4f} (+{self.config.STOP_LOSS_PCT}%)\n"
            f"TP: ${lot.take_profit_price:.4f} (−{self.config.TAKE_PROFIT_PCT}%)\n"
            f"OT macro: {state.macro_bias.upper()}"
        )
        logger.info(msg.replace("<b>", "").replace("</b>", ""))
        self.alerter.send(msg)

    # ── Exit ───────────────────────────────────────────────────────────────────

    def _exit_position(self, symbol: str, reason: str):
        """Close all lots for a symbol (handles both long and short)."""
        lots = self.position_mgr.pop_all_lots(symbol)
        if not lots:
            return

        current_price = self.broker.get_price(symbol)
        closed = self.broker.close_position(symbol)

        if closed and current_price:
            direction  = lots[0].direction
            total_qty  = sum(l.qty for l in lots)
            avg_entry  = sum(l.entry_price * l.qty for l in lots) / total_qty
            if direction == "long":
                pnl     = (current_price - avg_entry) * total_qty
                pnl_pct = (current_price - avg_entry) / avg_entry * 100
            else:
                pnl     = (avg_entry - current_price) * total_qty
                pnl_pct = (avg_entry - current_price) / avg_entry * 100

            self.position_mgr.record_trade_result(pnl)

            if self.trade_logger:
                for lot in lots:
                    if lot.direction == "long":
                        lot_pnl     = (current_price - lot.entry_price) * lot.qty
                        lot_pnl_pct = (current_price - lot.entry_price) / lot.entry_price * 100
                    else:
                        lot_pnl     = (lot.entry_price - current_price) * lot.qty
                        lot_pnl_pct = (lot.entry_price - current_price) / lot.entry_price * 100
                    self.trade_logger.log_trade(
                        symbol=symbol, lot_id=lot.lot_id, entry_time=lot.entry_time,
                        entry_price=lot.entry_price, exit_price=current_price, qty=lot.qty,
                        pnl_usd=lot_pnl, pnl_pct=lot_pnl_pct, reason=reason,
                    )

            dir_label = "LONG" if direction == "long" else "SHORT"
            emoji = "✅" if pnl >= 0 else "🔴"
            msg = (
                f"{emoji} <b>EXIT {dir_label}</b> {symbol}\n"
                f"Reason: {reason}\n"
                f"Avg entry ${avg_entry:.4f} → ${current_price:.4f}\n"
                f"P&amp;L: ${pnl:+.2f} ({pnl_pct:+.2f}%)\n"
                f"Lots closed: {len(lots)}\n"
                f"Daily P&amp;L: ${self.position_mgr.daily_pnl:+.2f}"
            )
            logger.info(msg.replace("<b>", "").replace("</b>", "").replace("&amp;", "&"))
            self.alerter.send(msg)
        else:
            logger.error(f"{symbol}: close_position failed or price unavailable")

    # ── Risk-manager API ───────────────────────────────────────────────────────

    def exit_position(self, symbol: str, reason: str):
        """Public: called by risk manager for SL hit."""
        self._exit_position(symbol, reason)

    def exit_one_lot(self, symbol: str, reason: str):
        """LIFO take-profit: close newest lot only."""
        lots = self.position_mgr.get_lots(symbol)
        if not lots:
            return
        direction = lots[-1].direction
        lot = self.position_mgr.pop_newest_lot(symbol)
        if not lot:
            return

        current_price = self.broker.get_price(symbol)
        if direction == "long":
            result = self.broker.sell_qty(symbol, lot.qty)
            pnl     = (current_price - lot.entry_price) * lot.qty if current_price else 0
            pnl_pct = (current_price - lot.entry_price) / lot.entry_price * 100 if current_price else 0
        else:
            result = self.broker.buy_qty(symbol, lot.qty)
            pnl     = (lot.entry_price - current_price) * lot.qty if current_price else 0
            pnl_pct = (lot.entry_price - current_price) / lot.entry_price * 100 if current_price else 0

        if result and current_price:
            self.position_mgr.record_trade_result(pnl)
            if self.trade_logger:
                self.trade_logger.log_trade(
                    symbol=symbol, lot_id=lot.lot_id, entry_time=lot.entry_time,
                    entry_price=lot.entry_price, exit_price=current_price, qty=lot.qty,
                    pnl_usd=pnl, pnl_pct=pnl_pct, reason=reason,
                )
            remaining = self.position_mgr.lot_count(symbol)
            dir_label = "LONG" if direction == "long" else "SHORT"
            msg = (
                f"💰 <b>TAKE-PROFIT {dir_label}</b> {symbol} [{lot.lot_id}]\n"
                f"Reason: {reason}\n"
                f"${lot.entry_price:.4f} → ${current_price:.4f} ({pnl_pct:+.2f}%)\n"
                f"P&amp;L: +${pnl:.2f}\n"
                f"Remaining lots: {remaining}"
            )
            logger.info(msg.replace("<b>", "").replace("</b>", "").replace("&amp;", "&"))
            self.alerter.send(msg)

    # ── Diagnostics ────────────────────────────────────────────────────────────

    def get_webhook_log(self) -> list:
        return list(reversed(self._webhook_log))

    def get_signal_state(self) -> dict:
        now = time.time()

        def fmt(d, window):
            if not d:
                return {"signal": None, "age_sec": None, "fresh": False, "active": False, "strength": None}
            age = now - d["ts"]
            return {
                "signal":   d["signal"],
                "age_sec":  round(age),
                "fresh":    age <= window,   # within SIGNAL_WINDOW_SEC (used for exits)
                "active":   True,            # state-based: valid until opposite fires (used for entries)
                "strength": d.get("strength"),
            }

        result = {}
        with self._lock:
            for sym, state in self._states.items():
                result[sym] = {
                    "conf":              fmt(state.conf,  state.window),
                    "mr":                fmt(state.mr,    state.window),
                    "trend":             fmt(state.trend, state.window),
                    "ot":                fmt(state.ot,    state.window),
                    "macro_bias":        state.macro_bias,
                    "has_long_entry":    state.has_buy_confluence(),
                    "has_short_entry":   state.has_short_confluence(),
                    "lot_count":         self.position_mgr.lot_count(sym),
                    "direction":         (
                        "long"  if self.position_mgr.has_long_position(sym)  else
                        "short" if self.position_mgr.has_short_position(sym) else
                        "flat"
                    ),
                }
        return result
