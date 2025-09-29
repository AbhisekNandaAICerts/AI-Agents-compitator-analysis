# comparisons.py
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func
from typing import List, Dict, Any, Optional, Tuple
from datetime import datetime, timedelta, date, time
import re

from core.database import SessionLocal
from core.auth import get_current_user

import models
import schemas

router = APIRouter(prefix="/comparisons", tags=["comparisons"], dependencies=[Depends(get_current_user)])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# -------------------------
# Utility helpers
# -------------------------
def _ensure_datetime_at_midnight(d: date | datetime) -> datetime:
    """Convert a date or datetime into a datetime at midnight (00:00:00)."""
    if isinstance(d, datetime):
        return datetime.combine(d.date(), time.min)
    return datetime.combine(d, time.min)


HASHTAG_SPLIT_REGEX = re.compile(r"[,\s]+")  # split on commas or whitespace


def _parse_hashtags_from_text(raw: Optional[Any]) -> List[str]:
    """
    Parse and normalize hashtags from a text field or from a list-like (InstrumentedList).
    Accepts:
      - string forms: "#ai #cloud", "ai, cloud", "['#ai','#ml']"
      - list/tuple: ['#ai', '#ml'] or InstrumentedList(['#ai', '#ml'])
      - mixed items (numbers) — coerced to str
    Normalization:
      - lowercased
      - leading '#' removed
      - removes surrounding punctuation
    """
    if raw is None:
        return []

    # If raw is a list/tuple/InstrumentedList — handle by joining elements
    # We detect sequence-like but exclude bytes/str
    if not isinstance(raw, (str, bytes)) and hasattr(raw, "__iter__"):
        parts = []
        # raw could be an InstrumentedList of SQLAlchemy Hashtag objects or strings
        for el in raw:
            if el is None:
                continue
            # If element looks like an object with 'tag' or 'name' attribute, try to use it
            if not isinstance(el, (str, bytes)):
                if hasattr(el, "tag"):
                    parts.append(str(getattr(el, "tag")))
                    continue
                if hasattr(el, "name"):
                    parts.append(str(getattr(el, "name")))
                    continue
                # fallback: try to stringify the element
                try:
                    parts.append(str(el))
                    continue
                except Exception:
                    continue
            else:
                parts.append(str(el))
        cleaned = " ".join(parts)
    else:
        # treat as string
        cleaned = str(raw)

    # remove common list-like characters leftover from JSON/text representations
    cleaned = cleaned.replace("[", " ").replace("]", " ").replace("'", " ").replace('"', " ")

    # split on commas / whitespace, then normalize tokens
    tokens = HASHTAG_SPLIT_REGEX.split(cleaned)
    tags: List[str] = []
    for token in tokens:
        if not token:
            continue
        t = token.strip().strip(",.;:!?#()")
        if not t:
            continue
        # drop leading '#', lowercase
        t = t.lstrip("#").lower()
        # remove any characters that are not alphanumeric, underscore, or hyphen
        t = re.sub(r"[^0-9a-z_\-]", "", t)
        if t:
            tags.append(t)
    return tags


def _post_to_postout_dict(post: models.SocialMediaPost) -> Dict[str, Any]:
    """
    Convert a SocialMediaPost SQLAlchemy object into a dict matching schemas.PostOut:
      post_id, company_id, author_id, posted_at, text, likes, comments_count, shares
    This avoids Pydantic validation errors when model attribute names differ.
    """
    return {
        "post_id": int(getattr(post, "id", None) or getattr(post, "post_id", None) or 0),
        "company_id": getattr(post, "company_id", None),
        "author_id": getattr(post, "author_id", None),
        "posted_at": getattr(post, "posted_at", None),
        "text": getattr(post, "post_description", None) or getattr(post, "text", None) or None,
        "likes": int(getattr(post, "likes", 0) or 0),
        "comments_count": int(getattr(post, "comments_count", 0) or 0),
        "shares": int(getattr(post, "shares", 0) or 0),
    }


# -------------------------
# Core aggregation helpers
# -------------------------
def _get_social_media_metrics(db: Session, company_id: int) -> schemas.SocialMediaMetrics:
    total_posts = db.query(models.SocialMediaPost).filter(models.SocialMediaPost.company_id == company_id).count()

    total_reactions = (
        db.query(func.coalesce(func.sum(models.SocialMediaPost.likes), 0))
        .filter(models.SocialMediaPost.company_id == company_id)
        .scalar()
    ) or 0

    total_comments = (
        db.query(func.coalesce(func.sum(models.SocialMediaPost.comments_count), 0))
        .filter(models.SocialMediaPost.company_id == company_id)
        .scalar()
    ) or 0

    avg_reactions_per_post = 0.0
    if total_posts > 0:
        avg_reactions_per_post = float(total_reactions) / float(total_posts)

    return schemas.SocialMediaMetrics(
        total_posts=total_posts,
        total_reactions=int(total_reactions),
        total_comments=int(total_comments),
        avg_reactions_per_post=round(avg_reactions_per_post, 2),
    )


def _get_engagement_trends(db: Session, company_id: int, days: int) -> List[schemas.TrendPoint]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(
            func.date(models.SocialMediaPost.posted_at).label("date"),
            func.coalesce(func.sum(models.SocialMediaPost.likes), 0).label("likes"),
            func.coalesce(func.sum(models.SocialMediaPost.comments_count), 0).label("comments"),
            func.coalesce(func.sum(models.SocialMediaPost.shares), 0).label("shares"),
        )
        .filter(models.SocialMediaPost.company_id == company_id)
        .filter(models.SocialMediaPost.posted_at >= cutoff)
        .group_by("date")
        .order_by("date")
        .all()
    )

    trend_points: List[schemas.TrendPoint] = []
    for r in rows:
        if r.date is None:
            continue
        dt = _ensure_datetime_at_midnight(r.date)
        trend_points.append(schemas.TrendPoint(date=dt, likes=int(r.likes or 0), comments=int(r.comments or 0), shares=int(r.shares or 0)))
    return trend_points


def _get_sentiment_trends(db: Session, company_id: int, days: int) -> List[schemas.SentimentTrendPoint]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(
            func.date(models.SocialMediaPost.posted_at).label("date"),
            models.SocialMediaPost.sentiment_label,
            func.count(models.SocialMediaPost.id).label("count"),
            func.coalesce(func.sum(models.SocialMediaPost.likes), 0).label("likes"),
            func.coalesce(func.sum(models.SocialMediaPost.comments_count), 0).label("comments"),
        )
        .filter(models.SocialMediaPost.company_id == company_id)
        .filter(models.SocialMediaPost.posted_at >= cutoff)
        .group_by("date", models.SocialMediaPost.sentiment_label)
        .order_by("date")
        .all()
    )

    date_map: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r.date is None:
            continue
        dt = _ensure_datetime_at_midnight(r.date)
        key = dt.isoformat()

        if key not in date_map:
            date_map[key] = {"date": dt, "positive": 0, "neutral": 0, "negative": 0, "likes": 0, "comments": 0}

        label = (r.sentiment_label or "neutral").strip().lower()
        cnt = int(r.count or 0)

        if "pos" in label:
            date_map[key]["positive"] += cnt
        elif "neg" in label:
            date_map[key]["negative"] += cnt
        else:
            date_map[key]["neutral"] += cnt

        date_map[key]["likes"] += int(r.likes or 0)
        date_map[key]["comments"] += int(r.comments or 0)

    trends: List[schemas.SentimentTrendPoint] = []
    for k in sorted(date_map.keys()):
        d = date_map[k]
        trends.append(
            schemas.SentimentTrendPoint(
                date=d["date"],
                positive=d["positive"],
                neutral=d["neutral"],
                negative=d["negative"],
                likes=d["likes"],
                comments=d["comments"],
            )
        )
    return trends


def _get_alerts(db: Session, company_id: int, limit: int = 10) -> List[schemas.AlertOut]:
    alerts = (
        db.query(models.Alert)
        .filter(models.Alert.company_id == company_id)
        .order_by(models.Alert.created_at.desc())
        .limit(limit)
        .all()
    )
    return alerts


@router.get("/full", response_model=schemas.FullComparison)
def get_full_comparison(
    company_a_id: int = Query(..., description="ID of the first company"),
    company_b_id: int = Query(..., description="ID of the second company"),
    days: int = Query(90, ge=1, le=365, description="Timeframe for data trends in days"),
    db: Session = Depends(get_db),
):
    company_a = db.query(models.Company).filter(models.Company.company_id == company_a_id).first()
    if not company_a:
        raise HTTPException(status_code=404, detail=f"Company with ID '{company_a_id}' not found")

    company_b = db.query(models.Company).filter(models.Company.company_id == company_b_id).first()
    if not company_b:
        raise HTTPException(status_code=404, detail=f"Company with ID '{company_b_id}' not found")

    data_a = schemas.ComparisonData(
        company_name=company_a.company_name,
        social_media_metrics=_get_social_media_metrics(db, company_a.company_id),
        engagement_trends=_get_engagement_trends(db, company_a.company_id, days),
        sentiment_trends=_get_sentiment_trends(db, company_a.company_id, days),
        alert_count=db.query(models.Alert).filter(models.Alert.company_id == company_a.company_id).count(),
        alerts=_get_alerts(db, company_a.company_id, limit=10),
    )

    data_b = schemas.ComparisonData(
        company_name=company_b.company_name,
        social_media_metrics=_get_social_media_metrics(db, company_b.company_id),
        engagement_trends=_get_engagement_trends(db, company_b.company_id, days),
        sentiment_trends=_get_sentiment_trends(db, company_b.company_id, days),
        alert_count=db.query(models.Alert).filter(models.Alert.company_id == company_b.company_id).count(),
        alerts=_get_alerts(db, company_b.company_id, limit=10),
    )

    return schemas.FullComparison(company_a=data_a, company_b=data_b)


# ---------------------------
# Hashtag analytics (general)
# ---------------------------
def _hashtag_filter_clause(hashtag: str) -> str:
    normalized = hashtag.lstrip("#").lower()
    pattern = f"%{normalized}%"
    return pattern


def _get_hashtag_engagement_trends(db: Session, hashtag: str, company_id: Optional[int], days: int) -> List[schemas.TrendPoint]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    pattern = _hashtag_filter_clause(hashtag)

    q = (
        db.query(
            func.date(models.SocialMediaPost.posted_at).label("date"),
            func.coalesce(func.sum(models.SocialMediaPost.likes), 0).label("likes"),
            func.coalesce(func.sum(models.SocialMediaPost.comments_count), 0).label("comments"),
            func.coalesce(func.sum(models.SocialMediaPost.shares), 0).label("shares"),
        )
        .filter(models.SocialMediaPost.posted_at >= cutoff)
    )

    if company_id:
        q = q.filter(models.SocialMediaPost.company_id == company_id)

    # If your DB uses a normalized hashtag table (it does) then SocialMediaPost.hashtags is a relationship,
    # and calling func.lower(models.SocialMediaPost.hashtags) will fail on some DBs.
    # In general this function is best-effort; for accurate DB-level hashtag queries consider joining the hashtag table.
    q = q.join(models.SocialMediaPost.hashtags).filter(func.lower(models.Hashtag.tag).ilike(pattern))

    q = q.group_by("date").order_by("date")

    rows = q.all()

    trend_points: List[schemas.TrendPoint] = []
    for r in rows:
        if r.date is None:
            continue
        dt = _ensure_datetime_at_midnight(r.date)
        trend_points.append(schemas.TrendPoint(date=dt, likes=int(r.likes or 0), comments=int(r.comments or 0), shares=int(r.shares or 0)))
    return trend_points


def _get_hashtag_sentiment_trends(db: Session, hashtag: str, company_id: Optional[int], days: int) -> List[schemas.SentimentTrendPoint]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    pattern = _hashtag_filter_clause(hashtag)

    q = (
        db.query(
            func.date(models.SocialMediaPost.posted_at).label("date"),
            models.SocialMediaPost.sentiment_label,
            func.count(models.SocialMediaPost.id).label("count"),
            func.coalesce(func.sum(models.SocialMediaPost.likes), 0).label("likes"),
            func.coalesce(func.sum(models.SocialMediaPost.comments_count), 0).label("comments"),
        )
        .filter(models.SocialMediaPost.posted_at >= cutoff)
    )

    if company_id:
        q = q.filter(models.SocialMediaPost.company_id == company_id)

    q = q.join(models.SocialMediaPost.hashtags).filter(func.lower(models.Hashtag.tag).ilike(pattern))

    q = q.group_by("date", models.SocialMediaPost.sentiment_label).order_by("date")

    rows = q.all()

    date_map: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r.date is None:
            continue
        dt = _ensure_datetime_at_midnight(r.date)
        key = dt.isoformat()
        if key not in date_map:
            date_map[key] = {"date": dt, "positive": 0, "neutral": 0, "negative": 0, "likes": 0, "comments": 0}

        label = (r.sentiment_label or "neutral").strip().lower()
        cnt = int(r.count or 0)
        if "pos" in label:
            date_map[key]["positive"] += cnt
        elif "neg" in label:
            date_map[key]["negative"] += cnt
        else:
            date_map[key]["neutral"] += cnt

        date_map[key]["likes"] += int(r.likes or 0)
        date_map[key]["comments"] += int(r.comments or 0)

    trends: List[schemas.SentimentTrendPoint] = []
    for k in sorted(date_map.keys()):
        d = date_map[k]
        trends.append(
            schemas.SentimentTrendPoint(
                date=d["date"],
                positive=d["positive"],
                neutral=d["neutral"],
                negative=d["negative"],
                likes=d["likes"],
                comments=d["comments"],
            )
        )
    return trends


def _get_hashtag_total_mentions(db: Session, hashtag: str, company_id: Optional[int], days: int) -> int:
    cutoff = datetime.utcnow() - timedelta(days=days)
    pattern = _hashtag_filter_clause(hashtag)

    q = db.query(func.count(models.SocialMediaPost.id)).filter(models.SocialMediaPost.posted_at >= cutoff)

    if company_id:
        q = q.filter(models.SocialMediaPost.company_id == company_id)

    q = q.join(models.SocialMediaPost.hashtags).filter(func.lower(models.Hashtag.tag).ilike(pattern))

    total = q.scalar() or 0
    return int(total)


def _get_hashtag_top_posts(db: Session, hashtag: str, company_id: Optional[int], days: int, limit: int = 5) -> List[Dict[str, Any]]:
    """
    Return top posts as dicts matching schemas.PostOut (safe for Pydantic validation).
    """
    cutoff = datetime.utcnow() - timedelta(days=days)
    pattern = _hashtag_filter_clause(hashtag)

    q = db.query(models.SocialMediaPost).filter(models.SocialMediaPost.posted_at >= cutoff)
    if company_id:
        q = q.filter(models.SocialMediaPost.company_id == company_id)

    q = q.join(models.SocialMediaPost.hashtags).filter(func.lower(models.Hashtag.tag).ilike(pattern))

    engagement_expr = (func.coalesce(models.SocialMediaPost.likes, 0) + func.coalesce(models.SocialMediaPost.comments_count, 0) + func.coalesce(models.SocialMediaPost.shares, 0))
    rows = q.order_by(engagement_expr.desc()).limit(limit).all()

    mapped = []
    for p in rows:
        mapped.append(_post_to_postout_dict(p))
    return mapped


def _get_hashtag_top_users(db: Session, hashtag: str, company_id: Optional[int], days: int, limit: int = 10) -> List[schemas.TopUserStat]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    pattern = _hashtag_filter_clause(hashtag)

    q = (
        db.query(
            models.SocialMediaPost.author_id.label("author_id"),
            func.count(models.SocialMediaPost.id).label("mentions"),
            func.coalesce(func.sum(models.SocialMediaPost.likes), 0).label("total_likes"),
            func.coalesce(func.sum(models.SocialMediaPost.comments_count), 0).label("total_comments"),
            func.coalesce(func.sum(models.SocialMediaPost.shares), 0).label("total_shares"),
        )
        .filter(models.SocialMediaPost.posted_at >= cutoff)
    )

    if company_id:
        q = q.filter(models.SocialMediaPost.company_id == company_id)

    q = q.join(models.SocialMediaPost.hashtags).filter(func.lower(models.Hashtag.tag).ilike(pattern))
    q = q.group_by(models.SocialMediaPost.author_id).order_by(func.count(models.SocialMediaPost.id).desc()).limit(limit)

    rows = q.all()

    results: List[schemas.TopUserStat] = []
    for r in rows:
        author_id = getattr(r, "author_id", None)
        user_display = None
        try:
            if author_id is not None:
                user = db.query(models.User).filter(models.User.id == author_id).first()
                if user:
                    user_display = getattr(user, "display_name", getattr(user, "name", None))
        except Exception:
            user_display = None

        results.append(
            schemas.TopUserStat(
                user_id=author_id,
                display_name=user_display,
                mentions=int(r.mentions or 0),
                total_likes=int(r.total_likes or 0),
                total_comments=int(r.total_comments or 0),
                total_shares=int(r.total_shares or 0),
            )
        )

    return results


@router.get("/hashtag", response_model=schemas.HashtagAnalytics)
def get_hashtag_analytics(
    hashtag: str = Query(..., description="Hashtag to analyze (with or without leading #)"),
    company_id: Optional[int] = Query(None, description="Optional company ID to filter to a single company"),
    days: int = Query(30, ge=1, le=365, description="Number of days to include in the analytics"),
    top_posts_limit: int = Query(5, ge=1, le=50, description="How many top posts to return"),
    db: Session = Depends(get_db),
):
    if not hashtag or not hashtag.strip():
        raise HTTPException(status_code=400, detail="hashtag parameter is required")

    pattern = hashtag.lstrip("#").strip().lower()

    analytics = schemas.HashtagAnalytics(hashtag=pattern, company_id=company_id, days=days)

    analytics.engagement_trends = _get_hashtag_engagement_trends(db, pattern, company_id, days)
    analytics.sentiment_trends = _get_hashtag_sentiment_trends(db, pattern, company_id, days)
    analytics.total_mentions = _get_hashtag_total_mentions(db, pattern, company_id, days)
    analytics.top_posts = _get_hashtag_top_posts(db, pattern, company_id, days, limit=top_posts_limit)
    analytics.top_users = _get_hashtag_top_users(db, pattern, company_id, days, limit=10)

    return analytics


# -------------------------
# Company-level hashtag analysis (detailed)
# -------------------------
def _fetch_company_posts_for_hashtag_analysis(db: Session, company_id: int, days: int) -> List[models.SocialMediaPost]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(models.SocialMediaPost)
        .filter(models.SocialMediaPost.company_id == company_id)
        .filter(models.SocialMediaPost.posted_at >= cutoff)
        .all()
    )
    return rows


def _compute_hashtag_aggregates_from_posts(posts: List[models.SocialMediaPost]) -> Tuple[Dict[str, Any], int]:
    hashtag_map: Dict[str, Dict[str, Any]] = {}
    total_mentions = 0

    for post in posts:
        raw = getattr(post, "hashtags", None)
        tags = _parse_hashtags_from_text(raw)
        if not tags:
            continue
        total_mentions += 1
        likes = int(getattr(post, "likes", 0) or 0)
        comments = int(getattr(post, "comments_count", 0) or 0)
        shares = int(getattr(post, "shares", 0) or 0)
        sentiment_label = (getattr(post, "sentiment_label", None) or "neutral").strip().lower()
        engagement = likes + comments + shares

        for t in tags:
            if t not in hashtag_map:
                hashtag_map[t] = {
                    "mentions": 0,
                    "total_likes": 0,
                    "total_comments": 0,
                    "total_shares": 0,
                    "posts": [],
                    "sentiment": {"positive": 0, "neutral": 0, "negative": 0},
                }
            entry = hashtag_map[t]
            entry["mentions"] += 1
            entry["total_likes"] += likes
            entry["total_comments"] += comments
            entry["total_shares"] += shares
            entry["posts"].append((post, engagement))
            if "pos" in sentiment_label:
                entry["sentiment"]["positive"] += 1
            elif "neg" in sentiment_label:
                entry["sentiment"]["negative"] += 1
            else:
                entry["sentiment"]["neutral"] += 1

    return hashtag_map, total_mentions


def _build_hashtag_stat_object(tag: str, data: Dict[str, Any]) -> schemas.HashtagStat:
    mentions = int(data.get("mentions", 0))
    total_likes = int(data.get("total_likes", 0))
    total_comments = int(data.get("total_comments", 0))
    total_shares = int(data.get("total_shares", 0))
    sum_engagement = total_likes + total_comments + total_shares
    avg_engagement = float(sum_engagement) / mentions if mentions > 0 else 0.0

    top_post_id = None
    top_post_engagement = 0
    posts = data.get("posts", [])
    for p_obj, eng in posts:
        if eng > top_post_engagement:
            top_post_engagement = int(eng or 0)
            top_post_id = int(getattr(p_obj, "id", None) or getattr(p_obj, "post_id", None) or None)

    return schemas.HashtagStat(
        hashtag=tag,
        mentions=mentions,
        total_likes=total_likes,
        total_comments=total_comments,
        total_shares=total_shares,
        avg_engagement_per_post=round(avg_engagement, 2),
        sentiment=data.get("sentiment", {"positive": 0, "neutral": 0, "negative": 0}),
        top_post_id=top_post_id,
        top_post_engagement=top_post_engagement,
    )


def _get_top_posts_for_company(posts: List[models.SocialMediaPost], limit: int = 10) -> List[Dict[str, Any]]:
    scored = []
    for p in posts:
        likes = int(getattr(p, "likes", 0) or 0)
        comments = int(getattr(p, "comments_count", 0) or 0)
        shares = int(getattr(p, "shares", 0) or 0)
        eng = likes + comments + shares
        scored.append((eng, p))
    scored.sort(key=lambda x: x[0], reverse=True)
    top_posts = [p for _, p in scored[:limit]]
    # map to dicts compatible with PostOut
    return [_post_to_postout_dict(p) for p in top_posts]


@router.get("/hashtag/company", response_model=schemas.HashtagAnalysisResponse)
def get_hashtag_analysis_for_company(
    company_id: int = Query(..., description="Company ID to analyze"),
    days: int = Query(30, ge=1, le=365, description="Lookback window in days"),
    top_n: int = Query(10, ge=1, le=50, description="How many top hashtags to return"),
    top_posts_limit: int = Query(10, ge=1, le=50, description="How many top posts to include"),
    db: Session = Depends(get_db),
):
    company = db.query(models.Company).filter(models.Company.company_id == company_id).first()
    if not company:
        raise HTTPException(status_code=404, detail=f"Company with ID '{company_id}' not found")

    posts = _fetch_company_posts_for_hashtag_analysis(db, company_id, days)
    hashtag_map, total_mentions = _compute_hashtag_aggregates_from_posts(posts)

    hashtag_stats = []
    for tag, data in hashtag_map.items():
        stat = _build_hashtag_stat_object(tag, data)
        hashtag_stats.append(stat)

    top_by_mentions = sorted(hashtag_stats, key=lambda x: x.mentions, reverse=True)[:top_n]
    top_by_engagement = sorted(hashtag_stats, key=lambda x: (x.total_likes + x.total_comments + x.total_shares), reverse=True)[:top_n]
    most_used = top_by_mentions[0].hashtag if top_by_mentions else None

    top_posts = _get_top_posts_for_company(posts, limit=top_posts_limit)

    response = schemas.HashtagAnalysisResponse(
        company_id=company_id,
        company_name=getattr(company, "company_name", None),
        days=days,
        total_mentions=total_mentions,
        most_used_hashtag=most_used,
        top_hashtags_by_mentions=top_by_mentions,
        top_hashtags_by_engagement=top_by_engagement,
        hashtag_stats=hashtag_stats,
        top_posts_for_company=top_posts,
    )

    return response
