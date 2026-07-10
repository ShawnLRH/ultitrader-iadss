"""
Daily news job scheduler — background daemon thread, same shape as risk_manager.py.

Runs run_daily_news_job once a day at NEWS_RUN_HOUR_ET:NEWS_RUN_MINUTE_ET
(default 8:30am America/New_York, 1h before the 9:30am open). Runs every
calendar day including weekends, since crypto trades 24/7 and macro news
still matters on weekends for those factors.
"""
import os
import time
import glob
import logging
import datetime

from news_analyst import run_daily_news_job

logger = logging.getLogger(__name__)

POLL_SEC = 60


class NewsScheduler:
    def __init__(self, config, alerter):
        self.config = config
        self.alerter = alerter
        self._last_run_date: str | None = None

    def _today_newsletter_exists(self) -> bool:
        pattern = os.path.join(self.config.NEWSLETTER_DIR, "newsletter_*.pdf")
        today = datetime.date.today().isoformat()
        return any(today in os.path.basename(p) for p in glob.glob(pattern))

    def _now_et(self) -> datetime.datetime:
        try:
            from zoneinfo import ZoneInfo
            return datetime.datetime.now(tz=ZoneInfo("America/New_York"))
        except Exception:
            return datetime.datetime.utcnow()

    def _run(self, reason: str):
        # Track "today" in the same ET basis the scheduling loop uses below — mixing this
        # with a UTC-based date previously caused the loop's guard to never match during
        # the ~4-5h/day window where UTC and ET fall on different calendar dates, triggering
        # a repeated-run bug.
        today = self._now_et().date().isoformat()
        logger.info(f"NewsScheduler: running daily news job ({reason})")
        try:
            run_daily_news_job(self.config, self.alerter)
        except Exception as e:
            logger.error(f"NewsScheduler: run_daily_news_job failed: {e}")
        self._last_run_date = today

    def run(self):
        """Blocking loop — run in daemon thread."""
        logger.info(
            f"NewsScheduler started — daily run at "
            f"{self.config.NEWS_RUN_HOUR_ET:02d}:{self.config.NEWS_RUN_MINUTE_ET:02d} ET"
        )

        if not self._today_newsletter_exists():
            self._run("startup catch-up — no newsletter for today yet")

        while True:
            try:
                now_et = self._now_et()
                today = now_et.date().isoformat()
                target_passed = (
                    now_et.hour > self.config.NEWS_RUN_HOUR_ET
                    or (now_et.hour == self.config.NEWS_RUN_HOUR_ET and now_et.minute >= self.config.NEWS_RUN_MINUTE_ET)
                )
                if target_passed and self._last_run_date != today:
                    self._run(f"scheduled {self.config.NEWS_RUN_HOUR_ET:02d}:{self.config.NEWS_RUN_MINUTE_ET:02d} ET")
            except Exception as e:
                logger.error(f"NewsScheduler loop error: {e}")
            time.sleep(POLL_SEC)
