# routers/crawler.py
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional, Tuple
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from apify_client import ApifyClient
from openai import OpenAI
from pydantic import BaseModel, Field

from dotenv import load_dotenv

load_dotenv()

from core.database import SessionLocal
from models import SocialMediaPost, Company, Alert, CrawlerLog, Hashtag
from schemas import CrawlResponse
import json

# --- Router Setup ---
router = APIRouter(prefix="/crawler", tags=["crawler"])
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# --- Pydantic Models for AI Responses ---
class SentimentResponseModel(BaseModel):
    label: str = Field(..., description="one of: positive/neutral/negative")
    score: float = Field(
        ..., ge=0.0, le=1.0, description="normalized confidence score 0..1"
    )
    explanation: Optional[str] = Field(
        None, description="brief explanation for the label"
    )


class AlertResponseModel(BaseModel):
    title: str = Field(..., description="Title of the alert in 10 words or less")
    message: str = Field(..., description="Detailed message of the alert")
    severity: str = Field(
        ..., description="Severity level of the alert (low|medium|high)"
    )


# --- Helper Functions ---
def _parse_posted_at(raw_time: Optional[str]) -> datetime:
    """
    Parses various date string formats and returns a timezone-aware datetime object.
    """
    if not raw_time:
        return datetime.now(timezone.utc)
    s = str(raw_time).strip()
    try:
        s_cleaned = s.replace("Z", "+00:00")
        if "." in s_cleaned:
            parts = s_cleaned.split(".")
            microseconds = parts[1].split("+")[0]
            if len(microseconds) > 6:
                s_cleaned = f"{parts[0]}.{microseconds[:6]}+{parts[1].split('+')[1]}"
        return datetime.fromisoformat(s_cleaned)
    except (ValueError, TypeError, IndexError):
        pass
    rel_match = re.search(
        r"(\d+)\s*(d|day|days|h|hour|hours|m|minute|minutes)\b", s, flags=re.I
    )
    if rel_match:
        qty = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        now = datetime.now(timezone.utc)
        if unit.startswith("d"):
            return now - timedelta(days=qty)
        if unit.startswith("h"):
            return now - timedelta(hours=qty)
        return now - timedelta(minutes=qty)
    logger.warning(f"Could not parse date string: '{s}'. Defaulting to now().")
    return datetime.now(timezone.utc)


def analyze_post_sentiment(
    openai_client: OpenAI, post_text: str
) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Analyzes the sentiment of a post using the OpenAI API.
    Only the post_text is used for sentiment analysis.
    """
    try:
        prompt = (
            "Analyze the sentiment of the following LinkedIn post. "
            "Respond ONLY with a valid JSON object containing: label (positive/neutral/negative), "
            "score (0..1), and a brief explanation.\n\n"
            f"Post: \"{post_text}\""
        )
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        result = json.loads(content)
        return result, None
    except Exception as e:
        return (
            {"label": "neutral", "score": 0.5, "explanation": "AI analysis failed."},
            str(e),
        )


def analyze_post_alert(
    openai_client: OpenAI, post_text: str
) -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Analyzes a post for potential competitive alerts using the OpenAI API.
    Uses a prompt to determine if this is important news for competitors.
    """
    try:
        prompt = (
            "You are an expert in competitive intelligence. "
            "Given the following LinkedIn post, determine if it contains important news or updates that competitors should be aware of. "
            "Respond with a JSON object containing: title (10 words or less), message (detailed explanation) must be less than 15 words, and severity (low|medium|high).\n\n"
            f"Post: \"{post_text}\""
        )
        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content
        try:
            result = json.loads(content)
            return result, None
        except Exception as json_err:
            return (
                {"title": "No Alert", "message": "AI response was not valid JSON.", "severity": "low"},
                str(json_err),
            )
    except Exception as e:
        return (
            {"title": "No Alert", "message": "AI analysis failed.", "severity": "low"},
            str(e),
        )


# --- Database Dependency ---
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# --- Main API Endpoint ---
@router.post("/crawl/linkedin/{company_id}", response_model=CrawlResponse)
def crawl_linkedin_by_company(
    company_id: int, db: Session = Depends(get_db), max_posts: int = 25
):
    """
    Endpoint to trigger the LinkedIn crawler for a specific company using Apify.
    """
    log = CrawlerLog(company_id=company_id)
    db.add(log)
    db.commit()
    db.refresh(log)
    log_id = log.log_id

    try:
        company = db.query(Company).filter(Company.company_id == company_id).first()
        if not company:
            raise HTTPException(
                status_code=404, detail=f"Company id={company_id} not found"
            )

        apify_token = os.getenv("APIFY_API_TOKEN")
        openai_key = os.getenv("OPENAI_API_KEY")
        if not apify_token or not openai_key:
            raise RuntimeError(
                "Required environment variables (APIFY_API_TOKEN, OPENAI_API_KEY) are not set."
            )

        apify_client = ApifyClient(apify_token)
        openai_client = OpenAI(api_key=openai_key)

        logger.info(f"Starting Apify actor for {company.company_name}")
        actor_run_input = {
            "company_name": company.company_name.lower().replace(" ", "-"),
            "page_number": 1,
            "limit": max_posts,
            "sort": "recent",
        }

        actor = apify_client.actor("apimaestro/linkedin-company-posts")
        run = actor.call(run_input=actor_run_input)

        scraped_items = list(apify_client.dataset(run["defaultDatasetId"]).iterate_items())

        if not scraped_items:
            log.status = "completed_no_posts"
            db.commit()
            return CrawlResponse(
                message="No posts scraped from Apify.",
                log_id=log_id,
                posts_scraped=0,
                posts_saved=0,
                alerts_saved=0,
                sample=[],
            )

        posts_saved = 0
        alerts_saved = 0
        hashtag_regex = re.compile(r"#(\w+)")

        for item in scraped_items:
            uid = item.get("full_urn") or item.get("postUrn") or item.get("urn")
            if not uid:
                logger.warning(f"Skipping post without UID. Item: {item}")
                continue

            existing_post = (
                db.query(SocialMediaPost).filter(SocialMediaPost.uid == uid).first()
            )
            if existing_post:
                logger.info(f"Skipping already existing post with UID: {uid}")
                continue

            post_text = item.get("text", "")
            comments = item.get("comments", [])
            sentiment_result, _ = analyze_post_sentiment(
                openai_client, post_text
            )
            
            alert_result, _ = analyze_post_alert(openai_client, post_text)

            stats = item.get("stats", {})
            post_data = {
                "uid": uid,
                "company_id": company_id,
                "post_url": item.get("postUrl"),
                "post_description": post_text,
                "posted_at": _parse_posted_at(item.get("postedAt")),
                "likes": stats.get("total_reactions", 0),
                "comments_count": item.get("commentsCount", 0),
                "shares": stats.get("reposts", 0),
                "sentiment_label": sentiment_result.get("label"),
                "sentiment_score": sentiment_result.get("score"),
            }

            new_post = SocialMediaPost(**post_data)
            db.add(new_post)
            posts_saved += 1
            db.flush()

            # --- HASHTAG PROCESSING ---
            if post_text:
                hashtags = hashtag_regex.findall(post_text)
                for tag in hashtags:
                    hashtag_obj = (
                        db.query(Hashtag).filter(Hashtag.tag == tag.lower()).first()
                    )
                    if not hashtag_obj:
                        hashtag_obj = Hashtag(tag=tag.lower())
                        db.add(hashtag_obj)
                        db.commit()
                        db.refresh(hashtag_obj)
                    if hashtag_obj not in new_post.hashtags:
                        new_post.hashtags.append(hashtag_obj)
            # --- END HASHTAG PROCESSING ---

            if alert_result and alert_result.get("message"):
                new_alert = Alert(
                    company_id=company_id,
                    post_id=new_post.id,
                    alert_message=alert_result.get("message"),
                    severity=alert_result.get("severity"),
                )
                db.add(new_alert)
                alerts_saved += 1

        db.commit()

        log.end_time = datetime.utcnow()
        log.status = "completed"
        log.posts_scraped = len(scraped_items)
        log.posts_saved = posts_saved
        log.alerts_saved = alerts_saved
        db.commit()

        return CrawlResponse(
            message="Crawl completed successfully using Apify.",
            log_id=log_id,
            posts_scraped=len(scraped_items),
            posts_saved=posts_saved,
            alerts_saved=alerts_saved,
            sample=scraped_items[:3],
        )

    except Exception as e:
        db.rollback()
        log.end_time = datetime.utcnow()
        log.status = "failed"
        log.error_message = str(e)
        db.commit()
        logger.exception("Crawler run failed")
        raise HTTPException(status_code=500, detail=f"Crawler failed: {str(e)}")