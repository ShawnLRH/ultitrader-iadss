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
MIN_NEWSLETTER_WORDS = 850  # below this, trigger one expansion pass so the paper isn't thin


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


SECTION_NAMES = [
    "GEOPOLITICAL & CONFLICT",
    "GLOBAL ECONOMY & CENTRAL BANKS",
    "POLITICS & ELECTIONS",
    "DISASTERS & ACCIDENTS",
    "MARKETS OUTLOOK & TRADING IMPLICATIONS",
]

_EDITION_FORMAT = """===MAIN_HEADLINE===
<3 to 6 words, ALL CAPS, punchy front-page newspaper headline for the day's dominant theme — may end with !!>
===DECK===
<one bold sentence, 12-22 words, expanding on the main headline>
===LEAD===
KICKER: <a 3-6 word section label for the lead story, e.g. "MARKETS ON EDGE">
IMAGE: <number of the single most front-page-worthy headline, prefer ones tagged [PHOTO]>
<two paragraphs, 70-100 words each — the front-page lead story tying together the day's most market-moving news>
""" + "".join(f"""===SECTION: {name}===
HEADLINE: <punchy newspaper headline for this section, 4-9 words>
IMAGE: <number of the headline whose photo best fits this section, prefer [PHOTO], or NONE>
QUOTE: <one striking pull-quote sentence capturing this section, 8-18 words, no quotation marks>
<three paragraphs, 55-85 words each>
""" for name in SECTION_NAMES)


def generate_newsletter_text(cfg, headlines: list[dict]) -> str:
    """Structured newspaper 'edition' text — parsed by _parse_edition for the vintage PDF."""
    prompt = f"""You are the editor of a vintage-style daily financial broadsheet read by an
active trading desk that trades US stocks and crypto. Using ONLY the numbered headlines
below (today's aggregated news), write today's edition.

OUTPUT FORMAT — copy the ===MARKER=== lines EXACTLY as shown, fill in the content.
No markdown, no JSON, no extra commentary before or after:

{_EDITION_FORMAT}
Requirements:
- Be specific — cite the actual events, countries, companies and figures from the
  headlines. Never write generic filler.
- Short, punchy, readable paragraphs — this is a newspaper, not an essay.
- Headlines must read like real newspaper headlines: active voice, present tense.
- The MARKETS OUTLOOK section must explicitly connect the day's news to likely
  near-term impact on US equities (especially high-beta names like NVDA, TSLA, COIN,
  MARA) and crypto (BTC, ETH, SOL and majors).
- If a section genuinely has little relevant news today, say so briefly in one
  paragraph rather than padding it.
- IMAGE numbers must reference the numbered headline list below.

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
fields and the same facts, but expand every section's paragraphs with more depth:
causes, context, named parties, numbers, and second-order effects. Target 1100-1500
words total. Output the full rewritten edition in the same format, nothing else.

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
    "bull_factor": "<one sentence>",
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
        return result

    result = {}
    for k in FACTOR_KEYS:
        try:
            v = int(round(float(parsed.get(k, 50))))
        except (TypeError, ValueError):
            v = 50
        result[k] = max(1, min(100, v))
    result["rationale"] = parsed.get("rationale", {}) if isinstance(parsed.get("rationale"), dict) else {}
    return result


# ---------------------------------------------------------------------------
# Edition parsing — the ===MARKER=== plain-text format from generate_newsletter_text
# ---------------------------------------------------------------------------

def _parse_block(body: str) -> dict:
    """One LEAD/SECTION body → {kicker, headline, image_idx, quote, paras}."""
    out = {"kicker": "", "headline": "", "image_idx": None, "quote": "", "paras": []}
    plain_lines = []
    for line in body.split("\n"):
        stripped = line.strip()
        m = re.match(r"^(KICKER|HEADLINE|IMAGE|QUOTE)\s*:\s*(.*)$", stripped, re.IGNORECASE)
        if m:
            key, val = m.group(1).upper(), m.group(2).strip().strip('"“”')
            if key == "IMAGE":
                num = re.search(r"\d+", val)
                out["image_idx"] = int(num.group(0)) if num else None
            elif key == "KICKER":
                out["kicker"] = val
            elif key == "HEADLINE":
                out["headline"] = val
            elif key == "QUOTE":
                out["quote"] = val
        else:
            plain_lines.append(line)
    rest = "\n".join(plain_lines).strip()
    out["paras"] = [" ".join(p.split()) for p in re.split(r"\n\s*\n", rest) if p.strip()]
    return out


def _parse_edition(text: str) -> dict | None:
    """Parse the ===MARKER=== edition format. Returns None if it doesn't look
    structured (renderer then falls back to plain rendering of the raw text)."""
    parts = re.split(r"^\s*===\s*(.+?)\s*===\s*$", text, flags=re.MULTILINE)
    if len(parts) < 5:
        return None
    ed = {"main_headline": "", "deck": "", "lead": None, "sections": []}
    for marker, body in zip(parts[1::2], parts[2::2]):
        marker = marker.strip().upper()
        body = body.strip()
        if marker == "MAIN_HEADLINE":
            ed["main_headline"] = " ".join(body.split()).upper()
        elif marker == "DECK":
            ed["deck"] = " ".join(body.split())
        elif marker == "LEAD":
            ed["lead"] = _parse_block(body)
        elif marker.startswith("SECTION"):
            name = marker.split(":", 1)[1].strip() if ":" in marker else marker
            blk = _parse_block(body)
            blk["name"] = name
            ed["sections"].append(blk)
    if not ed["main_headline"] or len(ed["sections"]) < 3:
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

# Section name keyword -> which factor gets stamped next to that section's headline.
_SECTION_FACTOR = [
    ("GEOPOLITICAL", "geopolitical_risk_factor"),
    ("ECONOMY", "economic_momentum_factor"),
    ("POLITICS", "instability_factor"),
    ("DISASTER", "instability_factor"),
    ("MARKETS", "bull_factor"),
]


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


def _barometer(factors: dict, width, fonts) -> KeepTogether:
    """Front-page 'THE DESK BAROMETER' box — the five macro factors as vintage
    ink gauges with verdict stamps and one-line rationales."""
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
    rows, styles_extra = [], []
    rows.append([[Paragraph("THE DESK BAROMETER", title_style),
                  Paragraph("TODAY'S AI MACRO FACTORS  *  SCORED 1-100 FROM THE MORNING WIRE", sub_title_style)]])
    styles_extra.append(("BACKGROUND", (0, 0), (-1, 0), INK))

    for k in FACTOR_KEYS:
        value = max(1, min(100, int(factors.get(k, 50))))
        name = FACTOR_PRINT[k][0]
        header = Table(
            [[Paragraph(name, label_style),
              Paragraph(f"{value} / 100 — {_verdict(k, value)}", verdict_style)]],
            colWidths=[inner_w * 0.55, inner_w * 0.45],
        )
        header.setStyle(TableStyle([
            ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
            ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ]))
        block = [header, _factor_bar(value, inner_w, fonts), Spacer(1, 2)]
        note = rationale.get(k, "")
        if note:
            block.append(Paragraph(escape(note), rationale_style))
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


def _factor_stamp(key: str, factors: dict, fonts) -> Table:
    """Small boxed index chip stamped beside a section headline, e.g.
    | GEOPOLITICAL RISK  90/100 — HIGH ALERT |"""
    value = max(1, min(100, int(factors.get(key, 50))))
    style = ParagraphStyle("Stamp", fontName=fonts["head"], fontSize=8.2, leading=10,
                           textColor=INK, alignment=TA_CENTER)
    chip = Table([[Paragraph(f"{FACTOR_PRINT[key][0]}: {value}/100 — {_verdict(key, value)}", style)]])
    chip.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.2, INK),
        ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
    ]))
    return chip


def _section_factor_key(section_name: str) -> str | None:
    up = section_name.upper()
    for keyword, key in _SECTION_FACTOR:
        if keyword in up:
            return key
    return None


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


def build_pdf(newsletter_text: str, factors: dict, date_str: str, path: str, headlines: list[dict] | None = None):
    """Render the daily edition as a vintage black-and-white broadsheet PDF."""
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
    quote_style = ParagraphStyle(
        "PullQuote", fontName="Times-BoldItalic", fontSize=13.5, leading=17,
        alignment=TA_CENTER, textColor=INK, spaceBefore=2, spaceAfter=2,
    )

    ed = _parse_edition(newsletter_text)
    used_urls: set[str] = set()
    story: list = []

    _masthead(story, fonts, date_print, vol_no)
    story.append(Spacer(1, 12))

    if ed:
        # --- front page: stamped headline, deck, lead story + photo ---
        for i, line in enumerate(_split_headline(ed["main_headline"])):
            story.append(_PosterLine(line, fonts["head"], max_size=86 if i == 0 else 62))
            story.append(Spacer(1, 4))
        story.append(Spacer(1, 4))
        _double_rule(story, heavy=2.4, light=0.8, heavy_first=True, gap=2.0)
        story.append(Spacer(1, 12))

        lead = ed["lead"] or {"kicker": "", "paras": [], "image_idx": None, "quote": ""}
        lead_flow = []
        if lead.get("kicker"):
            lead_flow.append(Paragraph(f"*&nbsp;&nbsp;{escape(lead['kicker'].upper())}", kicker_style))
        if ed.get("deck"):
            lead_flow.append(Paragraph(escape(ed["deck"]), deck_style))
        for p in lead.get("paras", []):
            lead_flow.append(Paragraph(escape(p), body_style))

        photo = _image_for_story(headlines, lead.get("image_idx"), used_urls) or _any_image(headlines, used_urls)
        if photo:
            img_flow = _photo_flowable(photo[0], width * 0.44 - 18, 3.1 * inch, photo[1], fonts)
            story.append(_story_with_photo(lead_flow, img_flow, width, photo_right=True))
        else:
            story.extend(lead_flow)

        story.append(Spacer(1, 14))
        story.append(_barometer(factors, width, fonts))

        # --- sections flow on after the barometer, separated by double rules ---
        for idx, sec in enumerate(ed["sections"]):
            block: list = [Spacer(1, 18)]
            block.append(_hr(2.4))
            block.append(Spacer(1, 2))
            block.append(_hr(0.8))
            block.append(Spacer(1, 8))

            label = Paragraph(f"*&nbsp;&nbsp;{escape(sec['name'])}&nbsp;&nbsp;*", kicker_style)
            fkey = _section_factor_key(sec["name"])
            if fkey:
                head_row = Table([[label, _factor_stamp(fkey, factors, fonts)]],
                                 colWidths=[width * 0.55, width * 0.45])
                head_row.setStyle(TableStyle([
                    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                    ("ALIGN", (1, 0), (1, 0), "RIGHT"),
                    ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ]))
                block.append(head_row)
            else:
                block.append(label)
            block.append(Spacer(1, 4))
            if sec.get("headline"):
                block.append(Paragraph(escape(sec["headline"].upper()), section_head_style))
            block.append(Spacer(1, 6))

            paras = [Paragraph(escape(p), body_style) for p in sec.get("paras", [])]
            photo = _image_for_story(headlines, sec.get("image_idx"), used_urls)
            first_chunk = paras[:2] if photo else paras

            if photo:
                img_flow = _photo_flowable(photo[0], width * 0.42 - 18, 2.6 * inch, photo[1], fonts)
                block.append(_story_with_photo(first_chunk, img_flow, width, photo_right=(idx % 2 == 0)))
                rest = paras[2:]
            else:
                block.extend(first_chunk)
                rest = []

            if sec.get("quote"):
                block.append(Spacer(1, 6))
                quote_tbl = Table(
                    [[[_hr(1.0), Spacer(1, 5),
                       Paragraph(f"&#8220;{escape(sec['quote'])}&#8221;", quote_style),
                       Spacer(1, 5), _hr(1.0)]]],
                    colWidths=[width * 0.8],
                )
                quote_tbl.setStyle(TableStyle([
                    ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                    ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                ]))
                block.append(quote_tbl)
                block.append(Spacer(1, 8))

            story.append(KeepTogether(block))
            story.extend(rest)
    else:
        # --- fallback: unstructured text — still render on newsprint ---
        logger.warning("build_pdf: edition parse failed — using fallback rendering")
        story.append(_barometer(factors, width, fonts))
        story.append(PageBreak())
        h2_style = ParagraphStyle("H2F", fontName=fonts["head"], fontSize=17, leading=20,
                                  textColor=INK, spaceBefore=14, spaceAfter=6)
        for blk in re.split(r"\n(?=##\s)", newsletter_text.strip()):
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

    decor = _page_decor(fonts, date_print)
    doc.build(story, onFirstPage=decor, onLaterPages=decor)
    logger.info(f"news_analyst: built PDF at {path}")


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

    newsletter_text = generate_newsletter_text(cfg, headlines)
    # Groq's on-demand tier is capped at 12,000 tokens/minute — give the quota a full
    # minute to reset before the second call instead of risking a 429 on the same window.
    time.sleep(65)
    factors = generate_factors(cfg, headlines)

    _clear_old_newsletters(cfg.NEWSLETTER_DIR)
    pdf_path = os.path.join(cfg.NEWSLETTER_DIR, f"newsletter_{date_str}.pdf")
    build_pdf(newsletter_text, factors, date_str, pdf_path, headlines=headlines)

    factors_out = dict(factors)
    factors_out["date"] = date_str
    factors_out["headline_count"] = len(headlines)
    with open(os.path.join(cfg.NEWSLETTER_DIR, "latest_factors.json"), "w") as f:
        json.dump(factors_out, f, indent=2)

    if alerter:
        alerter.send(
            "📰 <b>Daily Market Intelligence ready</b>\n"
            f"Date: {date_str}\n"
            f"Bull: {factors['bull_factor']} | Instability: {factors['instability_factor']}\n"
            f"Geopolitical risk: {factors['geopolitical_risk_factor']} | "
            f"Econ momentum: {factors['economic_momentum_factor']} | "
            f"Fed lean: {factors['fed_policy_lean']}\n"
            f"({len(headlines)} headlines analyzed) — full PDF on the dashboard."
        )

    logger.info(f"run_daily_news_job: complete for {date_str} — factors={factors_out}")
    return factors_out
