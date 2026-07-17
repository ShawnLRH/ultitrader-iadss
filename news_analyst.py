"""
Daily AI news analysis — Groq LLM turns aggregated headlines into:
  1. A vintage-newspaper-style briefing PDF ("Breaking News" broadsheet look:
     aged-paper pages, stamped headlines, kickers, pull-quotes, grayscale
     photos pulled from the scraped articles, and a front-page "Desk
     Barometer" box rendering the five macro factors)
  2. Five 1-100 macro factors (bull, instability, geopolitical risk, economic
     momentum, fed lean)

Two separate Groq calls (long-form + strict JSON) rather than one mixed call —
a malformed JSON block in a single response would otherwise risk losing the
whole newsletter too. The factor call always falls back to neutral defaults on
any parse failure so a bad AI response can never crash the daily job or corrupt
the live trading gate in signal_engine.py. Likewise the newsletter call uses a
plain-text ===MARKER=== format instead of JSON: if parsing fails, the PDF falls
back to rendering the raw text so a sloppy AI response still produces a usable
briefing.

Groq call pattern reused from recipreneur/adminside/utils.py (GROQ_API_URL,
Bearer auth, OpenAI-compatible chat/completions payload).
"""
import io
import os
import re
import time
import json
import glob
import logging
import datetime
import threading
from xml.sax.saxutils import escape

import requests
from PIL import Image as PILImage, ImageOps
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_JUSTIFY, TA_CENTER, TA_RIGHT
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
    Flowable, HRFlowable, KeepTogether, Image as RLImage,
)

from news_fetcher import fetch_headlines, resolve_article_image

logger = logging.getLogger(__name__)

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

FACTOR_KEYS = [
    "bull_factor",
    "instability_factor",
    "geopolitical_risk_factor",
    "economic_momentum_factor",
    "fed_policy_lean",
]

FACTOR_LABELS = {
    "bull_factor":              "Bull Factor (1=bearish, 100=bullish)",
    "instability_factor":       "Instability Factor (1=stable, 100=extreme)",
    "geopolitical_risk_factor": "Geopolitical Risk (1=calm, 100=high risk)",
    "economic_momentum_factor": "Economic Momentum (1=recession, 100=strong growth)",
    "fed_policy_lean":          "Fed/Rate Policy Lean (1=dovish, 100=hawkish)",
}

_NEUTRAL_FACTORS = {k: 50 for k in FACTOR_KEYS}
MIN_NEWSLETTER_WORDS = 450  # below this, trigger one expansion pass so the paper isn't thin


def _call_groq(cfg, messages, max_tokens=4000, json_mode=False, timeout=90, retries=3) -> str:
    headers = {
        "Authorization": f"Bearer {cfg.GROQ_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": cfg.GROQ_MODEL,
        "messages": messages,
        "temperature": 0.4,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    last_exc = None
    for attempt in range(retries):
        try:
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=timeout)
            if resp.status_code == 429 or resp.status_code >= 500:
                # 429 here is Groq's tokens-per-minute cap — needs a full minute to clear,
                # not a short backoff. 5xx gets a shorter retry.
                default_wait = 65 if resp.status_code == 429 else 5 * (attempt + 1)
                wait = int(resp.headers.get("Retry-After", default_wait))
                logger.warning(f"_call_groq: {resp.status_code}, retrying in {wait}s (attempt {attempt+1}/{retries})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except requests.RequestException as e:
            last_exc = e
            logger.warning(f"_call_groq: request error {e}, retrying (attempt {attempt+1}/{retries})")
            time.sleep(3 * (attempt + 1))
    raise last_exc or RuntimeError("_call_groq: exhausted retries")


def _headlines_block(headlines: list[dict]) -> str:
    """Numbered so the edition format can reference stories by index; [PHOTO]
    marks entries whose feed carries an image the PDF can actually use."""
    lines = []
    for i, h in enumerate(headlines, 1):
        photo = " [PHOTO]" if (h.get("image") or "news.google.com" not in h.get("link", "")) else ""
        line = f"{i}.{photo} [{h['source']}] {h['title']}"
        if h.get("summary"):
            line += f" — {h['summary']}"
        lines.append(line)
    return "\n".join(lines)


_STORY_FIELDS = """FACTOR_IMPACT: <name the factor indicators that move and the direction, with the reason — e.g. "Geopolitical Risk UP — shipping routes threatened; Economic Momentum DOWN — export demand weakening">
MARKET_VIEW: <2-3 sentences: which assets are biased UP or DOWN and through which mechanism — earnings, discount rates, supply, demand, risk premium, liquidity or positioning. Name indices, commodities, currencies, sectors or crypto. No unsupported ticker dumping.>
CATALYST: <the next scheduled event, data release or decision that moves this story, with timing if known>
INVALIDATION: <the specific evidence that would prove this view wrong>
CONFIDENCE: <High, Medium or Low> | HORIZON: <intraday / days / 1-3 weeks / months>"""

_EDITION_FORMAT = f"""===MARKET_IN_30_SECONDS===
<5 to 8 lines, one asset/market per line, format "MARKET: DIRECTION, size — main cause",
e.g. "S&P 500 FUTURES: DOWN 1.2% — chipmaker selloff spreads". Cover major indices,
rates/dollar, oil or gold, and BTC/ETH — but only what today's headlines actually support.>
===MAIN_HEADLINE===
<3 to 6 words, ALL CAPS, punchy front-page headline for the single most important market development — may end with !!>
===DECK===
<one bold sentence, 12-22 words, expanding on the main headline>
===TOP_SIGNAL===
KICKER: <3-6 word label>
IMAGE: <number of the most front-page-worthy headline, prefer ones tagged [PHOTO]>
SOURCES: <numbers of the headlines this story draws on, e.g. 3, 7, 12>
WHY_TOP: <one sentence: why this development outranks every other story today>
<two short paragraphs, 50-80 words each: FIRST the verified facts — dates, numbers,
names, decisions; SECOND why it matters now — what changed versus expectations>
{_STORY_FIELDS}
===STORY: <headline stating the event AND its market consequence, 6-12 words>===
IMAGE: <headline number whose photo fits, prefer [PHOTO], or NONE>
SOURCES: <headline numbers this story draws on>
<two short paragraphs, 45-75 words each: FIRST verified facts, SECOND why it matters now>
{_STORY_FIELDS}
===IMPACT_BOARD===
WINNERS: <assets/sectors biased higher today, each with a two-or-three-word reason>
LOSERS: <assets/sectors biased lower, each with a short reason>
HEDGES: <what hedges today's dominant risks>
NEUTRAL: <major assets with no clear bias today>
===DESK_VIEW===
BASE: <most likely path for risk assets over the horizon, with its trigger>
UPSIDE: <bull case and what triggers it>
DOWNSIDE: <bear case and what triggers it>
===WHAT_CHANGES_OUR_VIEW===
<3 to 5 lines, one per line: a specific data release, price level, policy decision or
geopolitical event that would reverse today's conclusions>"""


def generate_newsletter_text(cfg, headlines: list[dict]) -> str:
    """Structured 'daily risk brief' edition text — parsed by _parse_edition for the vintage PDF."""
    prompt = f"""You are the editor of ULTITRADER — a 5-minute global risk brief for an active
trading desk that trades US stocks and crypto. It must read like a decision brief, not a
general newspaper: the news is the evidence, the factor indicators are the interpretation,
the market view is the conclusion.

THE ONE RULE: every story must answer — what does this change in the market, which factor
indicator moves, which assets may rise or fall, and why (through which mechanism)?

The factor indicators are: Bull Factor, Fear & Instability, Geopolitical Risk,
Economic Momentum, Fed Policy Lean.

Using ONLY the numbered headlines below (today's aggregated news), write today's edition.

OUTPUT FORMAT — copy the ===MARKER=== lines EXACTLY as shown, fill in the content.
Write 4 to 6 ===STORY: ...=== blocks. No markdown, no JSON, no extra commentary:

{_EDITION_FORMAT}

Story selection rules:
- Include only events with a credible transmission mechanism into prices, earnings,
  rates, commodities, currencies, liquidity or risk appetite.
- Include contradictory evidence where it exists — it sharpens the scenarios.
- Exclude human-interest stories unless they change market risk, policy or supply chains.
- Never merely say an event "could affect markets" — always give the direction and the
  mechanism, and separate reported facts from desk interpretation.
- Be specific: cite the actual events, countries, companies and figures from the headlines.
- Short, punchy paragraphs. The whole issue must be readable in about five minutes.
- IMAGE and SOURCES numbers must reference the numbered headline list below.

TODAY'S NUMBERED HEADLINES:
{_headlines_block(headlines)}
"""
    try:
        text = _call_groq(cfg, [{"role": "user", "content": prompt}], max_tokens=3000)
    except Exception as e:
        logger.error(f"generate_newsletter_text failed: {e}")
        return (
            "## Newsletter generation failed\n\n"
            f"The AI analysis step errored out: {e}\n"
            "Raw headlines were still collected for today; see the log for details."
        )

    word_count = len(text.split())
    if word_count < MIN_NEWSLETTER_WORDS:
        logger.info(f"generate_newsletter_text: first draft only {word_count} words, requesting an expansion pass")
        time.sleep(65)  # fresh TPM window before the follow-up call
        expand_prompt = f"""The newspaper edition below is too short ({word_count} words). Rewrite it,
keeping EXACTLY the same ===MARKER=== structure, the same KICKER:/HEADLINE:/IMAGE:/QUOTE:
fields and the same facts, but deepen the stories: causes, named parties, numbers,
second-order effects, and sharper MARKET_VIEW mechanisms. Keep every ===MARKER=== line
and every FIELD: line (FACTOR_IMPACT, MARKET_VIEW, CATALYST, INVALIDATION, CONFIDENCE)
exactly in place. Target 900-1200 words total, and keep every individual paragraph
under 80 words — add stories rather than writing longer paragraphs. Output the full
rewritten edition in the same format, nothing else.

CURRENT DRAFT:
{text}
"""
        try:
            expanded = _call_groq(cfg, [{"role": "user", "content": expand_prompt}], max_tokens=3000)
            if len(expanded.split()) > word_count:
                text = expanded
        except Exception as e:
            logger.warning(f"generate_newsletter_text: expansion pass failed, keeping original draft: {e}")

    return text


def generate_factors(cfg, headlines: list[dict]) -> dict:
    """Strict-JSON 1-100 macro factors + one-line rationale each. Fails safe to neutral (50)."""
    prompt = f"""Based ONLY on the headlines below, output a JSON object scoring today's
macro/market conditions. Every score is an integer 1-100. Respond with ONLY the JSON
object, no other text, in exactly this shape:

{{
  "bull_factor": <int 1-100, 1=very bearish for risk assets, 100=very bullish>,
  "instability_factor": <int 1-100, 1=stable/calm, 100=extremely unstable/crisis-like>,
  "geopolitical_risk_factor": <int 1-100, 1=calm, 100=high war/conflict/sanctions risk>,
  "economic_momentum_factor": <int 1-100, 1=recessionary signals, 100=strong growth signals>,
  "fed_policy_lean": <int 1-100, 1=very dovish/rate-cut-leaning, 100=very hawkish/tightening-leaning>,
  "rationale": {{
    "bull_factor": "<one sentence: the evidence from today's headlines behind this score>",
    "instability_factor": "<one sentence>",
    "geopolitical_risk_factor": "<one sentence>",
    "economic_momentum_factor": "<one sentence>",
    "fed_policy_lean": "<one sentence>"
  }},
  "market_link": {{
    "bull_factor": "<one sentence: which assets this level favors or pressures, and why>",
    "instability_factor": "<one sentence>",
    "geopolitical_risk_factor": "<one sentence>",
    "economic_momentum_factor": "<one sentence>",
    "fed_policy_lean": "<one sentence>"
  }}
}}

TODAY'S AGGREGATED HEADLINES:
{_headlines_block(headlines)}
"""
    raw = None
    try:
        raw = _call_groq(cfg, [{"role": "user", "content": prompt}], max_tokens=900, json_mode=True)
    except Exception as e:
        logger.error(f"generate_factors: Groq call failed: {e}")

    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except Exception as e:
            logger.warning(f"generate_factors: direct parse failed ({e}), trying regex fallback")
            try:
                match = re.search(r"\{.*\}", raw, re.DOTALL)
                if match:
                    parsed = json.loads(match.group(0))
            except Exception as e2:
                logger.error(f"generate_factors: regex fallback also failed: {e2}")

    if not isinstance(parsed, dict):
        logger.error("generate_factors: no usable JSON — falling back to neutral (50) defaults")
        result = dict(_NEUTRAL_FACTORS)
        result["rationale"] = {k: "AI analysis unavailable — neutral default." for k in FACTOR_KEYS}
        result["market_link"] = {}
        return result

    result = {}
    for k in FACTOR_KEYS:
        try:
            v = int(round(float(parsed.get(k, 50))))
        except (TypeError, ValueError):
            v = 50
        result[k] = max(1, min(100, v))
    result["rationale"] = parsed.get("rationale", {}) if isinstance(parsed.get("rationale"), dict) else {}
    result["market_link"] = parsed.get("market_link", {}) if isinstance(parsed.get("market_link"), dict) else {}
    return result


# ---------------------------------------------------------------------------
# Edition parsing — the ===MARKER=== plain-text format from generate_newsletter_text
# ---------------------------------------------------------------------------

def _strip_md(text: str) -> str:
    """Remove markdown artifacts the LLM sometimes injects despite instructions
    (**bold**, `code`, leading #s) — they'd render literally in the PDF."""
    text = re.sub(r"[*_`]+", "", text)
    return text.lstrip("# ").strip()


# Fields a story block may carry. "Long" fields accept wrapped continuation lines.
_FIELD_KEYS = (
    "KICKER", "HEADLINE", "IMAGE", "QUOTE", "SOURCES", "WHY_TOP", "FACTOR_IMPACT",
    "MARKET_VIEW", "CATALYST", "INVALIDATION", "CONFIDENCE",
    "WINNERS", "LOSERS", "HEDGES", "NEUTRAL", "BASE", "UPSIDE", "DOWNSIDE",
)
# WHY_TOP is deliberately NOT here: the story paragraphs follow it directly
# (often without a blank line), and continuation-gluing would swallow them.
_LONG_FIELDS = {
    "FACTOR_IMPACT", "MARKET_VIEW", "CATALYST", "INVALIDATION",
    "WINNERS", "LOSERS", "HEDGES", "NEUTRAL", "BASE", "UPSIDE", "DOWNSIDE",
}
_FIELD_RE = re.compile(rf"^[*_`#\-\s]*({'|'.join(_FIELD_KEYS)})\s*:\s*(.*)$", re.IGNORECASE)


def _parse_block(body: str) -> dict:
    """One TOP_SIGNAL/STORY/board body → {fields, paras, image_idx, sources,
    confidence, horizon}. A non-blank line directly after a long field is treated
    as that field wrapping, not a new paragraph."""
    out = {"fields": {}, "paras": [], "image_idx": None, "sources": [], "confidence": "", "horizon": ""}
    plain_lines: list[str] = []
    last_field = None
    for raw in body.split("\n"):
        line = raw.strip()
        if not line:
            plain_lines.append("")
            last_field = None
            continue
        m = _FIELD_RE.match(line)
        if m:
            key, val = m.group(1).upper(), _strip_md(m.group(2)).strip('"“”')
            out["fields"][key] = val
            last_field = key if key in _LONG_FIELDS else None
        elif last_field:
            out["fields"][last_field] += " " + _strip_md(line)
        else:
            plain_lines.append(raw)
    rest = "\n".join(plain_lines).strip()
    out["paras"] = [_strip_md(" ".join(p.split())) for p in re.split(r"\n\s*\n", rest) if p.strip()]

    if "IMAGE" in out["fields"]:
        num = re.search(r"\d+", out["fields"]["IMAGE"])
        out["image_idx"] = int(num.group(0)) if num else None
    if "SOURCES" in out["fields"]:
        out["sources"] = [int(n) for n in re.findall(r"\d+", out["fields"]["SOURCES"])][:8]
    conf = out["fields"].get("CONFIDENCE", "")
    if conf:
        # "Medium | HORIZON: 1-3 weeks" — HORIZON may ride on the same line
        hm = re.search(r"HORIZON\s*:?\s*(.+)$", conf, re.IGNORECASE)
        out["horizon"] = _strip_md(hm.group(1)) if hm else ""
        out["confidence"] = conf.split("|")[0].strip().strip('.')
    return out


def _parse_edition(text: str) -> dict | None:
    """Parse the ===MARKER=== risk-brief format. Returns None if it doesn't look
    structured (renderer then falls back to plain rendering of the raw text)."""
    parts = re.split(r"^\s*===\s*(.+?)\s*===\s*$", text, flags=re.MULTILINE)
    if len(parts) < 5:
        return None
    ed = {
        "main_headline": "", "deck": "", "ticker": [], "top": None, "stories": [],
        "impact": {}, "desk_view": {}, "view_changers": [],
    }
    for marker, body in zip(parts[1::2], parts[2::2]):
        marker = marker.strip().upper()
        body = body.strip()
        if marker == "MAIN_HEADLINE":
            ed["main_headline"] = _strip_md(" ".join(body.split())).upper()
        elif marker == "DECK":
            ed["deck"] = _strip_md(" ".join(body.split()))
        elif marker == "MARKET_IN_30_SECONDS":
            ed["ticker"] = [_strip_md(ln.strip().lstrip("-•· ")) for ln in body.split("\n") if ln.strip()][:8]
        elif marker == "TOP_SIGNAL":
            ed["top"] = _parse_block(body)
        elif marker.startswith("STORY"):
            blk = _parse_block(body)
            blk["headline"] = _strip_md(marker.split(":", 1)[1].strip()) if ":" in marker else ""
            ed["stories"].append(blk)
        elif marker == "IMPACT_BOARD":
            ed["impact"] = _parse_block(body)["fields"]
        elif marker == "DESK_VIEW":
            ed["desk_view"] = _parse_block(body)["fields"]
        elif marker == "WHAT_CHANGES_OUR_VIEW":
            ed["view_changers"] = [_strip_md(ln.strip().lstrip("-•· ")) for ln in body.split("\n") if ln.strip()][:6]
    if not ed["main_headline"] or not ed["top"] or len(ed["stories"]) < 3:
        return None
    return ed


# ---------------------------------------------------------------------------
# Vintage newspaper PDF design
# ---------------------------------------------------------------------------

PAPER      = colors.HexColor("#f0ead9")   # aged newsprint cream
PAPER_DIM  = colors.HexColor("#e4dcc6")   # slightly darker cream (bar tracks, boxes)
INK        = colors.HexColor("#181410")   # near-black ink
INK_SOFT   = colors.HexColor("#4a4239")   # faded ink for captions/rationale

_FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
_FONTS: dict | None = None


def _register_fonts() -> dict:
    """Register the bundled vintage fonts; fall back to built-ins if missing."""
    global _FONTS
    if _FONTS:
        return _FONTS
    try:
        pdfmetrics.registerFont(TTFont("Playfair-Black", os.path.join(_FONT_DIR, "playfair-black.ttf")))
        pdfmetrics.registerFont(TTFont("Oswald-Bold", os.path.join(_FONT_DIR, "oswald-bold.ttf")))
        pdfmetrics.registerFont(TTFont("Oswald-Medium", os.path.join(_FONT_DIR, "oswald-medium.ttf")))
        _FONTS = {"masthead": "Playfair-Black", "head": "Oswald-Bold", "label": "Oswald-Medium"}
    except Exception as e:
        logger.warning(f"_register_fonts: bundled fonts unavailable ({e}) — using built-ins")
        _FONTS = {"masthead": "Times-Bold", "head": "Helvetica-Bold", "label": "Helvetica-Bold"}
    return _FONTS


class _PosterLine(Flowable):
    """One line of text scaled to span the full column width — the stamped
    vintage-poster headline effect from the reference design."""

    def __init__(self, text, font, max_size=88, min_size=18, color=INK, tracking=0.99):
        super().__init__()
        self.text = text
        self.font = font
        self.max_size = max_size
        self.min_size = min_size
        self.color = color
        self.tracking = tracking
        self.size = min_size

    def wrap(self, availWidth, availHeight):
        self.avail_width = availWidth
        w1000 = pdfmetrics.stringWidth(self.text, self.font, 1000)
        size = (availWidth * self.tracking * 1000.0 / w1000) if w1000 > 0 else self.min_size
        self.size = max(self.min_size, min(self.max_size, size))
        self.height = self.size * 1.04
        return availWidth, self.height

    def draw(self):
        self.canv.setFont(self.font, self.size)
        self.canv.setFillColor(self.color)
        self.canv.drawCentredString(self.avail_width / 2.0, self.size * 0.16, self.text)


def _hr(thickness=1.0, space_before=0, space_after=0, color=INK):
    return HRFlowable(width="100%", thickness=thickness, color=color, lineCap="butt",
                      spaceBefore=space_before, spaceAfter=space_after)


def _double_rule(story, heavy=2.6, light=0.8, gap=2.2, space_before=0, space_after=0, heavy_first=True):
    story.append(_hr(heavy if heavy_first else light, space_before=space_before))
    story.append(Spacer(1, gap))
    story.append(_hr(light if heavy_first else heavy, space_after=space_after))


def _split_headline(text: str) -> list[str]:
    """Split the main headline into 1-2 poster lines. Mimics the reference cover:
    a short huge first line and a longer second line (~40% / 60% split)."""
    text = " ".join(text.split())
    if len(text) <= 14:
        return [text]
    words = text.split()
    if len(words) == 1:
        return [text]
    target = len(text) * 0.42
    best, best_diff = 1, float("inf")
    for i in range(1, len(words)):
        l1 = len(" ".join(words[:i]))
        diff = abs(l1 - target)
        if diff < best_diff:
            best, best_diff = i, diff
    return [" ".join(words[:best]), " ".join(words[best:])]


# --- article images -> grayscale newsprint photos ---

_IMG_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _download_newsprint_image(url: str):
    """Download an article image and convert it to a vintage grayscale newsprint
    photo. Returns (jpeg_bytesio, px_width, px_height) or None."""
    try:
        resp = requests.get(url, timeout=10, headers=_IMG_UA)
        resp.raise_for_status()
        img = PILImage.open(io.BytesIO(resp.content))
        img.load()
    except Exception as e:
        logger.info(f"_download_newsprint_image: skip {url[:80]} ({e})")
        return None
    if img.width < 240 or img.height < 140:   # icons/logos — not photo material
        return None
    if img.mode != "L":
        img = img.convert("L")
    img = ImageOps.autocontrast(img, cutoff=1)
    if img.width > 1400:
        img = img.resize((1400, int(img.height * 1400 / img.width)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82)
    buf.seek(0)
    return buf, img.width, img.height


def _photo_flowable(img_data, target_w, max_h, caption, fonts):
    """Bordered grayscale photo + small caption, as a single flowable."""
    buf, pw, ph = img_data
    w = target_w
    h = w * ph / pw
    if h > max_h:
        h = max_h
        w = h * pw / ph
    photo = RLImage(buf, width=w, height=h)
    caption_style = ParagraphStyle(
        "Caption", fontName=fonts["label"], fontSize=7.2, leading=9,
        textColor=INK_SOFT, alignment=TA_CENTER, spaceBefore=3,
    )
    inner = [photo]
    if caption:
        inner.append(Paragraph(escape(caption.upper()), caption_style))
    frame = Table([[inner]], colWidths=[w + 8])
    frame.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.4, INK),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    return frame


def _image_for_story(headlines, image_idx, used_urls) -> tuple | None:
    """Resolve the image for a story block: RSS-embedded image first, then an
    og:image scrape of the article page. Returns (img_data, caption) or None."""
    if not headlines or not image_idx or not (1 <= image_idx <= len(headlines)):
        return None
    h = headlines[image_idx - 1]
    for url in (h.get("image", ""), None):
        if url is None:
            url = resolve_article_image(h.get("link", ""))
        if not url or url in used_urls:
            continue
        data = _download_newsprint_image(url)
        if data:
            used_urls.add(url)
            title = h.get("title", "")
            if len(title) > 78:
                title = title[:78].rsplit(" ", 1)[0] + "…"
            caption = f"{h.get('source', '')} — {title}"
            return data, caption
    return None


def _any_image(headlines, used_urls) -> tuple | None:
    """Front-page fallback: first downloadable RSS-embedded image in the pool."""
    for i, h in enumerate(headlines or [], 1):
        if h.get("image"):
            got = _image_for_story(headlines, i, used_urls)
            if got:
                return got
    return None


# --- factors -> "The Desk Barometer" ---

# Which factors read "high = bad" (mirrors FACTOR_META.invert in templates/dashboard.html).
FACTOR_INVERT = {
    "bull_factor": False,
    "instability_factor": True,
    "geopolitical_risk_factor": True,
    "economic_momentum_factor": False,
    "fed_policy_lean": True,
}

# Vintage-print display names + low/mid/high verdict stamps.
FACTOR_PRINT = {
    "bull_factor":              ("BULL FACTOR",           ("BEARISH", "NEUTRAL", "BULLISH")),
    "instability_factor":       ("FEAR & INSTABILITY",    ("CALM", "UNEASY", "FEARFUL")),
    "geopolitical_risk_factor": ("GEOPOLITICAL RISK",     ("QUIET", "ELEVATED", "HIGH ALERT")),
    "economic_momentum_factor": ("ECONOMIC MOMENTUM",     ("CONTRACTING", "MIXED", "EXPANDING")),
    "fed_policy_lean":          ("FED POLICY LEAN",       ("DOVISH", "BALANCED", "HAWKISH")),
}

def _verdict(key: str, value: int) -> str:
    words = FACTOR_PRINT[key][1]
    return words[0] if value < 34 else (words[1] if value < 66 else words[2])


def _factor_bar(value: int, width, fonts, height=0.13 * inch):
    """Monochrome ink gauge: filled black portion on a dimmed track, boxed."""
    filled = max(0.04 * inch, width * value / 100.0)
    track = max(0.04 * inch, width - filled)
    bar = Table([["", ""]], colWidths=[filled, track], rowHeights=[height])
    bar.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, 0), INK),
        ("BACKGROUND", (1, 0), (1, 0), PAPER_DIM),
        ("BOX", (0, 0), (-1, -1), 1.0, INK),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return bar


def _barometer(factors: dict, prev_factors: dict | None, width, fonts) -> KeepTogether:
    """'THE DESK BAROMETER' box — the five macro factors as vintage ink gauges
    with verdict stamps, change vs the previous issue, the evidence behind each
    score and its market consequence."""
    label_style = ParagraphStyle("BLabel", fontName=fonts["head"], fontSize=10.5, leading=12, textColor=INK)
    verdict_style = ParagraphStyle("BVerdict", fontName=fonts["head"], fontSize=10.5, leading=12,
                                   textColor=INK, alignment=TA_RIGHT)
    rationale_style = ParagraphStyle("BRat", fontName="Times-Italic", fontSize=8.6, leading=10.5,
                                     textColor=INK_SOFT)
    title_style = ParagraphStyle("BTitle", fontName=fonts["head"], fontSize=13, leading=15,
                                 textColor=PAPER, alignment=TA_CENTER)
    sub_title_style = ParagraphStyle("BSub", fontName=fonts["label"], fontSize=7.5, leading=9,
                                     textColor=PAPER, alignment=TA_CENTER)

    inner_w = width - 2 * 10  # box padding
    rationale = factors.get("rationale", {})
    market_link = factors.get("market_link", {}) if isinstance(factors.get("market_link"), dict) else {}
    rows, styles_extra = [], []
    rows.append([[Paragraph("THE DESK BAROMETER", title_style),
                  Paragraph("TODAY'S AI MACRO FACTORS  *  SCORED 1-100  *  CHANGE VS PREVIOUS ISSUE", sub_title_style)]])
    styles_extra.append(("BACKGROUND", (0, 0), (-1, 0), INK))

    for k in FACTOR_KEYS:
        value = max(1, min(100, int(factors.get(k, 50))))
        name = FACTOR_PRINT[k][0]
        delta_txt = "new"
        try:
            prev_v = int(prev_factors.get(k)) if prev_factors else None
            if prev_v is not None:
                d = value - prev_v
                delta_txt = f"{'+' if d > 0 else ''}{d}" if d else "unch"
        except (TypeError, ValueError):
            pass
        header = Table(
            [[Paragraph(name, label_style),
              Paragraph(f"{value} / 100 ({delta_txt}) — {_verdict(k, value)}", verdict_style)]],
            colWidths=[inner_w * 0.45, inner_w * 0.55],
        )
        header.setStyle(TableStyle([
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        block = [header, _factor_bar(value, inner_w, fonts), Spacer(1, 2)]
        note = rationale.get(k, "")
        if note:
            block.append(Paragraph(f'<font name="{fonts["head"]}" size="7.4">EVIDENCE:</font> {escape(note)}',
                                   rationale_style))
        link = market_link.get(k, "")
        if link:
            block.append(Paragraph(f'<font name="{fonts["head"]}" size="7.4">MARKET LINK:</font> {escape(link)}',
                                   rationale_style))
        block.append(Spacer(1, 5))
        rows.append([block])

    box = Table(rows, colWidths=[width])
    box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 2.2, INK),
        ("LINEBELOW", (0, 0), (-1, 0), 1.0, INK),
        ("BACKGROUND", (0, 1), (-1, -1), PAPER),
        ("TOPPADDING", (0, 0), (-1, 0), 6), ("BOTTOMPADDING", (0, 0), (-1, 0), 6),
        ("TOPPADDING", (0, 1), (-1, -1), 4), ("BOTTOMPADDING", (0, 1), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        *styles_extra,
    ]))
    return KeepTogether(box)


def _brief_box(title, inner_rows, width, fonts, subtitle=""):
    """Black title bar + content rows in a bordered box — shared chrome for the
    ticker, impact board, desk view and view-changers blocks."""
    title_style = ParagraphStyle("BoxTitle", fontName=fonts["head"], fontSize=11.5, leading=13.5,
                                 textColor=PAPER, alignment=TA_CENTER)
    sub_style = ParagraphStyle("BoxSub", fontName=fonts["label"], fontSize=7, leading=9,
                               textColor=PAPER, alignment=TA_CENTER)
    head = [Paragraph(title, title_style)]
    if subtitle:
        head.append(Paragraph(subtitle, sub_style))
    rows = [[head]] + [[r] for r in inner_rows]
    box = Table(rows, colWidths=[width])
    box.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.8, INK),
        ("BACKGROUND", (0, 0), (-1, 0), INK),
        ("LINEBELOW", (0, 0), (-1, 0), 1.0, INK),
        ("TOPPADDING", (0, 0), (-1, 0), 5), ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("TOPPADDING", (0, 1), (-1, -1), 2), ("BOTTOMPADDING", (0, 1), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 9), ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ]))
    return box


def _ticker_box(items, width, fonts):
    """'Market in 30 Seconds' — one bolded line per market move."""
    line_style = ParagraphStyle("Tick", fontName="Times-Roman", fontSize=9.4, leading=12.5, textColor=INK)
    rows = []
    for it in items:
        it = escape(it)
        if ":" in it:
            head, rest = it.split(":", 1)
            it = f'<font name="{fonts["head"]}" size="9">{head.upper()}:</font>{rest}'
        rows.append(Paragraph(it, line_style))
    return _brief_box("MARKET IN 30 SECONDS", rows, width, fonts,
                      subtitle="THE OVERNIGHT TAPE AT A GLANCE")


def _desk_card(blk, width, fonts):
    """The story's decision footer: factor impact, market view + mechanism,
    next catalyst, invalidation and confidence/horizon."""
    f = blk.get("fields", {})
    label_style = ParagraphStyle("CardLab", fontName=fonts["head"], fontSize=7.8, leading=10, textColor=INK)
    val_style = ParagraphStyle("CardVal", fontName="Times-Roman", fontSize=9.2, leading=11.8, textColor=INK)
    conf = " · ".join(x for x in (blk.get("confidence", ""), blk.get("horizon", "")) if x)
    rows = []
    for label, val in (
        ("FACTOR IMPACT", f.get("FACTOR_IMPACT", "")),
        ("MARKET VIEW", f.get("MARKET_VIEW", "")),
        ("NEXT CATALYST", f.get("CATALYST", "")),
        ("INVALIDATION", f.get("INVALIDATION", "")),
        ("CONFIDENCE / HORIZON", conf),
    ):
        if val:
            rows.append([Paragraph(label, label_style), Paragraph(escape(val), val_style)])
    if not rows:
        return None
    card = Table(rows, colWidths=[1.35 * inch, width - 1.35 * inch])
    card.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.2, INK),
        ("LINEBELOW", (0, 0), (-1, -2), 0.4, INK_SOFT),
        ("BACKGROUND", (0, 0), (0, -1), PAPER_DIM),
        ("LINEAFTER", (0, 0), (0, -1), 0.8, INK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return card


def _impact_board(fields, width, fonts):
    """Cross-Market Impact Board — 2x2 winners/losers/hedges/neutral grid."""
    lab_style = ParagraphStyle("ImpLab", fontName=fonts["head"], fontSize=8.8, leading=11, textColor=INK)
    txt_style = ParagraphStyle("ImpTxt", fontName="Times-Roman", fontSize=9, leading=11.5, textColor=INK)
    cells = []
    for key, label in (("WINNERS", "LIKELY WINNERS"), ("LOSERS", "LIKELY LOSERS"),
                       ("HEDGES", "HEDGES"), ("NEUTRAL", "NEUTRAL")):
        if fields.get(key):
            cells.append([Paragraph(label, lab_style), Spacer(1, 2),
                          Paragraph(escape(fields[key]), txt_style)])
    if not cells:
        return None
    while len(cells) % 2:
        cells.append([Paragraph("", txt_style)])
    grid = Table([cells[i:i + 2] for i in range(0, len(cells), 2)],
                 colWidths=[(width - 18) / 2.0] * 2)
    grid.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, INK_SOFT),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return _brief_box("CROSS-MARKET IMPACT BOARD", [grid], width, fonts,
                      subtitle="WHO GAINS, WHO LOSES, WHAT HEDGES")


def _desk_view_box(fields, view_changers, width, fonts):
    """Desk View & Scenarios (base/upside/downside) + What Changes Our View."""
    lab_style = ParagraphStyle("DvLab", fontName=fonts["head"], fontSize=8.2, leading=10.5, textColor=INK)
    txt_style = ParagraphStyle("DvTxt", fontName="Times-Roman", fontSize=9.2, leading=11.8, textColor=INK)
    rows = []
    for key, label in (("BASE", "BASE CASE"), ("UPSIDE", "UPSIDE CASE"), ("DOWNSIDE", "DOWNSIDE CASE")):
        if fields.get(key):
            rows.append(Table([[Paragraph(label, lab_style), Paragraph(escape(fields[key]), txt_style)]],
                              colWidths=[1.15 * inch, width - 18 - 1.15 * inch],
                              style=TableStyle([
                                  ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                  ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                              ])))
    if view_changers:
        rows.append(Spacer(1, 3))
        rows.append(_hr(0.6, color=INK_SOFT))
        rows.append(Spacer(1, 3))
        rows.append(Paragraph("WHAT CHANGES OUR VIEW", lab_style))
        for item in view_changers:
            rows.append(Paragraph(f"— {escape(item)}", txt_style))
    if not rows:
        return None
    return _brief_box("DESK VIEW &amp; SCENARIOS", rows, width, fonts,
                      subtitle="BASE / UPSIDE / DOWNSIDE — AND WHAT WOULD REVERSE THE CALL")


def _sources_block(ed, headlines, fonts):
    """Numbered source list for every story's cited headlines + compile timestamp,
    separating reported facts from desk interpretation."""
    idxs: list[int] = []
    blocks = ([ed["top"]] if ed.get("top") else []) + ed.get("stories", [])
    for b in blocks:
        for n in b.get("sources", []):
            if n not in idxs and headlines and 1 <= n <= len(headlines):
                idxs.append(n)
    src_style = ParagraphStyle("Src", fontName="Times-Roman", fontSize=8, leading=10.4, textColor=INK_SOFT)
    lab_style = ParagraphStyle("SrcLab", fontName=fonts["head"], fontSize=10, leading=12.5, textColor=INK)
    flows: list = [Spacer(1, 12), _hr(1.6), Spacer(1, 4),
                   Paragraph("SOURCES &amp; TIMESTAMP", lab_style), Spacer(1, 3)]
    for n in idxs[:16]:
        h = headlines[n - 1]
        flows.append(Paragraph(
            f"[{n}]  {escape(h.get('source', ''))} — {escape(h.get('title', ''))}"
            f" ({escape(h.get('published', ''))} UTC)", src_style))
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M")
    flows.append(Spacer(1, 4))
    flows.append(Paragraph(
        f"Compiled {stamp} UTC · ULTITRADER IADSS DESK. Reported facts are drawn from the sources above; "
        "factor scores, market views, scenarios and invalidation points are desk interpretation "
        "generated by AI and are not investment advice.", src_style))
    # One atomic block — a lone timestamp orphaned on its own page looks broken.
    return [KeepTogether(flows)]


# --- page furniture ---

def _page_decor(fonts, date_line):
    def draw(canv, doc):
        W, H = LETTER
        canv.saveState()
        canv.setFillColor(PAPER)
        canv.rect(0, 0, W, H, stroke=0, fill=1)
        # footer
        canv.setStrokeColor(INK)
        canv.setLineWidth(1.1)
        canv.line(doc.leftMargin, 0.62 * inch, W - doc.rightMargin, 0.62 * inch)
        canv.setFont(fonts["label"], 8)
        canv.setFillColor(INK)
        canv.drawString(doc.leftMargin, 0.42 * inch, "ULTITRADER IADSS  *  DAILY MARKET INTELLIGENCE")
        canv.drawRightString(W - doc.rightMargin, 0.42 * inch, f"PAGE {canv.getPageNumber():02d}")
        # running header on inner pages
        if canv.getPageNumber() > 1:
            canv.setFont(fonts["label"], 8)
            canv.drawString(doc.leftMargin, H - 0.52 * inch, "BREAKING NEWS")
            canv.drawRightString(W - doc.rightMargin, H - 0.52 * inch, date_line)
            canv.setLineWidth(1.1)
            canv.line(doc.leftMargin, H - 0.60 * inch, W - doc.rightMargin, H - 0.60 * inch)
        canv.restoreState()
    return draw


def _masthead(story, fonts, date_str, vol_no):
    vol_style = ParagraphStyle("Vol", fontName=fonts["label"], fontSize=9.5, leading=11, textColor=INK)
    _double_rule(story, heavy=1.0, light=2.8, heavy_first=True, gap=2.0)
    story.append(Spacer(1, 8))
    story.append(_PosterLine("BREAKING NEWS", fonts["masthead"], max_size=60))
    story.append(Spacer(1, 8))
    _double_rule(story, heavy=2.8, light=1.0, heavy_first=True, gap=2.0)
    story.append(Spacer(1, 5))
    vol_row = Table(
        [[Paragraph(vol_no, vol_style),
          Paragraph("*&nbsp;&nbsp;&nbsp;ULTITRADER IADSS DESK&nbsp;&nbsp;&nbsp;*",
                    ParagraphStyle("VolC", parent=vol_style, alignment=TA_CENTER)),
          Paragraph(date_str, ParagraphStyle("VolR", parent=vol_style, alignment=TA_RIGHT))]],
        colWidths=["30%", "40%", "30%"],
    )
    vol_row.setStyle(TableStyle([
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story.append(vol_row)
    story.append(Spacer(1, 5))
    story.append(_hr(1.2))


def _split_for_photo(paras: list[str], max_paras=2, max_chars=950) -> tuple[list[str], list[str]]:
    """Cap how much text sits beside a photo — the two-column block is a Table
    row, which ReportLab cannot split across pages, so an over-long AI story
    would otherwise crash layout or claim a whole page. Overflow flows
    full-width below; an oversized first paragraph is cut at a sentence end."""
    paras = list(paras)
    if not paras:
        return [], []
    first = paras[0]
    if len(first) > max_chars:
        cut = first.rfind(". ", 200, max_chars)
        if cut != -1:
            return [first[:cut + 1]], [first[cut + 2:]] + paras[1:]
        return [first], paras[1:]
    beside, total = [], 0
    for i, p in enumerate(paras):
        if len(beside) >= max_paras or total + len(p) > max_chars:
            return beside, paras[i:]
        beside.append(p)
        total += len(p)
    return beside, []


def _story_with_photo(paras_flow, photo, width, photo_right=True):
    """Two-column newspaper block: justified text beside a boxed photo."""
    text_w = width * 0.56
    img_w = width - text_w
    if photo_right:
        row = [paras_flow, [photo]]
        widths = [text_w, img_w]
    else:
        row = [[photo], paras_flow]
        widths = [img_w, text_w]
    tbl = Table([row], colWidths=widths)
    tbl.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("LEFTPADDING", (1, 0), (1, 0), 10),
        ("RIGHTPADDING", (0, 0), (0, 0), 10 if photo_right else 0),
    ]))
    return tbl


def build_pdf(newsletter_text: str, factors: dict, date_str: str, path: str,
              headlines: list[dict] | None = None, prev_factors: dict | None = None):
    """Render the daily risk brief as a vintage black-and-white broadsheet PDF."""
    fonts = _register_fonts()
    try:
        d = datetime.date.fromisoformat(date_str)
    except ValueError:
        d = datetime.date.today()
    date_print = d.strftime("%d %B %Y").upper()
    vol_no = f"VOL. {d.year - 2025}, NO. {d.timetuple().tm_yday}"

    doc = SimpleDocTemplate(
        path, pagesize=LETTER,
        topMargin=0.55 * inch, bottomMargin=0.95 * inch,
        leftMargin=0.7 * inch, rightMargin=0.7 * inch,
    )
    width = LETTER[0] - doc.leftMargin - doc.rightMargin

    body_style = ParagraphStyle(
        "NewsBody", fontName="Times-Roman", fontSize=10.6, leading=14.2,
        alignment=TA_JUSTIFY, spaceAfter=9, firstLineIndent=14, textColor=INK,
    )
    deck_style = ParagraphStyle(
        "Deck", fontName=fonts["head"], fontSize=14.5, leading=17.5,
        textColor=INK, spaceAfter=8,
    )
    kicker_style = ParagraphStyle(
        "Kicker", fontName=fonts["label"], fontSize=9.5, leading=11,
        textColor=INK, spaceAfter=4,
    )
    section_head_style = ParagraphStyle(
        "SecHead", fontName=fonts["head"], fontSize=22, leading=24.5,
        textColor=INK, spaceAfter=2,
    )
    why_style = ParagraphStyle(
        "WhyTop", fontName="Times-Italic", fontSize=10, leading=13,
        textColor=INK_SOFT, spaceAfter=8,
    )

    ed = _parse_edition(newsletter_text)
    used_urls: set[str] = set()
    story: list = []

    _masthead(story, fonts, date_print, vol_no)
    story.append(Spacer(1, 12))

    if ed:
        # --- front page: stamped headline, market ticker, top signal ---
        for i, line in enumerate(_split_headline(ed["main_headline"])):
            story.append(_PosterLine(line, fonts["head"], max_size=64 if i == 0 else 48))
            story.append(Spacer(1, 4))
        story.append(Spacer(1, 4))
        _double_rule(story, heavy=2.4, light=0.8, heavy_first=True, gap=2.0)
        story.append(Spacer(1, 10))

        if ed["ticker"]:
            story.append(_ticker_box(ed["ticker"], width, fonts))
            story.append(Spacer(1, 12))

        # Top Signal — the front-page story. Kicker/deck/why-it-leads run full
        # width; only the first text chunk shares the unsplittable two-column
        # table with the photo, so the front page can't be starved.
        top = ed["top"]
        kicker = top["fields"].get("KICKER", "")
        story.append(Paragraph(
            "TOP SIGNAL" + (f"&nbsp;&nbsp;*&nbsp;&nbsp;{escape(kicker.upper())}" if kicker else ""),
            kicker_style))
        if ed.get("deck"):
            story.append(Paragraph(escape(ed["deck"]), deck_style))
        if top["fields"].get("WHY_TOP"):
            story.append(Paragraph(
                f'<font name="{fonts["head"]}" size="8.5">WHY IT LEADS:</font> '
                f'<i>{escape(top["fields"]["WHY_TOP"])}</i>', why_style))
        beside, below = _split_for_photo(top.get("paras", []), max_chars=750)
        top_flow = [Paragraph(escape(p), body_style) for p in beside]
        photo = _image_for_story(headlines, top.get("image_idx"), used_urls) or _any_image(headlines, used_urls)
        if photo:
            img_flow = _photo_flowable(photo[0], width * 0.40 - 18, 2.0 * inch, photo[1], fonts)
            story.append(_story_with_photo(top_flow, img_flow, width, photo_right=True))
        else:
            story.extend(top_flow)
        for p in below:
            story.append(Paragraph(escape(p), body_style))
        card = _desk_card(top, width, fonts)
        if card:
            story.append(Spacer(1, 2))
            story.append(card)

        # --- factor indicator dashboard ---
        story.append(Spacer(1, 14))
        story.append(_barometer(factors, prev_factors, width, fonts))

        # --- market-linked stories, each closed by its desk card ---
        for idx, sec in enumerate(ed["stories"]):
            header: list = [
                Spacer(1, 18), _hr(2.4), Spacer(1, 2), _hr(0.8), Spacer(1, 8),
                Paragraph(f"*&nbsp;&nbsp;MARKET-LINKED STORY {idx + 1}&nbsp;&nbsp;*", kicker_style),
                Spacer(1, 4),
            ]
            if sec.get("headline"):
                header.append(Paragraph(escape(sec["headline"].upper()), section_head_style))
            header.append(Spacer(1, 6))

            photo = _image_for_story(headlines, sec.get("image_idx"), used_urls)
            if photo:
                beside, below = _split_for_photo(sec.get("paras", []))
                img_flow = _photo_flowable(photo[0], width * 0.42 - 18, 2.3 * inch, photo[1], fonts)
                content = [_story_with_photo(
                    [Paragraph(escape(p), body_style) for p in beside],
                    img_flow, width, photo_right=(idx % 2 == 0))]
                content += [Paragraph(escape(p), body_style) for p in below]
            else:
                content = [Paragraph(escape(p), body_style) for p in sec.get("paras", [])]

            # Only the header + first content chunk stay glued — the rest
            # flows freely so long stories don't force page breaks.
            story.append(KeepTogether(header + content[:1]))
            story.extend(content[1:])
            card = _desk_card(sec, width, fonts)
            if card:
                story.append(Spacer(1, 2))
                story.append(card)

        # --- impact board, desk view & scenarios, sources ---
        board = _impact_board(ed["impact"], width, fonts)
        if board:
            story.append(Spacer(1, 18))
            story.append(board)
        dv = _desk_view_box(ed["desk_view"], ed["view_changers"], width, fonts)
        if dv:
            story.append(Spacer(1, 12))
            story.append(dv)
        story.extend(_sources_block(ed, headlines or [], fonts))
    else:
        # --- fallback: unstructured text — still render on newsprint ---
        logger.warning("build_pdf: edition parse failed — using fallback rendering")
        story.append(_barometer(factors, prev_factors, width, fonts))
        story.append(PageBreak())
        _append_plain_body(story, newsletter_text, fonts, body_style)

    decor = _page_decor(fonts, date_print)
    try:
        doc.build(story, onFirstPage=decor, onLaterPages=decor)
    except Exception as e:
        # Last-resort retry: a pathological AI story (e.g. an unsplittable
        # oversized flowable) must never kill the daily job — rebuild the
        # whole document as masthead + barometer + plain flowing text.
        logger.error(f"build_pdf: layout failed ({e}) — retrying with plain rendering")
        doc = SimpleDocTemplate(
            path, pagesize=LETTER,
            topMargin=0.55 * inch, bottomMargin=0.95 * inch,
            leftMargin=0.7 * inch, rightMargin=0.7 * inch,
        )
        story = []
        _masthead(story, fonts, date_print, vol_no)
        story.append(Spacer(1, 12))
        story.append(_barometer(factors, prev_factors, width, fonts))
        story.append(PageBreak())
        plain = re.sub(r"^\s*===\s*(.+?)\s*===\s*$", r"## \1", newsletter_text, flags=re.MULTILINE)
        plain = re.sub(r"^(IMAGE|KICKER|SOURCES)\s*:.*$", "", plain, flags=re.MULTILINE)
        _append_plain_body(story, plain, fonts, body_style)
        doc.build(story, onFirstPage=decor, onLaterPages=decor)
    logger.info(f"news_analyst: built PDF at {path}")


def _append_plain_body(story, text, fonts, body_style):
    """Plain '## heading + prose' rendering — shared by the parse-failure
    fallback and the layout-crash retry."""
    h2_style = ParagraphStyle("H2F", fontName=fonts["head"], fontSize=17, leading=20,
                              textColor=INK, spaceBefore=14, spaceAfter=6)
    for blk in re.split(r"\n(?=##\s)", text.strip()):
        blk = blk.strip()
        if not blk:
            continue
        if blk.startswith("##"):
            lines = blk.split("\n", 1)
            story.append(Paragraph(escape(lines[0].lstrip("#").strip().upper()), h2_style))
            blk = lines[1].strip() if len(lines) > 1 else ""
        for para in blk.split("\n\n"):
            para = para.strip()
            if para:
                story.append(Paragraph(escape(" ".join(para.split())), body_style))


def _clear_old_newsletters(directory: str):
    for old in glob.glob(os.path.join(directory, "newsletter_*.pdf")):
        try:
            os.remove(old)
            logger.info(f"news_analyst: removed old newsletter {old}")
        except OSError as e:
            logger.warning(f"news_analyst: could not remove {old}: {e}")


_job_lock = threading.Lock()


def run_daily_news_job(cfg, alerter=None) -> dict:
    """Full pipeline: fetch -> analyze -> PDF -> factors.json -> alert. Returns the factors dict.

    Guarded by a process-wide lock — the scheduled daily run and a manual
    /admin/run-news-job trigger could otherwise overlap and both hit Groq at
    once, defeating the 65s TPM pacing between calls.
    """
    if not _job_lock.acquire(blocking=False):
        logger.warning("run_daily_news_job: a run is already in progress — skipping this call")
        return {"status": "already_running"}

    try:
        return _run_daily_news_job(cfg, alerter)
    finally:
        _job_lock.release()


def _run_daily_news_job(cfg, alerter=None) -> dict:
    os.makedirs(cfg.NEWSLETTER_DIR, exist_ok=True)
    date_str = datetime.date.today().isoformat()

    if not cfg.GROQ_API_KEY:
        logger.error("run_daily_news_job: GROQ_API_KEY not set — skipping")
        return {}

    headlines = fetch_headlines()
    if not headlines:
        logger.warning("run_daily_news_job: no headlines fetched — proceeding with empty set")

    # Previous issue's factors (for the day-over-day change column in the
    # barometer). Ephemeral storage means this survives day-to-day but not
    # across deploys — fail-open to "new" markers when absent.
    prev_factors = None
    try:
        with open(os.path.join(cfg.NEWSLETTER_DIR, "latest_factors.json")) as f:
            prev_factors = json.load(f)
    except Exception:
        pass

    newsletter_text = generate_newsletter_text(cfg, headlines)
    # Groq's on-demand tier is capped at 12,000 tokens/minute — give the quota a full
    # minute to reset before the second call instead of risking a 429 on the same window.
    time.sleep(65)
    factors = generate_factors(cfg, headlines)

    # Persist factors BEFORE building the PDF — the trading gate in
    # signal_engine.py reads this file, and it must not be lost if the
    # (cosmetic) PDF rendering step fails.
    factors_out = dict(factors)
    factors_out["date"] = date_str
    factors_out["headline_count"] = len(headlines)
    if prev_factors:
        factors_out["previous"] = {k: prev_factors.get(k) for k in FACTOR_KEYS}
    with open(os.path.join(cfg.NEWSLETTER_DIR, "latest_factors.json"), "w") as f:
        json.dump(factors_out, f, indent=2)
    try:
        # Score history — one JSON line per issue. Becomes durable once the
        # service gets a Railway volume; until then it spans deploy-to-deploy.
        with open(os.path.join(cfg.NEWSLETTER_DIR, "factors_history.jsonl"), "a") as f:
            f.write(json.dumps({k: factors_out.get(k) for k in FACTOR_KEYS + ["date", "headline_count"]}) + "\n")
    except OSError as e:
        logger.warning(f"run_daily_news_job: could not append factors history: {e}")

    _clear_old_newsletters(cfg.NEWSLETTER_DIR)
    pdf_path = os.path.join(cfg.NEWSLETTER_DIR, f"newsletter_{date_str}.pdf")
    build_pdf(newsletter_text, factors, date_str, pdf_path, headlines=headlines, prev_factors=prev_factors)

    if alerter:
        def _fmt(k):
            v = factors[k]
            try:
                d = v - int(prev_factors.get(k))
                return f"{v} ({'+' if d > 0 else ''}{d})" if d else f"{v} (unch)"
            except (TypeError, ValueError, AttributeError):
                return str(v)
        alerter.send(
            "📰 <b>Daily Risk Brief ready</b>\n"
            f"Date: {date_str}\n"
            f"Bull: {_fmt('bull_factor')} | Instability: {_fmt('instability_factor')}\n"
            f"Geopolitical risk: {_fmt('geopolitical_risk_factor')} | "
            f"Econ momentum: {_fmt('economic_momentum_factor')} | "
            f"Fed lean: {_fmt('fed_policy_lean')}\n"
            f"({len(headlines)} headlines analyzed) — full PDF on the dashboard."
        )

    logger.info(f"run_daily_news_job: complete for {date_str} — factors={factors_out}")
    return factors_out
