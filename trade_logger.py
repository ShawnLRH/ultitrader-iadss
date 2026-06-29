"""Persists every closed lot to trades.csv for dashboard and analysis."""
import csv
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


FIELDS = [
    "exit_time", "symbol", "lot_id", "entry_time",
    "entry_price", "exit_price", "qty",
    "pnl_usd", "pnl_pct", "reason",
]


def _reconstruct_trades_from_orders(orders: list) -> list:
    """FIFO reconstruction of closed round-trip trades from raw Alpaca fills.

    Handles longs (buy->sell) and shorts (sell->buy). Returns trade dicts
    matching FIELDS schema.
    """
    by_symbol = defaultdict(list)
    for o in orders:
        by_symbol[o["symbol"]].append(o)

    trades = []

    for symbol, sym_orders in by_symbol.items():
        sym_orders.sort(key=lambda x: x["filled_at"])
        long_q  = []   # [(qty, price, iso_time)]
        short_q = []

        for o in sym_orders:
            qty   = float(o["filled_qty"])
            price = float(o["filled_avg_price"])
            t     = o["filled_at"]
            oid   = o["id"][:8]
            side  = o["side"].lower()

            if side == "buy":
                if short_q:
                    remaining = qty
                    while remaining > 1e-8 and short_q:
                        s_qty, s_price, s_time = short_q[0]
                        matched = min(remaining, s_qty)
                        pnl = (s_price - price) * matched
                        trades.append({
                            "exit_time":   t,
                            "symbol":      symbol,
                            "lot_id":      f"ALP-{oid}",
                            "entry_time":  s_time,
                            "entry_price": round(s_price, 6),
                            "exit_price":  round(price, 6),
                            "qty":         round(matched, 8),
                            "pnl_usd":     round(pnl, 4),
                            "pnl_pct":     round((s_price - price) / s_price * 100, 4),
                            "reason":      "alpaca-history",
                        })
                        short_q[0] = (s_qty - matched, s_price, s_time)
                        remaining -= matched
                        if short_q[0][0] <= 1e-8:
                            short_q.pop(0)
                    if remaining > 1e-8:
                        long_q.append((remaining, price, t))
                else:
                    long_q.append((qty, price, t))

            elif side == "sell":
                if long_q:
                    remaining = qty
                    while remaining > 1e-8 and long_q:
                        l_qty, l_price, l_time = long_q[0]
                        matched = min(remaining, l_qty)
                        pnl = (price - l_price) * matched
                        trades.append({
                            "exit_time":   t,
                            "symbol":      symbol,
                            "lot_id":      f"ALP-{oid}",
                            "entry_time":  l_time,
                            "entry_price": round(l_price, 6),
                            "exit_price":  round(price, 6),
                            "qty":         round(matched, 8),
                            "pnl_usd":     round(pnl, 4),
                            "pnl_pct":     round((price - l_price) / l_price * 100, 4),
                            "reason":      "alpaca-history",
                        })
                        long_q[0] = (l_qty - matched, l_price, l_time)
                        remaining -= matched
                        if long_q[0][0] <= 1e-8:
                            long_q.pop(0)
                    if remaining > 1e-8:
                        short_q.append((remaining, price, t))
                else:
                    short_q.append((qty, price, t))

    trades.sort(key=lambda x: x["exit_time"])
    return trades


SEED_FILE = Path(__file__).parent / "trades_seed.csv"


class TradeLogger:
    def __init__(self, filepath: str = "trades.csv"):
        self.filepath = Path(filepath)
        self._lock = Lock()
        if not self.filepath.exists():
            if SEED_FILE.exists():
                import shutil
                shutil.copy(SEED_FILE, self.filepath)
                logger.info(f"Seeded {self.filepath} from {SEED_FILE}")
            else:
                with open(self.filepath, "w", newline="") as f:
                    csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def sync_from_alpaca(self, broker) -> int:
        """Pull Alpaca order history, reconstruct trades, merge into trades.csv.

        Only reconstructs trades for symbols in our trading universe so other
        bots sharing the same paper account don't pollute our history.
        ALP- rows are replaced on each call (rebuilt fresh from Alpaca).
        Bot-generated LOT- rows are preserved unchanged.
        Falls back to trades_seed.csv if Alpaca returns no usable trades.
        Returns number of Alpaca-history trades written.
        """
        orders = broker.get_closed_orders(days=90)
        universe = set(broker.config.ALL_SYMBOLS)
        our_orders = [o for o in orders if o["symbol"] in universe]
        logger.info(f"sync_from_alpaca: {len(orders)} total orders, {len(our_orders)} in our universe")

        alpaca_trades = _reconstruct_trades_from_orders(our_orders) if our_orders else []

        existing = self.get_all_trades()
        bot_rows = [t for t in existing if not t.get("lot_id", "").startswith("ALP-")]

        if not alpaca_trades and not bot_rows and SEED_FILE.exists():
            import shutil
            shutil.copy(SEED_FILE, self.filepath)
            logger.info(f"sync_from_alpaca: no live trades found, seeded from {SEED_FILE}")
            return 0

        merged = bot_rows + alpaca_trades
        merged.sort(key=lambda x: x.get("exit_time", ""))

        with self._lock:
            with open(self.filepath, "w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=FIELDS)
                w.writeheader()
                w.writerows(merged)

        logger.info(
            f"sync_from_alpaca: {len(alpaca_trades)} Alpaca + "
            f"{len(bot_rows)} bot = {len(merged)} total trades"
        )
        return len(alpaca_trades)

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
        wins   = [p for p in pnls if p > 0]
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
            "wins":         len(wins),
            "losses":       len(losses),
            "win_rate":     round(len(wins) / len(trades) * 100, 1),
            "total_pnl":    round(sum(pnls), 2),
            "avg_pnl":      round(sum(pnls) / len(pnls), 2),
            "best_trade":   round(max(pnls), 2),
            "worst_trade":  round(min(pnls), 2),
            "per_symbol":   per_symbol,
        }
