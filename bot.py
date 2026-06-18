"""
IADSS UltiTrader Scalper
========================
Entry point. Starts:
  1. Risk monitor (background daemon thread)
  2. Flask webhook server (main thread, blocks)

Run locally:   python bot.py
Deploy:        Railway / Render / any WSGI host pointing at bot.py
"""
import threading
import logging

from config import Config
from broker import AlpacaBroker
from alerts import TelegramAlerter
from position_manager import PositionManager
from signal_engine import SignalEngine
from risk_manager import RiskManager
from webhook_server import create_app

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


def main():
    cfg = Config()

    logger.info("=" * 60)
    logger.info("IADSS UltiTrader Scalper")
    logger.info(f"  Paper trading : {cfg.PAPER_TRADING}")
    logger.info(f"  Portfolio     : ${cfg.PORTFOLIO_VALUE_USD:,.0f}")
    logger.info(f"  Lot size      : ${cfg.LOT_SIZE_USD:.0f} ({cfg.LOT_SIZE_PCT*100:.0f}%)")
    logger.info(f"  Max lots/sym  : {cfg.MAX_LOTS_PER_SYMBOL}")
    logger.info(f"  Stop-loss     : -{cfg.STOP_LOSS_PCT}%")
    logger.info(f"  Take-profit   : +{cfg.TAKE_PROFIT_PCT}%")
    logger.info(f"  Signal window : {cfg.SIGNAL_WINDOW_SEC}s")
    logger.info(f"  Stocks        : {', '.join(cfg.STOCK_SYMBOLS)}")
    logger.info(f"  Crypto        : {', '.join(cfg.CRYPTO_SYMBOLS)}")
    logger.info("=" * 60)

    broker = AlpacaBroker(cfg)
    alerter = TelegramAlerter(cfg)

    # Auto-discover Telegram chat_id if not yet set
    if cfg.TELEGRAM_BOT_TOKEN and not cfg.TELEGRAM_CHAT_ID:
        chat_id = alerter.discover_chat_id()
        if chat_id:
            logger.info(f"Add to .env → TELEGRAM_CHAT_ID={chat_id}")

    position_mgr = PositionManager(cfg)
    signal_engine = SignalEngine(cfg, position_mgr, broker, alerter)
    risk_mgr = RiskManager(cfg, broker, position_mgr, signal_engine, alerter)

    # Start risk monitor in daemon thread
    threading.Thread(target=risk_mgr.run, daemon=True, name="RiskMonitor").start()

    # Startup notification
    try:
        acct = broker.get_account()
        alerter.send(
            f"🚀 <b>IADSS UltiTrader Scalper LIVE</b>\n"
            f"Equity: ${acct.get('equity', 0):,.2f}\n"
            f"Cash:   ${acct.get('cash', 0):,.2f}\n"
            f"Port:   ${cfg.PORTFOLIO_VALUE_USD:,.0f} | Lot ${cfg.LOT_SIZE_USD:.0f}\n"
            f"SL {cfg.STOP_LOSS_PCT}% | TP {cfg.TAKE_PROFIT_PCT}%\n"
            f"Symbols: {len(cfg.ALL_SYMBOLS)} | Port {cfg.WEBHOOK_PORT}"
        )
    except Exception as e:
        logger.error(f"Startup Alpaca check failed: {e}")

    # Start webhook server (blocking)
    app = create_app(cfg, signal_engine, alerter)
    logger.info(f"Webhook server listening on 0.0.0.0:{cfg.WEBHOOK_PORT}")
    logger.info(f"Webhook URL: http://0.0.0.0:{cfg.WEBHOOK_PORT}/webhook")
    logger.info(f"Status page: http://0.0.0.0:{cfg.WEBHOOK_PORT}/status")
    app.run(host="0.0.0.0", port=cfg.WEBHOOK_PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
