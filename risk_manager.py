"""
Background monitor loop – checks every MONITOR_INTERVAL_SEC:
  • Stop-loss:   price ≤ any lot's stop_price  → close ALL lots (cut losses fast)
  • Take-profit: price ≥ newest lot's TP price  → sell that one lot (LIFO partial exit)
  • Daily reset: midnight UTC resets loss counter and daily P&L
"""
import time
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class RiskManager:
    def __init__(self, config, broker, position_mgr, signal_engine, alerter):
        self.config = config
        self.broker = broker
        self.position_mgr = position_mgr
        self.signal_engine = signal_engine
        self.alerter = alerter
        self._last_day: int = datetime.now(timezone.utc).day

    def run(self):
        """Blocking loop – run in daemon thread."""
        logger.info(f"Risk monitor started (interval={self.config.MONITOR_INTERVAL_SEC}s)")
        while True:
            try:
                self._daily_reset_check()
                self._check_all_positions()
            except Exception as e:
                logger.error(f"Risk monitor error: {e}")
            time.sleep(self.config.MONITOR_INTERVAL_SEC)

    # ── Internal ───────────────────────────────────────────────────────────────

    def _daily_reset_check(self):
        today = datetime.now(timezone.utc).day
        if today != self._last_day:
            self._last_day = today
            self.position_mgr.reset_daily_stats()
            self.alerter.send(
                f"🔄 Daily stats reset\n"
                f"New day: {datetime.now(timezone.utc).strftime('%Y-%m-%d UTC')}"
            )

    def _check_all_positions(self):
        symbols = self.position_mgr.get_all_open_symbols()
        for symbol in symbols:
            lots = self.position_mgr.get_lots(symbol)
            if not lots:
                continue

            price = self.broker.get_price(symbol)
            if price is None:
                continue

            # Stop-loss: if ANY lot's SL is breached, close everything
            for lot in lots:
                if price <= lot.stop_price:
                    loss_pct = (price - lot.entry_price) / lot.entry_price * 100
                    logger.warning(
                        f"SL HIT {symbol}: price=${price:.4f} ≤ stop=${lot.stop_price:.4f} "
                        f"({loss_pct:.2f}%)"
                    )
                    self.signal_engine._exit_all(
                        symbol, f"STOP-LOSS @ ${price:.4f} ({loss_pct:.2f}%)"
                    )
                    break

            # Take-profit: only check newest lot (LIFO); stop-loss may have just closed it
            remaining = self.position_mgr.get_lots(symbol)
            if remaining:
                newest = remaining[-1]
                if price >= newest.take_profit_price:
                    gain_pct = (price - newest.entry_price) / newest.entry_price * 100
                    logger.info(
                        f"TP HIT {symbol}: price=${price:.4f} ≥ tp=${newest.take_profit_price:.4f} "
                        f"(+{gain_pct:.2f}%)"
                    )
                    self.signal_engine.exit_one_lot(
                        symbol, f"TAKE-PROFIT @ ${price:.4f} (+{gain_pct:.2f}%)"
                    )
