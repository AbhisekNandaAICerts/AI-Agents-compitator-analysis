# routers/dashboard.py
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, case, cast, Date
from typing import List, Optional
from datetime import datetime, timedelta

from core.database import SessionLocal
from core.auth import get_current_user
import models
import schemas

router = APIRouter(prefix="/dashboard", tags=["dashboard"], dependencies=[Depends(get_current_user)])

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- Dashboard Summary KPIs ---
@router.get("/summary", response_model=List[schemas.DashboardKPI])
def dashboard_summary(db: Session = Depends(get_db)):
    rows = (
        db.query(
            models.SocialMediaPost.company_id,
            func.count(models.SocialMediaPost.id).label("total_posts"),
            func.coalesce(func.sum(models.SocialMediaPost.likes), 0).label("total_likes"),
            func.coalesce(func.sum(models.SocialMediaPost.comments_count), 0).label("total_comments"),
        )
        .group_by(models.SocialMediaPost.company_id)
        .all()
    )

    result = []
    for r in rows:
        engagement_rate = 0.0
        if r.total_posts > 0:
            engagement_rate = (r.total_likes + r.total_comments) / r.total_posts
        result.append(
            schemas.DashboardKPI(
                company_id=r.company_id,
                total_posts=r.total_posts,
                total_likes=r.total_likes,
                total_comments=r.total_comments,
                engagement_rate=engagement_rate,
            )
        )
    return result

# --- Top Posts ---
@router.get("/top-posts", response_model=List[schemas.PostSummary])
def top_posts(
    db: Session = Depends(get_db),
    company_id: Optional[int] = None,
    limit: int = Query(10, le=50)
):
    query = db.query(models.SocialMediaPost)
    if company_id:
        query = query.filter(models.SocialMediaPost.company_id == company_id)

    posts = (
        query.order_by((models.SocialMediaPost.likes + models.SocialMediaPost.comments_count).desc())
        .limit(limit)
        .all()
    )
    return posts

# --- Trends: Engagement over time ---
@router.get("/trends/engagement", response_model=List[schemas.TrendPoint])
def engagement_trend(
    db: Session = Depends(get_db),
    company_id: int = Query(...),
    days: int = Query(30, ge=1, le=365)
):
    cutoff = datetime.utcnow() - timedelta(days=days)

    rows = (
        db.query(
            cast(models.SocialMediaPost.posted_at, Date).label("date"),
            func.coalesce(func.sum(models.SocialMediaPost.likes), 0).label("likes"),
            func.coalesce(func.sum(models.SocialMediaPost.comments_count), 0).label("comments"),
        )
        .filter(models.SocialMediaPost.company_id == company_id)
        .filter(models.SocialMediaPost.posted_at >= cutoff)
        .group_by(cast(models.SocialMediaPost.posted_at, Date))
        .order_by(cast(models.SocialMediaPost.posted_at, Date))
        .all()
    )

    return [schemas.TrendPoint(date=r.date, likes=r.likes, comments=r.comments) for r in rows]

# --- Trends: Sentiment distribution ---
@router.get("/trends/sentiment", response_model=schemas.SentimentStats)
def sentiment_distribution(
    db: Session = Depends(get_db),
    company_id: int = Query(...),
    days: int = Query(30, ge=1, le=365)
):
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        db.query(
            models.SocialMediaPost.sentiment_label,
            func.count(models.SocialMediaPost.id),
        )
        .filter(models.SocialMediaPost.company_id == company_id)
        .filter(models.SocialMediaPost.posted_at >= cutoff)
        .group_by(models.SocialMediaPost.sentiment_label)
        .all()
    )

    stats = {"positive": 0, "neutral": 0, "negative": 0}
    for label, count in rows:
        if not label:
            continue
        label = label.lower()
        if "pos" in label:
            stats["positive"] += count
        elif "neg" in label:
            stats["negative"] += count
        else:
            stats["neutral"] += count

    return schemas.SentimentStats(**stats)
