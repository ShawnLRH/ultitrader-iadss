"""
Free RSS news aggregation — no API key required.

Pulls macro/market-moving headlines from a fixed set of RSS feeds (Google News
topical feeds + major wire services), dedupes by normalized title, and filters
to roughly the last day so the daily digest reflects overnight developments.
"""
import logging
import re
import time
import calendar
from datetime import datetime, timezone

import feedparser
import requests

logger = logging.getLogger(__name__)

# (source label, feed URL) — mix of general wire feeds + topical Google News queries
# covering the categories the newsletter needs: war/conflict, economy, elections, disasters.
FEEDS = [
    ("Google News – Business",   "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en"),
    ("Google News – World",      "https://news.google.com/rss/headlines/section/topic/WORLD?hl=en-US&gl=US&ceid=US:en"),
    ("Google News – Markets",    "https://news.google.com/rss/search?q=stock%20market%20OR%20federal%20reserve%20OR%20inflation&hl=en-US&gl=US&ceid=US:en"),
    ("Google News – Conflict",   "https://news.google.com/rss/search?q=war%20OR%20conflict%20OR%20military%20strike%20OR%20sanctions&hl=en-US&gl=US&ceid=US:en"),
    ("Google News – Elections",  "https://news.google.com/rss/search?q=election%20OR%20president%20OR%20parliament%20vote&hl=en-US&gl=US&ceid=US:en"),
    ("Google News – Disasters",  "https://news.google.com/rss/search?q=earthquake%20OR%20hurricane%20OR%20plane%20crash%20OR%20explosion%20OR%20disaster&hl=en-US&gl=US&ceid=US:en"),
    ("Reuters Business",         "https://feeds.reuters.com/reuters/businessNews"),
    ("AP Top News",              "https://apnews.com/apf-topnews?format=rss"),
    ("CNBC Top News",            "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch Top Stories",  "https://feeds.marketwatch.com/marketwatch/topstories/"),
    ("Yahoo Finance",            "https://finance.yahoo.com/news/rssindex"),
]

MAX_AGE_SEC = 30 * 3600   # ~30 hours — overnight + prior-day catch-up
# Groq's on-demand tier caps this model at 12,000 tokens/minute (input+output combined) —
# keep headline count low enough that a single call's input+max_tokens stays well under that.
MAX_ITEMS = 45
FEED_TIMEOUT_SEC = 12


def _entry_ts(entry) -> float:
    """Best-effort published timestamp; falls back to now (never drop for missing dates)."""
    for key in ("published_parsed", "updated_parsed"):
        val = getattr(entry, key, None)
        if val:
            try:
                return calendar.timegm(val)
            except Exception:
                pass
    return time.time()


def _normalize_title(title: str) -> str:
    return " ".join(title.lower().split())[:140]


def _entry_image(entry) -> str:
    """Image URL embedded in the RSS entry itself (media:content / media:thumbnail /
    enclosure / <img> in the summary HTML). Empty string if the feed carries none."""
    for m in (getattr(entry, "media_content", None) or []):
        url = m.get("url", "")
        if url and not str(m.get("type", "")).startswith(("video", "audio")):
            return url
    for m in (getattr(entry, "media_thumbnail", None) or []):
        if m.get("url"):
            return m["url"]
    for enc in (getattr(entry, "enclosures", None) or []):
        if str(enc.get("type", "")).startswith("image") and enc.get("href"):
            return enc["href"]
    summary = getattr(entry, "summary", "") or ""
    m = re.search(r'<img[^>]+src=["\']([^"\']+)', summary)
    return m.group(1) if m else ""


_OG_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def resolve_article_image(link: str, timeout: int = 8) -> str:
    """Fetch the article page and pull its og:image / twitter:image.

    Skips news.google.com redirect stubs — their og:image is always the generic
    Google News logo, never the article photo.
    """
    if not link or "news.google.com" in link:
        return ""
    try:
        resp = requests.get(link, timeout=timeout, headers=_OG_UA)
        html = resp.text[:200_000]
    except requests.RequestException as e:
        logger.debug(f"news_fetcher: og:image fetch failed for {link}: {e}")
        return ""
    m = re.search(
        r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)(?::src)?["\'][^>]+content=["\']([^"\']+)',
        html, re.IGNORECASE,
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)',
            html, re.IGNORECASE,
        )
    return m.group(1) if m else ""


def fetch_headlines() -> list[dict]:
    """Fetch + aggregate recent headlines from all configured feeds.

    Returns a list of {title, summary, source, published, link}, newest first,
    deduped by normalized title, capped at MAX_ITEMS.
    """
    cutoff = time.time() - MAX_AGE_SEC
    seen_titles: set[str] = set()
    items: list[dict] = []

    for source, url in FEEDS:
        try:
            parsed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0"})
        except Exception as e:
            logger.warning(f"news_fetcher: failed to fetch {source}: {e}")
            continue

        for entry in getattr(parsed, "entries", []):
            title = (getattr(entry, "title", "") or "").strip()
            if not title:
                continue
            ts = _entry_ts(entry)
            if ts < cutoff:
                continue
            norm = _normalize_title(title)
            if norm in seen_titles:
                continue
            seen_titles.add(norm)

            summary = (getattr(entry, "summary", "") or "").strip()
            # Google News/RSS summaries are sometimes raw HTML — keep it short and plain-ish.
            if len(summary) > 180:
                summary = summary[:180] + "…"

            items.append({
                "title": title,
                "summary": summary,
                "source": source,
                "published": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M"),
                "link": getattr(entry, "link", ""),
                "image": _entry_image(entry),
                "_ts": ts,
            })

    items.sort(key=lambda x: x["_ts"], reverse=True)
    items = items[:MAX_ITEMS]
    for it in items:
        it.pop("_ts", None)

    logger.info(f"news_fetcher: aggregated {len(items)} headlines from {len(FEEDS)} feeds")
    return items
