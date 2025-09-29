# schemas.py
from pydantic import BaseModel, HttpUrl, Field, EmailStr
from datetime import datetime
from typing import Optional, List, Dict


# ------------------------
# Company / Auth / Base schemas
# ------------------------
class CompanyBase(BaseModel):
    company_name: str = Field(..., example="Udacity")
    industry: Optional[str] = Field(None, example="EdTech")
    headquarters: Optional[str] = Field(None, example="San Francisco, CA")
    founded_year: Optional[int] = Field(None, example=2011)
    employee_count: Optional[int] = Field(None, example=500)
    website: Optional[HttpUrl] = Field(None, example="https://www.udacity.com")


class CompanyCreate(CompanyBase):
    pass


class CompanyUpdate(BaseModel):
    company_name: Optional[str] = None
    industry: Optional[str] = None
    headquarters: Optional[str] = None
    founded_year: Optional[int] = None
    employee_count: Optional[int] = None
    website: Optional[HttpUrl] = None


class CompanyOut(CompanyBase):
    company_id: int

    class Config:
        orm_mode = True


# ------------------------
# Company Social (for social profiles)
# ------------------------
class CompanySocialBase(BaseModel):
    platform: str = Field(..., example="LinkedIn")
    handle: Optional[str] = Field(None, example="@company")
    profile_url: Optional[HttpUrl] = None
    followers_count: Optional[int] = 0
    bio: Optional[str] = None


class CompanySocialCreate(CompanySocialBase):
    company_id: int


class CompanySocialUpdate(BaseModel):
    platform: Optional[str] = None
    handle: Optional[str] = None
    profile_url: Optional[HttpUrl] = None
    followers_count: Optional[int] = None
    bio: Optional[str] = None


class CompanySocialOut(CompanySocialBase):
    """
    Output schema for company social accounts.
    """
    id: int
    company_id: int
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None

    class Config:
        from_attributes = True
        orm_mode = True


# ------------------------
# Company with socials (added to satisfy routers expecting this)
# ------------------------
class CompanyWithSocials(CompanyOut):
    """
    Company output that includes an embedded list of social profiles.
    Routers that return a company plus its socials can use this schema.
    """
    socials: List[CompanySocialOut] = []

    class Config:
        orm_mode = True
        from_attributes = True


# ------------------------
# Token / User schemas
# ------------------------
class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: Optional[int] = None


class TokenPayload(BaseModel):
    sub: Optional[str] = None


class UserCreate(BaseModel):
    email: EmailStr
    password: str


class UserOut(BaseModel):
    id: int
    email: EmailStr
    is_active: bool
    is_superuser: bool

    class Config:
        orm_mode = True


class LoginRequest(BaseModel):
    username: EmailStr
    password: str


# ------------------------
# Social & Post related schemas
# ------------------------
class PostSummary(BaseModel):
    id: int
    company_id: int
    post_url: Optional[str]
    post_description: Optional[str]
    likes: int
    comments_count: int
    shares: Optional[int] = 0
    sentiment_label: Optional[str]
    sentiment_score: Optional[float]
    posted_at: Optional[datetime]

    class Config:
        from_attributes = True


class PostWithAlert(PostSummary):
    alert_id: int
    alert_message: str
    severity: str
    alert_created_at: datetime


class DashboardKPI(BaseModel):
    company_id: int
    total_posts: int
    total_likes: int
    total_comments: int
    engagement_rate: float


# ------------------------
# Trend & Sentiment schemas
# ------------------------
class TrendPoint(BaseModel):
    """
    Engagement trend point per day.
    Stored as datetime (midnight) to keep compatibility with Pydantic datetime serialization.
    """
    date: datetime
    likes: int = 0
    comments: int = 0
    shares: int = 0


class SentimentTrendPoint(BaseModel):
    """
    Sentiment trend point per day.
    positive/neutral/negative are counts of posts labeled with that sentiment.
    likes/comments are optional aggregates for context.
    """
    date: datetime
    positive: int = 0
    neutral: int = 0
    negative: int = 0
    likes: int = 0
    comments: int = 0


class SentimentStats(BaseModel):
    positive: int
    neutral: int
    negative: int


# ------------------------
# Alerts / Crawler schemas
# ------------------------
class AlertBase(BaseModel):
    alert_message: str
    severity: str


class AlertOut(AlertBase):
    alert_id: int
    company_id: int
    post_id: int
    created_at: datetime

    class Config:
        from_attributes = True


class CrawlerLogOut(BaseModel):
    log_id: int
    company_id: int
    start_time: datetime
    end_time: Optional[datetime]
    status: str
    posts_scraped: int
    posts_saved: int
    alerts_saved: int
    error_message: Optional[str]

    class Config:
        from_attributes = True


class CrawlResponse(BaseModel):
    message: str
    log_id: int
    posts_scraped: int
    posts_saved: int
    alerts_saved: int
    sample: List[dict]


# ------------------------
# Comparison schemas
# ------------------------
class SocialMediaMetrics(BaseModel):
    total_posts: int = 0
    total_reactions: int = 0
    total_comments: int = 0
    avg_reactions_per_post: float = 0.0


class ComparisonData(BaseModel):
    company_name: str
    social_media_metrics: SocialMediaMetrics
    engagement_trends: List[TrendPoint] = []
    sentiment_trends: List[SentimentTrendPoint] = []
    alert_count: int = 0
    alerts: List[AlertOut] = []


class FullComparison(BaseModel):
    company_a: ComparisonData
    company_b: ComparisonData


# ------------------------
# Hashtag analytics schemas
# ------------------------
class PostOut(BaseModel):
    post_id: int
    company_id: Optional[int]
    author_id: Optional[int] = None
    posted_at: datetime
    text: Optional[str] = None
    likes: int = 0
    comments_count: int = 0
    shares: int = 0

    class Config:
        from_attributes = True


class TopUserStat(BaseModel):
    user_id: Optional[int]
    display_name: Optional[str]
    mentions: int = 0
    total_likes: int = 0
    total_comments: int = 0
    total_shares: int = 0


class HashtagAnalytics(BaseModel):
    hashtag: str
    company_id: Optional[int] = None
    days: int = 30
    engagement_trends: List[TrendPoint] = []
    sentiment_trends: List[SentimentTrendPoint] = []
    total_mentions: int = 0
    top_posts: List[PostOut] = []
    top_users: List[TopUserStat] = []


class HashtagStat(BaseModel):
    hashtag: str
    mentions: int = 0
    total_likes: int = 0
    total_comments: int = 0
    total_shares: int = 0
    avg_engagement_per_post: float = 0.0
    sentiment: Dict[str, int] = Field(default_factory=lambda: {"positive": 0, "neutral": 0, "negative": 0})
    top_post_id: Optional[int] = None
    top_post_engagement: int = 0


class HashtagAnalysisResponse(BaseModel):
    company_id: int
    company_name: Optional[str] = None
    days: int = 30
    total_mentions: int = 0
    most_used_hashtag: Optional[str] = None
    top_hashtags_by_mentions: List[HashtagStat] = []
    top_hashtags_by_engagement: List[HashtagStat] = []
    hashtag_stats: List[HashtagStat] = []
    top_posts_for_company: List[PostOut] = []
