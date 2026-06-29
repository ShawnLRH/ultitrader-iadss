"""Alpaca broker wrapper – handles both stocks and crypto."""
import time
import logging
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, StockLatestTradeRequest, CryptoLatestQuoteRequest

logger = logging.getLogger(__name__)


class AlpacaBroker:
    def __init__(self, config):
        self.config = config
        self.trading = TradingClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
            paper=config.PAPER_TRADING,
        )
        self.stock_data = StockHistoricalDataClient(
            api_key=config.ALPACA_API_KEY,
            secret_key=config.ALPACA_SECRET_KEY,
        )
        self.crypto_data = CryptoHistoricalDataClient()

    # ── Price queries ──────────────────────────────────────────────────────────

    def get_price(self, symbol: str) -> Optional[float]:
        """Return current price. Stocks use last trade; crypto uses quote mid."""
        try:
            if self.config.is_crypto(symbol):
                req = CryptoLatestQuoteRequest(symbol_or_symbols=symbol)
                q = self.crypto_data.get_crypto_latest_quote(req)[symbol]
                ask, bid = float(q.ask_price), float(q.bid_price)
                if ask > 0 and bid > 0:
                    return round((ask + bid) / 2, 6)
                return round(ask or bid, 6) or None
            else:
                # Use last trade price — avoids stale one-sided NBBO at open/close
                # where ask=0 causes (ask+bid)/2 to return half the real price.
                req = StockLatestTradeRequest(symbol_or_symbols=symbol)
                t = self.stock_data.get_stock_latest_trade(req)[symbol]
                return round(float(t.price), 4)
        except Exception as e:
            logger.warning(f"get_price {symbol}: {e}")
            return None

    # ── Order execution ────────────────────────────────────────────────────────

    def buy(self, symbol: str, notional_usd: float) -> Optional[dict]:
        """Market buy by notional USD. Waits for fill and returns fill data."""
        try:
            tif = TimeInForce.GTC if self.config.is_crypto(symbol) else TimeInForce.DAY
            order = self.trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    notional=round(notional_usd, 2),
                    side=OrderSide.BUY,
                    time_in_force=tif,
                )
            )
            # Poll until filled (up to 5s); single 0.8s sleep risks returning before fill
            fill_price, fill_qty = 0.0, 0.0
            for _ in range(10):
                time.sleep(0.5)
                filled = self.trading.get_order_by_id(str(order.id))
                fill_price = float(filled.filled_avg_price or 0)
                fill_qty   = float(filled.filled_qty or 0)
                if fill_price > 0:
                    break
            logger.info(f"BUY {symbol} ${notional_usd:.2f} → qty={fill_qty:.6f} @ ${fill_price:.4f}")
            return {
                "order_id": str(order.id),
                "symbol": symbol,
                "fill_price": fill_price,
                "fill_qty": fill_qty,
                "notional": notional_usd,
            }
        except Exception as e:
            logger.error(f"buy {symbol} ${notional_usd:.2f}: {e}")
            return None

    def short(self, symbol: str, notional_usd: float) -> Optional[dict]:
        """Open a short (sell short) by notional USD. Stocks only."""
        try:
            price = self.get_price(symbol)
            if not price:
                return None
            qty = round(notional_usd / price, 6)
            order = self.trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            fill_price, fill_qty = 0.0, 0.0
            for _ in range(10):
                time.sleep(0.5)
                filled = self.trading.get_order_by_id(str(order.id))
                fill_price = float(filled.filled_avg_price or 0)
                fill_qty   = float(filled.filled_qty or 0)
                if fill_price > 0:
                    break
            logger.info(f"SHORT {symbol} ${notional_usd:.2f} → qty={fill_qty:.6f} @ ${fill_price:.4f}")
            return {"order_id": str(order.id), "symbol": symbol, "fill_price": fill_price, "fill_qty": fill_qty}
        except Exception as e:
            logger.error(f"short {symbol}: {e}")
            return None

    def buy_qty(self, symbol: str, qty: float) -> Optional[dict]:
        """Market buy by quantity (used to cover a short lot in LIFO partial exit)."""
        try:
            tif = TimeInForce.GTC if self.config.is_crypto(symbol) else TimeInForce.DAY
            order = self.trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=round(qty, 6),
                    side=OrderSide.BUY,
                    time_in_force=tif,
                )
            )
            fill_price = 0.0
            for _ in range(10):
                time.sleep(0.5)
                filled = self.trading.get_order_by_id(str(order.id))
                fill_price = float(filled.filled_avg_price or 0)
                if fill_price > 0:
                    break
            logger.info(f"BUY_QTY {symbol} qty={qty:.6f} @ ${fill_price:.4f}")
            return {"order_id": str(order.id), "fill_price": fill_price}
        except Exception as e:
            logger.error(f"buy_qty {symbol}: {e}")
            return None

    def sell_qty(self, symbol: str, qty: float) -> Optional[dict]:
        """Market sell by quantity (for LIFO partial exits)."""
        try:
            tif = TimeInForce.GTC if self.config.is_crypto(symbol) else TimeInForce.DAY
            order = self.trading.submit_order(
                MarketOrderRequest(
                    symbol=symbol,
                    qty=round(qty, 6),
                    side=OrderSide.SELL,
                    time_in_force=tif,
                )
            )
            fill_price = 0.0
            for _ in range(10):
                time.sleep(0.5)
                filled = self.trading.get_order_by_id(str(order.id))
                fill_price = float(filled.filled_avg_price or 0)
                if fill_price > 0:
                    break
            logger.info(f"SELL {symbol} qty={qty:.6f} @ ${fill_price:.4f}")
            return {"order_id": str(order.id), "fill_price": fill_price}
        except Exception as e:
            logger.error(f"sell_qty {symbol} {qty}: {e}")
            return None

    def close_position(self, symbol: str) -> bool:
        """Close the entire Alpaca position for a symbol."""
        try:
            self.trading.close_position(symbol)
            logger.info(f"CLOSE all {symbol}")
            return True
        except Exception as e:
            logger.error(f"close_position {symbol}: {e}")
            return False

    # ── Account / market info ──────────────────────────────────────────────────

    def get_account(self) -> dict:
        try:
            a = self.trading.get_account()
            return {
                "equity": float(a.equity),
                "cash": float(a.cash),
                "buying_power": float(a.buying_power),
            }
        except Exception as e:
            logger.error(f"get_account: {e}")
            return {}

    def get_closed_orders(self, days: int = 90) -> list:
        """Return all filled orders from the last N days, sorted oldest-first."""
        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus
        from datetime import datetime, timezone, timedelta
        try:
            since = datetime.now(timezone.utc) - timedelta(days=days)
            req = GetOrdersRequest(
                status=QueryOrderStatus.CLOSED,
                after=since,
                direction="asc",
                limit=500,
            )
            orders = self.trading.get_orders(filter=req)
            result = []
            for o in orders:
                fqty  = float(o.filled_qty or 0)
                fprice = float(o.filled_avg_price or 0)
                if fqty <= 0 or fprice <= 0 or not o.filled_at:
                    continue
                result.append({
                    "id":                str(o.id),
                    "symbol":            str(o.symbol),
                    "side":              o.side.value if hasattr(o.side, "value") else str(o.side),
                    "filled_qty":        fqty,
                    "filled_avg_price":  fprice,
                    "filled_at":         o.filled_at.isoformat(),
                })
            return result
        except Exception as e:
            logger.error(f"get_closed_orders: {e}")
            return []

    def get_positions(self) -> list:
        """Return all open Alpaca positions as plain dicts."""
        try:
            positions = self.trading.get_all_positions()
            return [
                {
                    "symbol":          str(p.symbol),
                    "qty":             float(p.qty),
                    "side":            p.side.value if hasattr(p.side, "value") else str(p.side),
                    "avg_entry_price": float(p.avg_entry_price),
                    "unrealized_pl":   float(p.unrealized_pl),
                }
                for p in positions
            ]
        except Exception as e:
            logger.error(f"get_positions: {e}")
            return []

    def is_market_open(self) -> bool:
        try:
            return self.trading.get_clock().is_open
        except Exception as e:
            logger.warning(f"is_market_open: {e}")
            return False
