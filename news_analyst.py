"""
Daily AI news analysis — Groq LLM turns aggregated headlines into:
  1. A 4-5 page newsletter PDF (geopolitics, economy, elections, disasters, markets outlook)
  2. Five 1-100 macro factors (bull, instability, geopolitical risk, economic momentum, fed lean)

Two separate Groq calls (long-form + strict JSON) rather than one mixed call —
a malformed JSON block in a single response would otherwise risk losing the
whole newsletter too. The factor call always falls back to neutral defaults on
any parse failure so a bad AI response can never crash the daily job or corrupt
the live trading gate in signal_engine.py.

Groq call pattern reused from recipreneur/adminside/utils.py (GROQ_API_URL,
Bearer auth, OpenAI-compatible chat/completions payload).
"""
import os
import re
import time
import json
import glob
import logging
import datetime
import threading

import requests
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak

from news_fetcher import fetch_headlines

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
MIN_NEWSLETTER_WORDS = 1100  # below this, trigger one expansion pass so the PDF still hits ~4-5 pages


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
    lines = []
    for h in headlines:
        line = f"- [{h['source']}] {h['title']}"
        if h.get("summary"):
            line += f" — {h['summary']}"
        lines.append(line)
    return "\n".join(lines)


def generate_newsletter_text(cfg, headlines: list[dict]) -> str:
    """Long-form newsletter across 5 sections, ~2000-2800 words (renders to ~4-5 PDF pages)."""
    prompt = f"""You are a senior macro/markets analyst writing a daily briefing for an
active trading desk that trades US stocks and crypto. Using ONLY the headlines below
(today's aggregated news), write a newsletter with these five sections, each as a
"## Section Title" heading followed by well-developed prose (not bullet lists):

## Geopolitical & Conflict
## Global Economy & Central Banks
## Politics & Elections
## Disasters & Accidents
## Markets Outlook & Trading Implications

Requirements:
- Each of the five sections should be 250-350 words (total ~1300-1700 words) — enough
  to go beyond a one-line summary and give real context, but not padded filler.
- Be specific — cite the actual events/countries/companies/figures from the headlines,
  don't write generic filler.
- The final section must explicitly connect the day's news to likely near-term impact
  on US equities (especially high-beta names like NVDA, TSLA, COIN, MARA) and crypto
  (BTC, ETH, SOL and majors).
- If a section genuinely has little relevant news today, say so briefly rather than
  padding it.
- Plain prose only, no markdown tables, no JSON.

TODAY'S AGGREGATED HEADLINES:
{_headlines_block(headlines)}
"""
    try:
        text = _call_groq(cfg, [{"role": "user", "content": prompt}], max_tokens=2600)
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
        expand_prompt = f"""The newsletter below is too short ({word_count} words). Rewrite it,
keeping the same five "## Section Title" headings and the same facts, but expand every
section with more depth: causes, historical context, named parties, numbers, and
second-order effects. Target 1300-1700 words total. Output the full rewritten
newsletter (not just additions), same format as before, plain prose only.

CURRENT DRAFT:
{text}
"""
        try:
            expanded = _call_groq(cfg, [{"role": "user", "content": expand_prompt}], max_tokens=2600)
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


def build_pdf(newsletter_text: str, factors: dict, date_str: str, path: str):
    """Render the newsletter + factor summary to a multi-page PDF via ReportLab."""
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("TitleBig", parent=styles["Title"], fontSize=22, spaceAfter=4)
    sub_style   = ParagraphStyle("Sub", parent=styles["Normal"], fontSize=11, textColor=colors.grey, spaceAfter=18)
    h2_style    = ParagraphStyle("H2", parent=styles["Heading2"], spaceBefore=16, spaceAfter=8, textColor=colors.HexColor("#1e3a5f"))
    body_style  = ParagraphStyle("Body", parent=styles["Normal"], fontSize=11.5, leading=18, spaceAfter=13)

    doc = SimpleDocTemplate(
        path, pagesize=LETTER,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch,
    )

    story = []
    story.append(Paragraph("IADSS UltiTrader — Daily Market Intelligence", title_style))
    story.append(Paragraph(f"{date_str}  ·  AI-generated briefing (Groq {os.getenv('GROQ_MODEL', 'llama-3.3-70b-versatile')})", sub_style))

    # Factor summary table
    table_data = [["Factor", "Score (1-100)", "Rationale"]]
    rationale = factors.get("rationale", {})
    for k in FACTOR_KEYS:
        table_data.append([FACTOR_LABELS[k], str(factors.get(k, "—")), rationale.get(k, "")])
    tbl = Table(table_data, colWidths=[2.1 * inch, 0.9 * inch, 3.2 * inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1e3a5f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cccccc")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f6fa")]),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    story.append(tbl)
    story.append(PageBreak())

    # Newsletter body — split on "## Heading" lines into styled sections
    for block in re.split(r"\n(?=##\s)", newsletter_text.strip()):
        block = block.strip()
        if not block:
            continue
        if block.startswith("##"):
            lines = block.split("\n", 1)
            heading = lines[0].lstrip("#").strip()
            body = lines[1].strip() if len(lines) > 1 else ""
            story.append(Paragraph(heading, h2_style))
        else:
            body = block
        for para in body.split("\n\n"):
            para = para.strip()
            if para:
                story.append(Paragraph(para.replace("\n", " "), body_style))

    doc.build(story)
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
    build_pdf(newsletter_text, factors, date_str, pdf_path)

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
