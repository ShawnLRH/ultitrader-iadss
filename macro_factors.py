"""Read-side accessor for the daily news factors — deliberately dumb and fail-open.

signal_engine.py calls get_latest() on every entry check. If the file doesn't
exist yet (first run before 8:30am, or a fresh deploy), is stale JSON, or any
other error occurs, this returns None and callers must treat that as "no
opinion" — never block a trade because this file is missing or broken.
"""
import os
import json
import logging

logger = logging.getLogger(__name__)


def get_latest(newsletter_dir: str = "newsletters") -> dict | None:
    path = os.path.join(newsletter_dir, "latest_factors.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning(f"macro_factors: failed to read {path}: {e}")
        return None
