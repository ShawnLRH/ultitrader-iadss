"""Flask webhook server – receives TradingView alerts and routes them to SignalEngine."""
import logging
from flask import Flask, request, jsonify

logger = logging.getLogger(__name__)


def create_app(config, signal_engine, alerter):
    app = Flask(__name__)

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "bot": "IADSS UltiTrader Scalper"})

    @app.route("/webhook", methods=["POST"])
    def webhook():
        # Authenticate via X-Secret header OR ?secret= query param
        secret = request.headers.get("X-Secret", "") or request.args.get("secret", "")
        if config.WEBHOOK_SECRET and secret != config.WEBHOOK_SECRET:
            logger.warning(f"Unauthorized webhook from {request.remote_addr}")
            return jsonify({"error": "unauthorized"}), 401

        try:
            data = request.get_json(force=True, silent=True) or {}
            if not data:
                return jsonify({"error": "empty or invalid JSON"}), 400

            # Accept both short keys (compact alert msg) and long keys
            symbol = str(data.get("sym") or data.get("symbol") or "").strip()
            model = str(data.get("mdl") or data.get("model") or "").strip().lower()
            signal = str(data.get("sig") or data.get("signal") or "").strip().lower()
            price = float(data.get("px") or data.get("price") or 0)
            strength = str(data.get("str") or data.get("strength") or "confirmed").strip().lower()

            if not symbol or not model or not signal:
                return jsonify({"error": "required fields: sym, mdl, sig"}), 400

            signal_engine.process_signal(symbol, model, signal, price, strength)
            return jsonify({"status": "ok", "sym": symbol, "mdl": model, "sig": signal})

        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/status", methods=["GET"])
    def status():
        """Quick dashboard – open positions and daily stats."""
        open_syms = signal_engine.position_mgr.get_all_open_symbols()
        positions = {}
        for sym in open_syms:
            lots = signal_engine.position_mgr.get_lots(sym)
            price = signal_engine.broker.get_price(sym)
            positions[sym] = {
                "current_price": price,
                "avg_entry": signal_engine.position_mgr.avg_entry_price(sym),
                "lots": [
                    {
                        "id": l.lot_id,
                        "qty": round(l.qty, 6),
                        "entry": round(l.entry_price, 4),
                        "sl": round(l.stop_price, 4),
                        "tp": round(l.take_profit_price, 4),
                    }
                    for l in lots
                ],
            }
        return jsonify(
            {
                "positions": positions,
                "daily_pnl": round(signal_engine.position_mgr.daily_pnl, 2),
                "daily_losses": signal_engine.position_mgr.daily_losses,
                "limit_reached": signal_engine.position_mgr.daily_loss_limit_reached(),
            }
        )

    return app
