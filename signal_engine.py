"""
IADSS Signal Engine
-------------------
Tracks signals from TradingView for three IADSS models per symbol.
Fires a trade when all three agree within the confluence window.

Hierarchy (from IADSS docs):
  1. Confluence Confirmed Buy/Sell   → primary signal
  2. Mean Reversion Buy/Sell         → secondary confirmation
  3. Trend Model Buy/Sell            → MANDATORY last gate before entry/exit
  4. Optimized Trend Up/Down (15m)  → macro bias filter (only take longs with OT=UP)

Entry fires when: Confluence=BUY + MR=BUY + Trend=BUY (all within SIGNAL_WINDOW_SEC)
Exit fires when : Trend=SELL alone (fast flip exit) OR Confluence=SELL + MR=SELL
"""
import time
import logging
from collections import deque
from threading import Lock

logger = logging.getLogger(__name__)

# Model identifiers (must match the "mdl" field in TradingView alert JSON)
MODEL_CONF = "conf"
MODEL_MR = "mr"
MODEL_TREND = "trend"
MODEL_OT = "ot"   # Optimized Trend – macro filter only, no direct trades

SIGNAL_BUY = "buy"
SIGNAL_SELL = "sell"
SIGNAL_UP = "up"
SIGNAL_DOWN = "down"


class _SymbolState:
    """Per-symbol signal state."""

    def __init__(self, window: int):
        self.window = window
        self.conf: dict = {}
        self.mr: dict = {}
        self.trend: dict = {}
        self.macro_bias: str = "up"   # default: bullish until OT says otherwise
        self._last_entry: float = 0.0

    def update(self, model: str, signal: str, strength: str):
        data = {"signal": signal, "strength": strength, "ts": time.time()}
        if model == MODEL_CONF:
            self.conf = data
        elif model == MODEL_MR:
            self.mr = data
        elif model == MODEL_TREND:
            self.trend = data
        elif model == MODEL_OT:
            self.macro_bias = "up" if signal in (SIGNAL_BUY, SIGNAL_UP) else "down"

    def _fresh(self, d: dict, signal_val: str) -> bool:
        return bool(d) and d.get("signal") == signal_val and (time.time() - d["ts"]) <= self.window

    def has_buy_confluence(self) -> bool:
        """All three models showing fresh BUY within window."""
        return (
            self._fresh(self.conf, SIGNAL_BUY)
            and self._fresh(self.mr, SIGNAL_BUY)
            and self._fresh(self.trend, SIGNAL_BUY)
        )

    def has_sell_signal(self) -> bool:
        """Trend flip alone is sufficient for fast exit; or Conf+MR both sell."""
        trend_sell = self._fresh(self.trend, SIGNAL_SELL)
        both_sell = self._fresh(self.conf, SIGNAL_SELL) and self._fresh(self.mr, SIGNAL_SELL)
        return trend_sell or both_sell

    def in_cooldown(self, cooldown: int) -> bool:
        return (time.time() - self._last_entry) < cooldown

    def mark_entry(self):
        self._last_entry = time.time()
        # Clear signals so they don't retrigger immediately
        self.conf = {}
        self.mr = {}
        self.trend = {}


class SignalEngine:
    def __init__(self, config, position_mgr, broker, alerter, trade_logger=None):
        self.config = config
        self.position_mgr = position_mgr
        self.broker = broker
        self.alerter = alerter
        self.trade_logger = trade_logger
        self._lock = Lock()
        self._states: dict[str, _SymbolState] = {
            sym: _SymbolState(config.SIGNAL_WINDOW_SEC) for sym in config.ALL_SYMBOLS
        }
        self._webhook_log: deque = deque(maxlen=200)

    def process_signal(self, symbol: str, model: str, signal: str, price: float, strength: str = "confirmed"):
        """Called by webhook server for every incoming TradingView alert."""
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

        # Skip unconfirmed confluence signals – they create too many false positives on short TFs
        if model == MODEL_CONF and strength == "unconfirmed":
            logger.debug(f"Unconfirmed confluence {symbol} – skipped")
            return

        logger.info(f"[{symbol}] {model} → {signal} ({strength}) @ ${price:.4f}")

        with self._lock:
            state = self._states[symbol]
            state.update(model, signal, strength)

            # Exit check first (protect open positions)
            if self.position_mgr.lot_count(symbol) > 0 and state.has_sell_signal():
                self._exit_all(symbol, f"IADSS sell ({model})")
                return

            # Entry check
            if self._can_enter(symbol, state):
                self._enter(symbol, price, state)

    # ── Entry gate ─────────────────────────────────────────────────────────────

    def _can_enter(self, symbol: str, state: _SymbolState) -> bool:
        if not state.has_buy_confluence():
            return False
        if state.macro_bias == "down":
            logger.info(f"{symbol}: macro OT=DOWN – skipping long entry")
            return False
        if self.position_mgr.daily_loss_limit_reached():
            logger.warning(f"{symbol}: daily loss limit reached")
            return False
        if self.position_mgr.lot_count(symbol) >= self.config.MAX_LOTS_PER_SYMBOL:
            logger.info(f"{symbol}: max lots ({self.config.MAX_LOTS_PER_SYMBOL}) reached")
            return False
        if state.in_cooldown(self.config.ENTRY_COOLDOWN_SEC):
            logger.info(f"{symbol}: entry cooldown active")
            return False
        # Stocks need market to be open; crypto trades 24/7
        if not self.config.is_crypto(symbol) and not self.broker.is_market_open():
            logger.info(f"{symbol}: stock market closed")
            return False
        return True

    def _enter(self, symbol: str, signal_price: float, state: _SymbolState):
        lot_usd = self.config.LOT_SIZE_USD
        result = self.broker.buy(symbol, lot_usd)
        if not result:
            logger.error(f"{symbol}: buy order failed")
            return

        fill_price = result.get("fill_price") or signal_price
        fill_qty = result.get("fill_qty") or (lot_usd / signal_price)
        lot = self.position_mgr.add_lot(symbol, fill_qty, fill_price)
        state.mark_entry()

        open_count = self.position_mgr.lot_count(symbol)
        msg = (
            f"🟢 <b>ENTRY</b> {symbol}\n"
            f"Lot: {lot.lot_id} | ${lot_usd:.0f}\n"
            f"Fill: ${fill_price:.4f} × {fill_qty:.6f}\n"
            f"SL: ${lot.stop_price:.4f} (−{self.config.STOP_LOSS_PCT}%)\n"
            f"TP: ${lot.take_profit_price:.4f} (+{self.config.TAKE_PROFIT_PCT}%)\n"
            f"Lots open: {open_count}/{self.config.MAX_LOTS_PER_SYMBOL}"
        )
        logger.info(msg.replace("<b>", "").replace("</b>", ""))
        self.alerter.send(msg)

    # ── Exit ───────────────────────────────────────────────────────────────────

    def _exit_all(self, symbol: str, reason: str):
        """Called from signal engine or risk manager to close all lots."""
        lots = self.position_mgr.pop_all_lots(symbol)
        if not lots:
            return

        current_price = self.broker.get_price(symbol)
        closed = self.broker.close_position(symbol)

        if closed and current_price:
            total_qty = sum(l.qty for l in lots)
            avg_entry = sum(l.entry_price * l.qty for l in lots) / total_qty
            pnl = (current_price - avg_entry) * total_qty
            pnl_pct = (current_price - avg_entry) / avg_entry * 100
            self.position_mgr.record_trade_result(pnl)

            if self.trade_logger:
                for lot in lots:
                    lot_pnl = (current_price - lot.entry_price) * lot.qty
                    lot_pnl_pct = (current_price - lot.entry_price) / lot.entry_price * 100
                    self.trade_logger.log_trade(
                        symbol=symbol, lot_id=lot.lot_id, entry_time=lot.entry_time,
                        entry_price=lot.entry_price, exit_price=current_price, qty=lot.qty,
                        pnl_usd=lot_pnl, pnl_pct=lot_pnl_pct, reason=reason,
                    )

            emoji = "✅" if pnl >= 0 else "🔴"
            msg = (
                f"{emoji} <b>EXIT</b> {symbol}\n"
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

    # ── Diagnostics ────────────────────────────────────────────────────────────

    def get_webhook_log(self) -> list:
        """Last 200 webhook hits, newest first."""
        return list(reversed(self._webhook_log))

    def get_signal_state(self) -> dict:
        """Current per-symbol signal state for the dashboard diagnostics panel."""
        now = time.time()

        def fmt(d, window):
            if not d:
                return {"signal": None, "age_sec": None, "fresh": False, "strength": None}
            age = now - d["ts"]
            return {
                "signal":   d["signal"],
                "age_sec":  round(age),
                "fresh":    age <= window,
                "strength": d.get("strength"),
            }

        result = {}
        with self._lock:
            for sym, state in self._states.items():
                result[sym] = {
                    "conf":          fmt(state.conf,  state.window),
                    "mr":            fmt(state.mr,    state.window),
                    "trend":         fmt(state.trend, state.window),
                    "macro_bias":    state.macro_bias,
                    "has_confluence": state.has_buy_confluence(),
                    "lot_count":     self.position_mgr.lot_count(sym),
                }
        return result

    def exit_one_lot(self, symbol: str, reason: str):
        """Called by risk manager for LIFO take-profit (newest lot only)."""
        lot = self.position_mgr.pop_newest_lot(symbol)
        if not lot:
            return
        current_price = self.broker.get_price(symbol)
        result = self.broker.sell_qty(symbol, lot.qty)
        if result and current_price:
            pnl = (current_price - lot.entry_price) * lot.qty
            pnl_pct = (current_price - lot.entry_price) / lot.entry_price * 100
            self.position_mgr.record_trade_result(pnl)
            if self.trade_logger:
                self.trade_logger.log_trade(
                    symbol=symbol, lot_id=lot.lot_id, entry_time=lot.entry_time,
                    entry_price=lot.entry_price, exit_price=current_price, qty=lot.qty,
                    pnl_usd=pnl, pnl_pct=pnl_pct, reason=reason,
                )
            remaining = self.position_mgr.lot_count(symbol)
            msg = (
                f"💰 <b>TAKE-PROFIT</b> {symbol} [{lot.lot_id}]\n"
                f"Reason: {reason}\n"
                f"${lot.entry_price:.4f} → ${current_price:.4f} (+{pnl_pct:.2f}%)\n"
                f"P&amp;L: +${pnl:.2f}\n"
                f"Remaining lots: {remaining}"
            )
            logger.info(msg.replace("<b>", "").replace("</b>", "").replace("&amp;", "&"))
            self.alerter.send(msg)
