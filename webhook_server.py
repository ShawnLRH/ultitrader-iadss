"""Flask webhook server — receives TradingView alerts, serves dashboard and APIs."""
import logging
from flask import Flask, request, jsonify, render_template

logger = logging.getLogger(__name__)


def create_app(config, signal_engine, alerter, trade_logger=None):
    app = Flask(__name__)

    # ── Health ──────────────────────────────────────────────────────────────────

    @app.route("/health", methods=["GET"])
    def health():
        return jsonify({"status": "ok", "bot": "IADSS UltiTrader Scalper"})

    # ── TradingView webhook ─────────────────────────────────────────────────────

    @app.route("/webhook", methods=["POST"])
    def webhook():
        secret = request.headers.get("X-Secret", "") or request.args.get("secret", "")
        if config.WEBHOOK_SECRET and secret != config.WEBHOOK_SECRET:
            logger.warning(f"Unauthorized webhook from {request.remote_addr}")
            return jsonify({"error": "unauthorized"}), 401

        try:
            data = request.get_json(force=True, silent=True) or {}
            if not data:
                return jsonify({"error": "empty or invalid JSON"}), 400

            symbol   = str(data.get("sym")      or data.get("symbol")   or "").strip()
            model    = str(data.get("mdl")      or data.get("model")    or "").strip().lower()
            signal   = str(data.get("sig")      or data.get("signal")   or "").strip().lower()
            price    = float(data.get("px")     or data.get("price")    or 0)
            strength = str(data.get("str")      or data.get("strength") or "confirmed").strip().lower()

            if not symbol or not model or not signal:
                return jsonify({"error": "required fields: sym, mdl, sig"}), 400

            signal_engine.process_signal(symbol, model, signal, price, strength)
            return jsonify({"status": "ok", "sym": symbol, "mdl": model, "sig": signal})

        except Exception as e:
            logger.error(f"Webhook error: {e}")
            return jsonify({"error": str(e)}), 500

    # ── Dashboard HTML ──────────────────────────────────────────────────────────

    @app.route("/", methods=["GET"])
    @app.route("/dashboard", methods=["GET"])
    def dashboard():
        return render_template("dashboard.html")

    # ── JSON APIs ───────────────────────────────────────────────────────────────

    @app.route("/status", methods=["GET"])
    def status():
        """Open positions + daily stats (used by dashboard)."""
        open_syms = signal_engine.position_mgr.get_all_open_symbols()
        positions = {}
        for sym in open_syms:
            lots  = signal_engine.position_mgr.get_lots(sym)
            price = signal_engine.broker.get_price(sym)
            positions[sym] = {
                "current_price": price,
                "avg_entry":     signal_engine.position_mgr.avg_entry_price(sym),
                "lots": [
                    {
                        "id":  l.lot_id,
                        "qty": round(l.qty, 6),
                        "entry": round(l.entry_price, 4),
                        "sl":    round(l.stop_price, 4),
                        "tp":    round(l.take_profit_price, 4),
                    }
                    for l in lots
                ],
            }
        return jsonify({
            "positions":     positions,
            "daily_pnl":     round(signal_engine.position_mgr.daily_pnl, 2),
            "daily_losses":  signal_engine.position_mgr.daily_losses,
            "limit_reached": signal_engine.position_mgr.daily_loss_limit_reached(),
        })

    @app.route("/api/stats", methods=["GET"])
    def api_stats():
        """Aggregated trade statistics from trades.csv."""
        if trade_logger is None:
            return jsonify({"total_trades": 0, "wins": 0, "losses": 0,
                            "win_rate": 0, "total_pnl": 0, "avg_pnl": 0,
                            "best_trade": 0, "worst_trade": 0, "per_symbol": {}})
        return jsonify(trade_logger.get_stats())

    @app.route("/api/trades", methods=["GET"])
    def api_trades():
        """Full trade history from trades.csv."""
        if trade_logger is None:
            return jsonify({"trades": []})
        return jsonify({"trades": trade_logger.get_all_trades()})

    @app.route("/api/log", methods=["GET"])
    def api_log():
        """Recent incoming TradingView webhook hits (newest first, max 200)."""
        return jsonify({"log": signal_engine.get_webhook_log()})

    @app.route("/api/signals", methods=["GET"])
    def api_signals():
        """Current per-symbol signal state (freshness, confluence, macro bias)."""
        return jsonify(signal_engine.get_signal_state())

    return app
