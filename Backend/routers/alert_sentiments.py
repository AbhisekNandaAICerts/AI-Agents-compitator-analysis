from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, cast, Date, and_
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

# --- Dashboard APIs ---

@router.get("/alerts", response_model=List[schemas.AlertOut])
def get_alerts(
    db: Session = Depends(get_db),
    company_id: Optional[int] = Query(None),
    limit: int = Query(10, le=50)
):
    """
    Retrieves a list of alerts, optionally filtered by company ID.
    """
    query = db.query(models.Alert)
    if company_id:
        query = query.filter(models.Alert.company_id == company_id)

    alerts = query.order_by(models.Alert.created_at.desc()).limit(limit).all()
    return alerts

@router.get("/posts-with-alerts", response_model=List[schemas.PostWithAlert])
def get_posts_with_alerts(
    db: Session = Depends(get_db),
    company_id: Optional[int] = Query(None),
    limit: int = Query(10, le=50)
):
    """
    Retrieves posts that have associated alerts, combining data from both tables.
    """
    query = (
        db.query(models.SocialMediaPost, models.Alert)
        .join(models.Alert, models.SocialMediaPost.id == models.Alert.post_id)
    )

    if company_id:
        query = query.filter(and_(models.SocialMediaPost.company_id == company_id, models.Alert.company_id == company_id))

    posts_with_alerts = query.order_by(models.Alert.created_at.desc()).limit(limit).all()
    
    result = []
    for post, alert in posts_with_alerts:
        result.append(schemas.PostWithAlert(
            id=post.id,
            company_id=post.company_id,
            post_url=post.post_url,
            post_description=post.post_description,
            likes=post.likes,
            comments_count=post.comments_count,
            shares=post.shares,
            sentiment_label=post.sentiment_label,
            sentiment_score=post.sentiment_score,
            posted_at=post.posted_at,
            alert_id=alert.alert_id,
            alert_message=alert.alert_message,
            severity=alert.severity,
            alert_created_at=alert.created_at
        ))
    return result

@router.get("/sentiment-comparison", response_model=schemas.SentimentStats)
def get_sentiment_comparison(
    db: Session = Depends(get_db),
    company_id: int = Query(...),
    days: int = Query(30, ge=1, le=365)
):
    """
    Returns sentiment distribution for a given company over a time frame.
    """
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
