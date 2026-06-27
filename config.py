import os
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / '.env')


class Config:
    # Alpaca
    ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
    ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
    PAPER_TRADING: bool = os.getenv("PAPER_TRADING", "true").lower() == "true"

    # Portfolio sizing
    PORTFOLIO_VALUE_USD: float = float(os.getenv("PORTFOLIO_VALUE_USD", "10000"))
    LOT_SIZE_PCT: float = float(os.getenv("LOT_SIZE_PCT", "0.10"))
    MAX_LOTS_PER_SYMBOL: int = int(os.getenv("MAX_LOTS_PER_SYMBOL", "3"))

    # Risk management
    STOP_LOSS_PCT: float = float(os.getenv("STOP_LOSS_PCT", "1.5"))
    TAKE_PROFIT_PCT: float = float(os.getenv("TAKE_PROFIT_PCT", "2.5"))
    MAX_DAILY_LOSSES: int = int(os.getenv("MAX_DAILY_LOSSES", "3"))
    DAILY_DRAWDOWN_USD: float = float(os.getenv("DAILY_DRAWDOWN_USD", "300"))

    # IADSS confluence window – signals from any model remain valid for this long.
    # 1200s (20 min) = 4 bars on a 5-min chart; fresh enough to be meaningful.
    SIGNAL_WINDOW_SEC: int = int(os.getenv("SIGNAL_WINDOW_SEC", "1200"))
    # Cooldown after entry before re-entering same symbol (seconds)
    ENTRY_COOLDOWN_SEC: int = int(os.getenv("ENTRY_COOLDOWN_SEC", "60"))
    # Allow short selling stocks (not crypto – Alpaca crypto is long-only)
    ALLOW_SHORTS: bool = os.getenv("ALLOW_SHORTS", "true").lower() == "true"

    # Webhook server
    WEBHOOK_PORT: int = int(os.getenv("PORT") or os.getenv("WEBHOOK_PORT", "3000"))
    WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "iadss-ultitrader-2024")

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Monitor loop frequency
    MONITOR_INTERVAL_SEC: int = int(os.getenv("MONITOR_INTERVAL_SEC", "30"))
    # Seconds after 9:30 AM ET open to block stock entries (avoids open volatility)
    OPEN_BUFFER_SEC: int = int(os.getenv("OPEN_BUFFER_SEC", "1800"))
    # Minimum unrealized profit (USD) required before Trend=SELL alone can exit a long
    TREND_EXIT_MIN_PROFIT_USD: float = float(os.getenv("TREND_EXIT_MIN_PROFIT_USD", "3.0"))

    # Target assets
    STOCK_SYMBOLS: list = [
        # Original core
        "NVDA", "TSLA", "AAPL", "MSFT", "AMZN",
        "META", "GOOGL", "AVGO", "AMD",
        "WMT", "GME", "SPY", "JPM",
        # Added: high-beta / high-signal-frequency scalping candidates
        "QQQ",   # NASDAQ-100 ETF — most liquid, fires signals constantly
        "COIN",  # Coinbase — crypto-correlated, high volatility
        "PLTR",  # Palantir — popular momentum stock
        "MARA",  # Bitcoin miner — extreme volatility, crypto-correlated
        "RIOT",  # Bitcoin miner — extreme volatility, crypto-correlated
    ]
    CRYPTO_SYMBOLS: list = [
        # Original core
        "BTC/USD", "SOL/USD", "ETH/USD",
        "DOGE/USD", "XRP/USD", "AVAX/USD", "LINK/USD",
        # Added for 24/7 night coverage
        "ADA/USD",   # Cardano — high volume
        "LTC/USD",   # Litecoin — established, active
        "MATIC/USD", # Polygon — high frequency signals
        "DOT/USD",   # Polkadot — active
        "BCH/USD",   # Bitcoin Cash — decent volume
    ]

    # TradingView ticker → Alpaca symbol normalisation
    _TV_MAP: dict = {
        # Crypto — existing
        "BTCUSD": "BTC/USD", "BTCUSDT": "BTC/USD",
        "COINBASE:BTCUSD": "BTC/USD", "BINANCE:BTCUSDT": "BTC/USD",
        "ETHUSD": "ETH/USD", "ETHUSDT": "ETH/USD",
        "COINBASE:ETHUSD": "ETH/USD", "BINANCE:ETHUSDT": "ETH/USD",
        "SOLUSD": "SOL/USD", "SOLUSDT": "SOL/USD",
        "COINBASE:SOLUSD": "SOL/USD", "BINANCE:SOLUSDT": "SOL/USD",
        "DOGEUSD": "DOGE/USD", "DOGEUSDT": "DOGE/USD",
        "COINBASE:DOGEUSD": "DOGE/USD", "BINANCE:DOGEUSDT": "DOGE/USD",
        "XRPUSD": "XRP/USD", "XRPUSDT": "XRP/USD",
        "COINBASE:XRPUSD": "XRP/USD", "BINANCE:XRPUSDT": "XRP/USD",
        "AVAXUSD": "AVAX/USD", "AVAXUSDT": "AVAX/USD",
        "COINBASE:AVAXUSD": "AVAX/USD", "BINANCE:AVAXUSDT": "AVAX/USD",
        "LINKUSD": "LINK/USD", "LINKUSDT": "LINK/USD",
        "COINBASE:LINKUSD": "LINK/USD", "BINANCE:LINKUSDT": "LINK/USD",
        # Crypto — new additions
        "ADAUSD": "ADA/USD", "ADAUSDT": "ADA/USD",
        "COINBASE:ADAUSD": "ADA/USD", "BINANCE:ADAUSDT": "ADA/USD",
        "LTCUSD": "LTC/USD", "LTCUSDT": "LTC/USD",
        "COINBASE:LTCUSD": "LTC/USD", "BINANCE:LTCUSDT": "LTC/USD",
        "MATICUSD": "MATIC/USD", "MATICUSDT": "MATIC/USD",
        "COINBASE:MATICUSD": "MATIC/USD", "BINANCE:MATICUSDT": "MATIC/USD",
        "DOTUSD": "DOT/USD", "DOTUSDT": "DOT/USD",
        "COINBASE:DOTUSD": "DOT/USD", "BINANCE:DOTUSDT": "DOT/USD",
        "BCHUSD": "BCH/USD", "BCHUSDT": "BCH/USD",
        "COINBASE:BCHUSD": "BCH/USD", "BINANCE:BCHUSDT": "BCH/USD",
        # Stocks — existing
        "NASDAQ:NVDA": "NVDA", "NASDAQ:AAPL": "AAPL",
        "NASDAQ:MSFT": "MSFT", "NASDAQ:AMZN": "AMZN",
        "NASDAQ:META": "META", "NASDAQ:GOOGL": "GOOGL",
        "NASDAQ:AVGO": "AVGO", "NASDAQ:AMD": "AMD",
        "NASDAQ:TSLA": "TSLA", "NYSE:TSLA": "TSLA",
        "NYSE:WMT": "WMT", "NYSE:GME": "GME",
        "NYSE:JPM": "JPM",
        "AMEX:SPY": "SPY", "NYSE:SPY": "SPY",
        # Stocks — new additions
        "NASDAQ:QQQ": "QQQ", "AMEX:QQQ": "QQQ",
        "NASDAQ:COIN": "COIN",
        "NASDAQ:PLTR": "PLTR",
        "NASDAQ:MARA": "MARA",
        "NASDAQ:RIOT": "RIOT",
    }

    @property
    def LOT_SIZE_USD(self) -> float:
        return self.PORTFOLIO_VALUE_USD * self.LOT_SIZE_PCT

    @property
    def ALL_SYMBOLS(self) -> list:
        return self.STOCK_SYMBOLS + self.CRYPTO_SYMBOLS

    def normalize_symbol(self, symbol: str) -> str:
        s = symbol.strip().upper()
        return self._TV_MAP.get(s, s)

    def is_crypto(self, symbol: str) -> bool:
        return "/" in symbol

    def is_valid_symbol(self, symbol: str) -> bool:
        return symbol in self.ALL_SYMBOLS
