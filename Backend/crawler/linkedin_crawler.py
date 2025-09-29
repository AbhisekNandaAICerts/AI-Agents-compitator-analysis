import os
import time
import json
import re
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Dict, Any, Tuple
import ast

# Selenium imports
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    StaleElementReferenceException, NoSuchElementException, WebDriverException
)

# OpenAI client
from openai import OpenAI

# Pydantic models for validation and structured output
from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

# --- Pydantic Models for LLM Responses ---
class SentimentResponseModel(BaseModel):
    label: str = Field(..., description="one of: positive/neutral/negative")
    score: float = Field(..., ge=0.0, le=1.0, description="normalized confidence score 0..1")
    explanation: Optional[str] = Field(None, description="brief explanation for the label")

class AlertResponseModel(BaseModel):
    title: str = Field(..., description="Title of the alert in 10 words or less")
    message: str = Field(..., description="Detailed message of the alert")
    severity: str = Field(..., description="Severity level of the alert (low|medium|high)")

class LLMAlertRawModel(BaseModel):
    is_alert: bool
    confidence: float
    reason: str
    suggested_title: Optional[str] = None
    suggested_message: Optional[str] = None
    suggested_severity: Optional[str] = None

# --- Helpers (refactored into the module) ---
def _safe_text(el):
    try:
        return el.text.strip()
    except Exception:
        try:
            return el.get_attribute("textContent").strip()
        except Exception:
            return ""

def _find_anchor_href_in_el(el, pattern=None):
    try:
        anchors = el.find_elements(By.CSS_SELECTOR, "a[href]")
    except Exception:
        return None
    for a in anchors:
        try:
            href = a.get_attribute("href") or ""
            if not href or href.startswith("javascript:"):
                continue
            if pattern:
                if re.search(pattern, href):
                    return href
            else:
                if href.startswith("http"):
                    return href
        except StaleElementReferenceException:
            continue
    return None

def _unique_preserve_order(seq):
    seen = set()
    out = []
    for s in seq:
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out

def _get_post_permalink(el, driver, company_base_url=None):
    try:
        data_urn = None
        try:
            data_urn = el.get_attribute("data-urn") or el.get_attribute("data-entity-urn") or None
        except Exception:
            data_urn = None
        if data_urn and "urn:li:activity:" in data_urn:
            return f"https://www.linkedin.com/feed/update/{data_urn}/"

        href = _find_anchor_href_in_el(el, r"/feed/update/urn:li:activity:|/activity/|/posts/|/feed/update/")
        if href:
            if href.startswith("/"):
                return "https://www.linkedin.com" + href
            return href
        
        try:
            time_el = el.find_element(By.CSS_SELECTOR, "time")
            script = ("let node = arguments[0]; "
                      "while(node){ if(node.tagName==='A' && node.href) return node.href; node = node.parentElement; }"
                      "return null;")
            href = driver.execute_script(script, time_el)
            if href:
                return href
        except Exception:
            pass

        href = _find_anchor_href_in_el(el, r"/feed/update/urn:li:activity:|/feed/update/")
        if href:
            return href if href.startswith("http") else "https://www.linkedin.com" + href

        eid = el.get_attribute("id") or ""
        if data_urn:
            return f"{company_base_url or 'https://www.linkedin.com'}/feed/update/{data_urn}/"
        if eid:
            return f"{company_base_url or 'https://www.linkedin.com'}/posts/{eid}"

    except StaleElementReferenceException:
        return None
    except Exception:
        return None
    return None

def _click_while_present(driver, el, selector_css, max_clicks=10, small_wait=0.2):
    clicks = 0
    while clicks < max_clicks:
        try:
            buttons = el.find_elements(By.CSS_SELECTOR, selector_css)
        except Exception:
            break
        if not buttons:
            break
        any_clicked = False
        for b in buttons:
            try:
                if not b.is_displayed():
                    continue
                try:
                    b.click()
                except Exception:
                    try:
                        driver.execute_script("arguments[0].click();", b)
                    except Exception:
                        continue
                any_clicked = True
                clicks += 1
                time.sleep(small_wait)
                if clicks >= max_clicks:
                    break
            except StaleElementReferenceException:
                continue
        if not any_clicked:
            break

def _extract_comment_node_data(comment_el, driver):
    try:
        author = ""
        author_profile = None
        try:
            a = comment_el.find_element(By.CSS_SELECTOR, "a[href*='/in/'], a.comments-comment-meta__description-container, a.comments-comment-meta__image-link")
            author = (a.text or a.get_attribute("textContent") or "").strip()
            author_profile = a.get_attribute("href")
        except Exception:
            try:
                author = _safe_text(comment_el.find_element(By.CSS_SELECTOR, ".comments-comment-meta__description-title"))
            except Exception:
                author = ""

        raw_time = ""
        try:
            t = comment_el.find_element(By.CSS_SELECTOR, "time, .comments-comment-meta__data")
            raw_time = t.get_attribute("datetime") or t.text or ""
        except Exception:
            try:
                spans = comment_el.find_elements(By.CSS_SELECTOR, "span")
                for s in spans:
                    txt = (s.text or "").strip()
                    if re.search(r'\b(ago|h|hour|d|day|mo|month|[0-9]{4}-[0-9]{2}-[0-9]{2})\b', txt, flags=re.I):
                        raw_time = txt
                        break
            except Exception:
                raw_time = ""

        text = ""
        try:
            candidates = [
                ".comments-comment-item__main-content .update-components-text",
                ".comments-comment-item__main-content",
                ".feed-shared-inline-show-more-text .update-components-text",
                ".update-components-text",
                ".feed-shared-text"
            ]
            for sel in candidates:
                try:
                    n = comment_el.find_element(By.CSS_SELECTOR, sel)
                    text = driver.execute_script("return arguments[0].textContent", n) or n.text
                    text = (text or "").strip()
                    if text:
                        break
                except Exception:
                    continue
            if not text:
                text = _safe_text(comment_el)
        except Exception:
            text = _safe_text(comment_el)

        reactions = None
        try:
            rc_btn = comment_el.find_element(By.CSS_SELECTOR, ".comments-comment-social-bar__reactions-count--cr, .comments-comment-social-bar__reactions-count--cr, button[aria-label*='Reactions'], .comments-comment-social-bar__reactions-count--cr")
            txt = (rc_btn.text or rc_btn.get_attribute("textContent") or "").strip()
            m = re.search(r'(\d{1,6})', txt.replace(',', ''))
            if m:
                reactions = m.group(1)
        except Exception:
            reactions = None

        replies = []
        try:
            reply_containers = comment_el.find_elements(By.CSS_SELECTOR, "div.comments-replies-list, div.comments-replies-list__container, div.comments-replies-list, div.comments-replies")
            for rc in reply_containers:
                try:
                    items = rc.find_elements(By.CSS_SELECTOR, "article.comments-comment-entity, article.comments-comment-entity--reply, div.comments-comment-entity")
                    for ri in items:
                        try:
                            rdata = _extract_comment_node_data(ri, driver)
                            if rdata:
                                replies.append(rdata)
                        except Exception:
                            continue
                except Exception:
                    continue
            if not replies:
                items = comment_el.find_elements(By.CSS_SELECTOR, "article.comments-comment-entity--reply, article.comments-comment-entity")
                for ri in items:
                    try:
                        if ri == comment_el:
                            continue
                        rdata = _extract_comment_node_data(ri, driver)
                        if rdata:
                            replies.append(rdata)
                    except Exception:
                        continue
        except Exception:
            replies = []

        return {
            "author": author,
            "author_profile": author_profile,
            "text": text,
            "raw_time": raw_time,
            "replies": replies,
            "reactions": reactions
        }
    except StaleElementReferenceException:
        return None
    except Exception:
        return None

def _collect_all_comments_for_post(el, driver, max_expand_clicks=20, reply_expand_clicks=30, per_post_timeout=25):
    start = time.time()
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
        time.sleep(0.12)
    except Exception:
        pass
    comment_load_selectors = [
        "button.comments-comments-list__load-more-comments-button",
        "button.comments-load-more-button",
        "button[jsaction*='loadMoreComments']",
        "button[aria-label*='more comments']",
        "button[aria-label*='comments']",
        "button.comments-comments-list__load-more-comments-button--cr",
        "button.comments-comments-list__load-more-comments-button"
    ]
    for sel in comment_load_selectors:
        if (time.time() - start) > per_post_timeout:
            break
        _click_while_present(driver, el, sel, max_clicks=max_expand_clicks, small_wait=0.25)
    reply_load_selectors = [
        "button.comment-replies__load-more, button.comments-replies-load-more__button",
        "button[aria-label*='more replies']",
        "button[data-control-name='load_more_replies']",
        "button.comments-comments-list__load-more-comments-button--cr"
    ]
    for sel in reply_load_selectors:
        if (time.time() - start) > per_post_timeout:
            break
        _click_while_present(driver, el, sel, max_clicks=reply_expand_clicks, small_wait=0.2)
    time.sleep(0.3)
    comment_nodes = []
    try:
        selectors = [
            "ul.comments-comments-list > li, div.comments-comment-item, div.comment, li.comments-comment-item, article.comments-comment-entity",
            "div.update-comments-list__comments-container li, li.comment",
            "div.comments-comment-item, li.comments-comment-item"
        ]
        for sel in selectors:
            try:
                found = el.find_elements(By.CSS_SELECTOR, sel)
                if found:
                    comment_nodes = found
                    break
            except Exception:
                continue
    except Exception:
        comment_nodes = []
    comments_data = []
    for cn in comment_nodes:
        if (time.time() - start) > per_post_timeout:
            break
        try:
            data = _extract_comment_node_data(cn, driver)
            if data:
                comments_data.append(data)
        except Exception:
            continue
    return comments_data

def _build_sentiment_prompt(post_text: str, comments: List[Dict[str, Any]]) -> str:
    MAX_CHARS = 12000
    pieces = []
    pieces.append("You are a sentiment analysis assistant. Given the main post text and its comments (including replies), classify the overall post sentiment as one of: positive, neutral, or negative. Provide a numeric confidence score between 0.0 and 1.0 (higher means more confident), and a short explanation (1-2 sentences).")
    pieces.append("\n\nMain post:\n")
    pieces.append(post_text or "(no text)")
    pieces.append("\n\nComments (most relevant first):\n")
    for c in comments:
        author = c.get("author") or c.get("author_profile") or "anon"
        text = (c.get("text") or "").strip()
        pieces.append(f"- {author}: {text}")
        for r in c.get("replies", [])[:3]:
            ra = r.get("author") or r.get("author_profile") or "anon"
            rt = (r.get("text") or "").strip()
            pieces.append(f"   - reply {ra}: {rt}")
    prompt = "\n".join(pieces)
    if len(prompt) > MAX_CHARS:
        prompt = prompt[:MAX_CHARS-200] + "\n\n[TRUNCATED]"
    return prompt

def analyze_post_sentiment(openai_client: OpenAI, post_text: str, comments: List[Dict[str, Any]], model: str = "gpt-4o-mini", max_tokens: int = 512, timeout: int = 60) -> Tuple[Dict[str, Any], Optional[str]]:
    prompt = _build_sentiment_prompt(post_text, comments)
    system_msg = {
        "role": "system",
        "content": "You are a helpful assistant that MUST return a JSON object exactly matching the schema: {\"label\": \"positive|neutral|negative\", \"score\": float(0..1), \"explanation\": \"short explanation\"}. Do not return anything else."
    }
    user_msg = {
        "role": "user",
        "content": (
            "Analyze the sentiment and return only the JSON object.\n\n"
            f"Input:\n{prompt}\n\n"
            "Remember: respond ONLY with valid JSON with keys label, score, explanation."
        )
    }
    try:
        resp = openai_client.chat.completions.create(
            model=model,
            messages=[system_msg, user_msg],
            max_tokens=max_tokens,
            temperature=0.0
        )
        # Fix: access attribute instead of dict key
        out_text = resp.choices[0].message.content
        json_text = out_text.strip()
        json_text = re.sub(r"^```(?:json)?\n", "", json_text)
        json_text = re.sub(r"\n```$", "", json_text)
        m = re.search(r"(\{.*\})", json_text, flags=re.S)
        if m:
            json_text = m.group(1)
        try:
            parsed = json.loads(json_text)
        except Exception:
            try:
                parsed = json.loads(json_text.replace("'", '"'))
            except Exception:
                return ({"label":"neutral","score":0.0,"explanation":"Failed to parse model response: "+json_text[:200]}, "parse_error")
        try:
            model_obj = SentimentResponseModel.model_validate(parsed)
            return (model_obj.model_dump(), None)
        except ValidationError as ve:
            try:
                coerced = {
                    "label": parsed.get("label", "neutral"),
                    "score": float(parsed.get("score", 0.0)) if parsed.get("score") is not None else 0.0,
                    "explanation": parsed.get("explanation", "")
                }
                model_obj = SentimentResponseModel.model_validate(coerced)
                return (model_obj.model_dump(), None)
            except Exception as e2:
                return ({"label":"neutral","score":0.0,"explanation":"Validation failed: "+str(ve)}, "validation_error")
    except Exception as oe:
        return ({"label":"neutral","score":0.0,"explanation":f"OpenAI API error: {str(oe)}"}, "openai_error")
    except Exception as e:
        return ({"label":"neutral","score":0.0,"explanation":f"Unexpected error: {str(e)}"}, "unexpected_error")
        
def _build_alert_prompt(post_text: str, comments: List[Dict[str, Any]], metadata: Optional[Dict[str, Any]] = None, max_chars: int = 12000) -> str:
    pieces = ["""You are a COMPETITOR INTELLIGENCE classification assistant.

Your job
------
Decide whether a LinkedIn post (including its comments/replies) should trigger a COMPETITOR ALERT for the *target company* (use metadata.target_company when available). A COMPETITOR ALERT means the post contains timely, verifiable, or plausible information that may affect the target company's strategy, product competitiveness, hiring, market share, or reputation.


What to look for (signals)
--------------------------
- Product & Service Launches: new products, betas, feature rollouts, rebrands, or explicit product claims.
- Pricing & Promotions: pricing model changes, large discounts, free tiers, or claims like "40% cheaper".
- Hiring & Talent Moves: large hires, new R&D centers, key exec moves, or targeted recruitment campaigns.
- Partnerships, M&A & Funding: alliances, integrations, acquisitions, funding rounds, or investor news.
- Customer Wins & Case Studies: large or strategic customer logos, pilot programs, testimonials vs. competitors.
- Market Expansion: new regions, offices, verticals, or sales channels being announced.
- Technology & IP: patents, published benchmarks, open-source releases, or claims of technical superiority.
- Reputation & PR: negative press, regulatory actions, awards, analyst reports, or public controversies.
- Events & Public Statements: conference keynotes, product demos, or roadmap announcements that indicate change.
- Financial signals: revenue milestones, IPO/funding news, or explicit market share/trajectory claims.

Context & provenance
--------------------
- Prefer official sources (company accounts, press releases). Treat third-party posts as lower-confidence unless independently corroborated.
- Pay attention to who posts (official account vs employee vs unknown) and where the claim appears (post body vs comments).
- Distinguish explicit facts ("we launched X on 2025-09-01") from speculation / opinion ("might disrupt the market").

Output requirements (STRICT)
---------------------------
Return ONLY a single JSON object with EXACT keys and valid JSON (no extra text, no markdown). Schema:

{
  "is_alert": true|false,               // boolean
  "confidence": 0.0-1.0,               // float: model's confidence
  "reason": "short explanation (1-2 sentences)", 
  "suggested_title": "short title (<=10 words)",
  "suggested_message": "actionable message with why it matters and suggested next step",
  "suggested_severity": "low|medium|high"
}

If uncertain, return is_alert=false with confidence 0.0-0.3 and explain the ambiguity in 'reason'.

Important scoring heuristics
----------------------------
- High (>=0.85): clear, specific, and high-impact signals (e.g., confirmed major customer win, large funding, product launch with proof).
- Medium (0.5-0.85): plausible risk signals that need human review (e.g., pilot with big customer, announcement of hiring plans).
- Low (<0.5): weak or irrelevant signals (team events, sponsorships, non-strategic mentions).

Few-shot examples (RESPOND WITH JSON ONLY)
------------------------------------------
### Example 1 — HIGH alert
Input summary: Official post from CompetitorX: "Launching Acme-Compete AI platform next week; beta access available for enterprise customers."
Expected JSON:
{
  "is_alert": true,
  "confidence": 0.92,
  "reason": "Official product launch announcement for an enterprise AI platform signals direct competitive threat to target company.",
  "suggested_title": "CompetitorX launches enterprise AI platform",
  "suggested_message": "CompetitorX announced a new enterprise AI platform with beta access for enterprise customers. This is a direct product-level threat — monitor feature set and top customers; notify product and sales teams.",
  "suggested_severity": "high"
}

### Example 2 — LOW / No alert
Input summary: Team post: "We sponsored a local hackathon — great turnout!"
Expected JSON:
{
  "is_alert": false,
  "confidence": 0.10,
  "reason": "This is a local sponsorship/community event with no competitive product, hiring, or financial signals.",
  "suggested_title": "No competitor risk detected",
  "suggested_message": "Post describes community engagement and sponsorship; not relevant to competitive dynamics. Action: none needed.",
  "suggested_severity": "low"
}

### Example 3 — MEDIUM alert (ambiguous)
Input summary: Employee post: "We're hiring 30 ML engineers for a 'new initiative' — details soon."
Expected JSON:
{
  "is_alert": true,
  "confidence": 0.65,
  "reason": "Significant hiring in ML indicates possible product/R&D expansion but lacks public detail; warrants monitoring.",
  "suggested_title": "Competitor hiring indicates ML expansion",
  "suggested_message": "Competitor announced hiring 30 ML engineers for an unspecified initiative. This suggests an R&D push that could affect talent supply or product roadmap. Action: monitor for job descriptions and public announcements.",
  "suggested_severity": "medium"
}

Final notes
-----------
- Use metadata.target_company to bias detection (treat posts that mention that company or its customers as higher priority).
- If the post contains verifiable links (press release, blog, PR), mention 'source: link' in the suggested_message to help triage.
- Always return strictly valid JSON — no commentary outside the JSON block.
"""]
    if metadata:
        meta_lines = [f"{k}: {v}" for k, v in metadata.items()]
        pieces.append("\n\nMetadata:\n" + "\n".join(meta_lines))
    pieces.append("\n\nMain post:\n")
    pieces.append(post_text or "(no text)")
    pieces.append("\n\nComments and replies (most relevant first):\n")
    comment_count = 0
    for c in comments:
        if comment_count >= 200:
            pieces.append("[...comment list truncated due to length...]")
            break
        author = c.get("author") or c.get("author_profile") or c.get("user") or "anon"
        text = (c.get("text") or c.get("body") or "").strip()
        if not text:
            continue
        pieces.append(f"- {author}: {text}")
        comment_count += 1
        for r in c.get("replies", [])[:2]:
            ra = r.get("author") or r.get("author_profile") or "anon"
            rt = (r.get("text") or r.get("body") or "").strip()
            if rt:
                pieces.append(f"   - reply {ra}: {rt}")
                comment_count += 1
                if comment_count >= 200:
                    break
    pieces.append("\n\nINSTRUCTIONS (IMPORTANT):\n"
                  "1) Decide whether this post should raise a COMPETITOR ALERT for the target company.\n"
                  "2) Respond ONLY with valid JSON matching this exact schema (no extra text):\n"
                  "{\n"
                  '  "is_alert": true|false,\n'
                  '  "confidence": 0.0-1.0,\n'
                  '  "reason": "short explanation (1-2 sentences)",\n'
                  '  "suggested_title": "short alert title (10 words or less)",\n'
                  '  "suggested_message": "detailed alert message",\n'
                  '  "suggested_severity": "low|medium|high"\n'
                  "}\n"
                  "3) If uncertain, choose is_alert=false with a low confidence (0.0-0.3) and explain why.\n"
                  "4) Keep title concise and message actionable (what happened, why it matters, recommended next step).\n"
                  "5) Do NOT include any text outside the JSON object.")
    prompt = "\n".join(pieces)
    if len(prompt) > max_chars:
        prompt = prompt[: max_chars - 200] + "\n\n[TRUNCATED]"
    return prompt

def _extract_balanced_json(s: str) -> Optional[str]:
    if not s:
        return None
    start = s.find("{")
    if start == -1:
        return None
    in_str = False
    escape = False
    quote_char = None
    depth = 0
    for i in range(start, len(s)):
        ch = s[i]
        if escape:
            escape = False
            continue
        if ch == "\\":
            escape = True
            continue
        if in_str:
            if ch == quote_char:
                in_str = False
            continue
        else:
            if ch == '"' or ch == "'":
                in_str = True
                quote_char = ch
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return s[start:i+1]
    return None

def _extract_json_from_text(out_text: str) -> Optional[str]:
    if out_text is None:
        return None
    jt = out_text.strip()
    jt = re.sub(r"^```(?:json)?\s*", "", jt, flags=re.I)
    jt = re.sub(r"\s*```$", "", jt, flags=re.I)
    candidate = _extract_balanced_json(jt) or jt
    try:
        json.loads(candidate)
        return candidate
    except Exception:
        pass
    try:
        cand2 = bytes(candidate, "utf-8").decode("unicode_escape")
    except Exception:
        cand2 = candidate
    try:
        json.loads(cand2)
        return cand2
    except Exception:
        pass
    cand3 = cand2
    cand3 = re.sub(r"(?P<prefix>(?:\{|,)\s*)'(?P<key>[^']+?)'\s*:", r'\g<prefix>"\g<key>":', cand3)
    cand3 = re.sub(r":\s*'(?P<val>[^']*?)'(?P<post>\s*(?:,|\}))", r': "\g<val>"\g<post>', cand3)
    cand3 = re.sub(r",\s*([}\]])", r"\1", cand3)
    try:
        json.loads(cand3)
        return cand3
    except Exception:
        pass
    try:
        pyobj = ast.literal_eval(candidate)
        return json.dumps(pyobj)
    except Exception:
        pass
    return None

def analyze_post_alert(openai_client, post_text: str, comments: List[Dict[str, Any]], metadata: Optional[Dict[str, Any]] = None, model: str = "gpt-4o-mini", max_tokens: int = 512, temperature: float = 0.0, timeout: int = 60) -> Tuple[Dict[str, Any], Optional[str]]:
    prompt = _build_alert_prompt(post_text=post_text, comments=comments, metadata=metadata)
    system_msg = {"role": "system", "content": ("You are a strict JSON-output assistant. Produce ONLY a single JSON object that exactly matches " "the instructed schema. Do not add commentary outside the JSON.")}
    user_msg = {"role": "user", "content": prompt}
    try:
        resp = openai_client.chat.completions.create(
            model=model,
            messages=[system_msg, user_msg],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        # Fix: access attribute instead of dict key
        out_text = resp.choices[0].message.content
        raw_json_text = _extract_json_from_text(out_text)
        if not raw_json_text:
            return ({"title": "No alert (parse failure)", "message": "LLM response could not be parsed. Raw output: " + (out_text or "")[:400], "severity": "low",}, "parse_error")
        try:
            parsed = json.loads(raw_json_text)
        except Exception:
            try:
                parsed = json.loads(raw_json_text.replace("'", '"'))
            except Exception as e_parse:
                return ({"title": "No alert (parse failure)", "message": "LLM response could not be parsed. Raw output: " + (out_text or "")[:400], "severity": "low",}, "parse_error")
        try:
            llm_raw = LLMAlertRawModel.model_validate(parsed)
        except ValidationError:
            coerced = {"is_alert": bool(parsed.get("is_alert") or parsed.get("alert") or parsed.get("should_alert") or False),
                       "confidence": float(parsed.get("confidence") or parsed.get("score") or 0.0),
                       "reason": str(parsed.get("reason") or parsed.get("explanation") or parsed.get("why") or "")[:1000],
                       "suggested_title": parsed.get("suggested_title") or parsed.get("title") or None,
                       "suggested_message": parsed.get("suggested_message") or parsed.get("message") or None,
                       "suggested_severity": (parsed.get("suggested_severity") or parsed.get("severity") or None),}
            try:
                llm_raw = LLMAlertRawModel.model_validate(coerced)
            except ValidationError as e2:
                return ({"title": "No alert (validation failure)", "message": "LLM returned unexpected schema. Raw parsed JSON: " + json.dumps(parsed)[:400], "severity": "low",}, "validation_error",)
        sev = (llm_raw.suggested_severity or "").lower() if llm_raw.suggested_severity else None
        if sev not in ("low", "medium", "high"):
            if not llm_raw.is_alert:
                sev = "low"
            else:
                if llm_raw.confidence >= 0.85:
                    sev = "high"
                elif llm_raw.confidence >= 0.5:
                    sev = "medium"
                else:
                    sev = "low"
        title = (llm_raw.suggested_title or "").strip()
        if not title:
            reason_snippet = (llm_raw.reason or "").split(".")[0][:80]
            title = ("Competitor alert: " + reason_snippet).strip()
        message = (llm_raw.suggested_message or "").strip()
        if not message:
            message = (f"LLM reason: {llm_raw.reason.strip()}\n" f"Confidence: {llm_raw.confidence:.2f}\n" "Action: Review the post and decide whether to escalate.")
        alert_obj = AlertResponseModel(title=title, message=message, severity=sev)
        return (alert_obj.model_dump(), None)
    except Exception as oe:
        return ({"title": "No alert (API error)", "message": f"OpenAI API error: {str(oe)}", "severity": "low",}, "openai_error",)
    
def _parse_posted_at(raw_time: Optional[str]) -> str:
    if not raw_time:
        return datetime.now(timezone.utc).isoformat()
    s = raw_time.strip()
    iso_match = re.search(r"\d{4}-\d{2}-\d{2}", s)
    if iso_match:
        try:
            return datetime.fromisoformat(iso_match.group(0)).replace(tzinfo=timezone.utc).isoformat()
        except Exception:
            pass
    rel_match = re.search(r"(\d+)\s*(d|day|days|h|hour|hours|m|minute|minutes)\b", s, flags=re.I)
    if rel_match:
        qty = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        now = datetime.now(timezone.utc)
        if unit.startswith("d"):
            dt = now - timedelta(days=qty)
        elif unit.startswith("h"):
            dt = now - timedelta(hours=qty)
        elif unit.startswith("m"):
            dt = now - timedelta(minutes=qty)
        else:
            dt = now
        return dt.isoformat()
    return datetime.now(timezone.utc).isoformat()

def process_linkedin_data(data) -> List[Dict[str, Any]]:
    processed_posts: List[Dict[str, Any]] = []
    company_id_counter = 1
    for post in data:
        uid = post.get('uid') or ""
        if not isinstance(uid, str) or not uid.startswith('urn:li:activity:'):
            continue
        social_counts = post.get('social_counts') or {}
        post_sentiment = post.get('post_sentiment') or {}
        comments_summary = post.get('comments_sentiment_summary') or {}
        post_alert = post.get('post_alert') or {}
        alert_title = post_alert.get('title')
        alert_message = post_alert.get('message')
        alert_severity = post_alert.get('severity')
        alert_confidence = post_alert.get('confidence') or post_alert.get('score') or post_alert.get('confidence_score')
        processed_post: Dict[str, Any] = {
            "uid": uid,
            "company_id": company_id_counter,
            "post_platform": "linkedin",
            "post_url": post.get('post_url'),
            "post_description": post.get('text'),
            "posted_at": _parse_posted_at(post.get('raw_time') or post.get('raw_time_text') or ""),
            "likes": social_counts.get('reactions_count') if social_counts.get('reactions_count') is not None else 0,
            "comments_count": social_counts.get('comments_count') if social_counts.get('comments_count') is not None else 0,
            "shares": social_counts.get('reposts_count') if social_counts.get('reposts_count') is not None else 0,
            "sentiment_label": post_sentiment.get('label'),
            "sentiment_score": post_sentiment.get('score'),
            "sentiment_explanation": post_sentiment.get('explanation'),
            "comments_summary": {
                "total_comments": comments_summary.get('total_comments'),
                "positive": comments_summary.get('positive'),
                "neutral": comments_summary.get('neutral'),
                "negative": comments_summary.get('negative'),
                "average_score": comments_summary.get('average_score'),
                "error": comments_summary.get('error', "")
            },
            "alert": {
                "title": alert_title or None,
                "message": alert_message or None,
                "severity": alert_severity or None,
                "confidence": float(alert_confidence) if alert_confidence is not None else None
            }
        }
        processed_posts.append(processed_post)
    return processed_posts

def scrape_posts_with_comments(driver: WebDriver,
                               openai_api_key: Optional[str] = None,
                               post_selector: str = "div[data-urn^='urn:li:activity']",
                               scroll_times: int = 5,
                               scroll_pause: float = 0.5,
                               days: int = 7,
                               max_posts: Optional[int] = None,
                               company_base_url: Optional[str] = None,
                               openai_model: str = "gpt-4o-mini"):
    cutoff = datetime.now() - timedelta(days=days)
    results = []
    seen = set()
    driver.get(company_base_url)

    if openai_api_key is None:
        raise RuntimeError("OpenAI API key not found.")
    openai_client = OpenAI(api_key=openai_api_key)

    for _ in range(scroll_times):
        try:
            driver.execute_script("window.scrollBy(0, Math.max(document.documentElement.clientHeight, 800));")
            time.sleep(scroll_pause)
        except Exception:
            break

    try:
        post_elems = driver.find_elements(By.CSS_SELECTOR, post_selector)
    except Exception:
        post_elems = []

    for el in post_elems:
        if max_posts and len(results) >= max_posts:
            break
        try:
            uid = (el.get_attribute("data-urn") or el.get_attribute("id") or el.get_attribute("data-entity-urn") or "") or (_safe_text(el)[:200])
            if not uid or uid in seen:
                continue
            seen.add(uid)
            permalink = _get_post_permalink(el, driver, company_base_url=company_base_url)
            if not permalink:
                continue
            text = ""
            try:
                txt_el = el.find_element(By.CSS_SELECTOR, "div.update-components-text, div.update-components-update-v2__commentary, span.break-words, div.feed-shared-text")
                text = driver.execute_script("return arguments[0].textContent", txt_el) or txt_el.text or ""
                text = (text or "").strip()
            except Exception:
                text = ""
            raw_time = ""
            try:
                time_el = el.find_element(By.CSS_SELECTOR, "time")
                raw_time = time_el.get_attribute("datetime") or time_el.text or ""
            except Exception:
                pass
            social_counts = {"reactions_count": None, "comments_count": None, "reposts_count": None, "reaction_types": []}
            try:
                soc = el.find_element(By.CSS_SELECTOR, "div.social-details-social-counts")
                try:
                    btn = soc.find_element(By.CSS_SELECTOR, "button[data-reaction-details]")
                    rc_txt = btn.text.strip()
                    m = re.search(r'([\d,]+)', rc_txt)
                    if m:
                        social_counts["reactions_count"] = int(m.group(1).replace(",", ""))
                except Exception:
                    pass
                try:
                    cbtn = soc.find_element(By.CSS_SELECTOR, "li.social-details-social-counts__comments button")
                    c_txt = cbtn.text.strip()
                    m = re.search(r'([\d,]+)', c_txt)
                    if m:
                        social_counts["comments_count"] = int(m.group(1).replace(",", ""))
                except Exception:
                    pass
                try:
                    rbtn = soc.find_element(By.CSS_SELECTOR, "li.social-details-social-counts__item button[aria-label*='repost']")
                    r_txt = rbtn.text.strip()
                    m = re.search(r'([\d,]+)', r_txt)
                    if m:
                        social_counts["reposts_count"] = int(m.group(1).replace(",", ""))
                except Exception:
                    pass
            except Exception:
                pass

            comments_thread = _collect_all_comments_for_post(el, driver) if social_counts.get("comments_count", 0) > 0 else []
            
            post_obj = {
                "uid": uid,
                "permalink": permalink,
                "post_url": permalink,
                "text": text,
                "raw_time": raw_time,
                "social_counts": social_counts,
                "comments_thread": comments_thread,
            }
            
            flattened_comments = []
            def _flatten_comments_for_llm(ct):
                out = []
                for c in ct:
                    item = {"author": c.get("author"), "text": c.get("text")}
                    out.append(item)
                    if c.get("replies"):
                        for r in c.get("replies", [])[:3]:
                            out.append({"author": r.get("author"), "text": r.get("text")})
                return out
            
            flattened_comments = _flatten_comments_for_llm(comments_thread)
            
            sentiment_result, _ = analyze_post_sentiment(openai_client, text, flattened_comments, model=openai_model)
            alert_result, _ = analyze_post_alert(openai_client, text, flattened_comments, model=openai_model)
            
            post_obj["post_alert"] = alert_result
            post_obj["post_sentiment"] = sentiment_result
            
            try:
                total_comments = social_counts.get("comments_count") or len(comments_thread)
                post_obj["comments_sentiment_summary"] = {
                    "total_comments": total_comments,
                    "positive": 0, "neutral": total_comments, "negative": 0,
                    "average_score": sentiment_result.get("score", 0.0),
                    "error": None
                }
            except Exception:
                post_obj["comments_sentiment_summary"] = {"total_comments": 0, "positive": 0, "neutral": 0, "negative": 0, "average_score": 0.0, "error": "summary_error"}
            
            results.append(post_obj)

        except StaleElementReferenceException:
            continue
        except Exception:
            continue

    processed_data = process_linkedin_data(results)
    return processed_data