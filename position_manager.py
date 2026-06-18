"""LIFO position tracking – mirrors real Alpaca lots locally for fast SL/TP checks."""
import time
import logging
from dataclasses import dataclass, field
from threading import Lock
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Lot:
    lot_id: str
    symbol: str
    qty: float
    entry_price: float
    stop_price: float
    take_profit_price: float
    entry_time: float = field(default_factory=time.time)


class PositionManager:
    def __init__(self, config):
        self.config = config
        self._lock = Lock()
        # symbol → list[Lot], index 0=oldest, index -1=newest (LIFO pops from -1)
        self._lots: dict[str, List[Lot]] = {}
        self._daily_pnl: float = 0.0
        self._daily_losses: int = 0
        self._counter: int = 0

    # ── Lot lifecycle ──────────────────────────────────────────────────────────

    def add_lot(self, symbol: str, qty: float, fill_price: float) -> Lot:
        with self._lock:
            self._counter += 1
            lot = Lot(
                lot_id=f"LOT-{self._counter:04d}",
                symbol=symbol,
                qty=qty,
                entry_price=fill_price,
                stop_price=fill_price * (1 - self.config.STOP_LOSS_PCT / 100),
                take_profit_price=fill_price * (1 + self.config.TAKE_PROFIT_PCT / 100),
            )
            self._lots.setdefault(symbol, []).append(lot)
        logger.info(
            f"{lot.lot_id} {symbol} qty={qty:.6f} @ ${fill_price:.4f} "
            f"SL=${lot.stop_price:.4f} TP=${lot.take_profit_price:.4f}"
        )
        return lot

    def pop_newest_lot(self, symbol: str) -> Optional[Lot]:
        """LIFO: remove and return newest lot."""
        with self._lock:
            lots = self._lots.get(symbol, [])
            return lots.pop() if lots else None

    def pop_all_lots(self, symbol: str) -> List[Lot]:
        """Remove and return all lots for symbol (oldest first)."""
        with self._lock:
            return self._lots.pop(symbol, [])

    # ── Queries ────────────────────────────────────────────────────────────────

    def get_lots(self, symbol: str) -> List[Lot]:
        with self._lock:
            return list(self._lots.get(symbol, []))

    def lot_count(self, symbol: str) -> int:
        with self._lock:
            return len(self._lots.get(symbol, []))

    def get_all_open_symbols(self) -> List[str]:
        with self._lock:
            return [s for s, lots in self._lots.items() if lots]

    def avg_entry_price(self, symbol: str) -> Optional[float]:
        with self._lock:
            lots = self._lots.get(symbol, [])
            if not lots:
                return None
            total_qty = sum(l.qty for l in lots)
            return sum(l.entry_price * l.qty for l in lots) / total_qty

    # ── Daily tracking ─────────────────────────────────────────────────────────

    def record_trade_result(self, pnl_usd: float):
        with self._lock:
            self._daily_pnl += pnl_usd
            if pnl_usd < 0:
                self._daily_losses += 1

    def daily_loss_limit_reached(self) -> bool:
        with self._lock:
            return (
                self._daily_losses >= self.config.MAX_DAILY_LOSSES
                or self._daily_pnl <= -self.config.DAILY_DRAWDOWN_USD
            )

    def reset_daily_stats(self):
        with self._lock:
            self._daily_pnl = 0.0
            self._daily_losses = 0
        logger.info("Daily stats reset")

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    @property
    def daily_losses(self) -> int:
        return self._daily_losses
