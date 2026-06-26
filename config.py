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

    # Target assets
    STOCK_SYMBOLS: list = [
        "NVDA", "TSLA", "AAPL", "MSFT", "AMZN",
        "META", "GOOGL", "AVGO", "AMD",
    ]
    CRYPTO_SYMBOLS: list = ["BTC/USD", "SOL/USD", "ETH/USD"]

    # TradingView ticker → Alpaca symbol normalisation
    _TV_MAP: dict = {
        "BTCUSD": "BTC/USD", "BTCUSDT": "BTC/USD",
        "COINBASE:BTCUSD": "BTC/USD", "BINANCE:BTCUSDT": "BTC/USD",
        "ETHUSD": "ETH/USD", "ETHUSDT": "ETH/USD",
        "COINBASE:ETHUSD": "ETH/USD", "BINANCE:ETHUSDT": "ETH/USD",
        "SOLUSD": "SOL/USD", "SOLUSDT": "SOL/USD",
        "COINBASE:SOLUSD": "SOL/USD", "BINANCE:SOLUSDT": "SOL/USD",
        # Stock exchanges sometimes prefix with exchange code
        "NASDAQ:NVDA": "NVDA", "NASDAQ:AAPL": "AAPL",
        "NASDAQ:MSFT": "MSFT", "NASDAQ:AMZN": "AMZN",
        "NASDAQ:META": "META", "NASDAQ:GOOGL": "GOOGL",
        "NASDAQ:AVGO": "AVGO", "NASDAQ:AMD": "AMD",
        "NASDAQ:TSLA": "TSLA", "NYSE:TSLA": "TSLA",
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
