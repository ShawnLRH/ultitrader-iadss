"""Persists every closed lot to trades.csv for dashboard and analysis."""
import csv
import logging
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)

FIELDS = [
    "exit_time", "symbol", "lot_id", "entry_time",
    "entry_price", "exit_price", "qty",
    "pnl_usd", "pnl_pct", "reason",
]


SEED_FILE = Path(__file__).parent / "trades_seed.csv"


class TradeLogger:
    def __init__(self, filepath: str = "trades.csv"):
        self.filepath = Path(filepath)
        self._lock = Lock()
        if not self.filepath.exists():
            # Restore known-good historical trades from seed so deploys don't wipe history
            if SEED_FILE.exists():
                import shutil
                shutil.copy(SEED_FILE, self.filepath)
                logger.info(f"Seeded {self.filepath} from {SEED_FILE}")
            else:
                with open(self.filepath, "w", newline="") as f:
                    csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def log_trade(
        self, symbol: str, lot_id: str, entry_time: float,
        entry_price: float, exit_price: float, qty: float,
        pnl_usd: float, pnl_pct: float, reason: str,
    ):
        row = {
            "exit_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": symbol,
            "lot_id": lot_id,
            "entry_time": datetime.fromtimestamp(entry_time, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            "entry_price": round(entry_price, 6),
            "exit_price": round(exit_price, 6),
            "qty": round(qty, 8),
            "pnl_usd": round(pnl_usd, 4),
            "pnl_pct": round(pnl_pct, 4),
            "reason": reason,
        }
        with self._lock:
            with open(self.filepath, "a", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
        logger.info(f"TRADE LOG {symbol} {lot_id}: P&L ${pnl_usd:+.2f} ({pnl_pct:+.2f}%)")

    def get_all_trades(self) -> list[dict]:
        with self._lock:
            if not self.filepath.exists():
                return []
            with open(self.filepath, "r", newline="") as f:
                return list(csv.DictReader(f))

    def get_stats(self) -> dict:
        trades = self.get_all_trades()
        if not trades:
            return {
                "total_trades": 0, "wins": 0, "losses": 0, "win_rate": 0.0,
                "total_pnl": 0.0, "avg_pnl": 0.0, "best_trade": 0.0, "worst_trade": 0.0,
                "per_symbol": {},
            }

        pnls = [float(t["pnl_usd"]) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        per_symbol: dict[str, dict] = {}
        for t in trades:
            sym = t["symbol"]
            pnl = float(t["pnl_usd"])
            if sym not in per_symbol:
                per_symbol[sym] = {"trades": 0, "wins": 0, "pnl": 0.0}
            per_symbol[sym]["trades"] += 1
            per_symbol[sym]["pnl"] = round(per_symbol[sym]["pnl"] + pnl, 4)
            if pnl > 0:
                per_symbol[sym]["wins"] += 1

        return {
            "total_trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(trades) * 100, 1),
            "total_pnl": round(sum(pnls), 2),
            "avg_pnl": round(sum(pnls) / len(pnls), 2),
            "best_trade": round(max(pnls), 2),
            "worst_trade": round(min(pnls), 2),
            "per_symbol": per_symbol,
        }
